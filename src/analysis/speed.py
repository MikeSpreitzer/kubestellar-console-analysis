# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Speed and cadence metrics, weekly time series.

Per DESIGN.md these are speed metrics, not quality metrics. A high
throughput system can be shipping bad work quickly; nothing here makes
a quality claim.

Per DESIGN.md's era-awareness section the corpus is non-stationary on
a timescale of weeks (six ACMM-paper layer transitions in five months),
so all outputs are weekly time series rather than aggregates over the
full window. Era-boundary annotations are drawn on every plot.

Per-issue / per-PR values are placed in a weekly bin by the
**closing-PR's ``merged_at``** (the most recently merged linked PR),
with a fallback to the issue's ``closed_at`` for issues closed
without a linked merged PR. Issues closed without a PR are included;
the docstring of each metric notes which timestamp was used.

Exception: ``speed_issue_to_first_linked_pr`` measures the latency to
the *first* linked PR merge, not the closer, so it bins by that
first-merged PR's ``merged_at`` rather than by the closing PR's.
Internally consistent with the metric being measured; deviates from
the uniform binning rule above.

Metrics produced (all per subject repo, all weekly):

- ``speed_issue_to_first_linked_pr``: median per week per issue-author
  producer of the interval from issue ``created_at`` to the
  first-linked PR's ``merged_at``. Bin: that first-merged PR's
  ``merged_at``. Edges from ``linked_pr`` only.

- ``speed_pr_open_to_merge``: median per week per PR-author producer
  of the interval from PR ``created_at`` to PR ``merged_at``.

- ``speed_fast_close``: count per week of issues closed within
  ``--fast-close-threshold-minutes`` minutes (default 5) of being
  opened, stacked by closer producer. Use the threshold flag to
  produce the count for any threshold; this replaces the previous
  cumulative threshold table at fixed multiples.

- ``speed_mttr_*``: four separate charts, all stacked by closer
  producer. Each is per-week per-producer. The ``cumulative_open``
  methodology is the sum of ``(open -> close)`` intervals across the
  issue's life (excludes any closed-then-reopened gap); the
  ``final_close`` methodology is ``created_at -> last observed close``.
  The previous ``first_close`` methodology has been dropped per the
  conversation that produced this rewrite.

  * ``speed_mttr_cumulative_open_median``
  * ``speed_mttr_cumulative_open_mean``
  * ``speed_mttr_final_close_median``
  * ``speed_mttr_final_close_mean``

  DESIGN.md recommends paying attention to the gap between
  cumulative-open and final-close (an indicator of reopen-driven
  abandonment). That gap is not plotted directly; both methodologies
  appear side by side in their own charts, and the per-issue CSV
  ``speed_mttr_per_issue.csv`` carries both columns plus
  ``reopen_count`` so a reader can compute the gap and the
  reopen-frequency signal directly.

Run as ``python -m src.analysis.speed --config /config/config.yaml``.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402

from ..classifier.record import Record
from ..classifier.rules import (
    PRODUCER_HUMAN,
    PRODUCER_UNKNOWN,
    classify,
)
from ..common.config import load_config
from ..common.db import connect_readonly
from ..common.eras import annotate_matplotlib, annotate_plotly, to_week
from ._plotly_html import write_html_with_title


log = logging.getLogger(__name__)


DEFAULT_FAST_CLOSE_THRESHOLD_MIN = 5

PRODUCER_DISPLAY_ORDER = (
    PRODUCER_HUMAN,
    "copilot",
    "claude-app",
    "hive-scanner",
    "hive-reviewer",
    "hive-bot",
    "prow",
    "project-bot",
    "netlify",
    "dependabot",
    "other-bot-app",
    PRODUCER_UNKNOWN,
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _classify_login(login: Optional[str]) -> str:
    rec = Record(
        target_kind="issue",
        target_id=0,
        author_login=login,
        author_email=None,
        author_name=None,
        created_at="",
    )
    verdict, _ = classify(rec)
    return verdict.producer


def _ordered_producers(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for p in PRODUCER_DISPLAY_ORDER:
        if p in values and p not in seen:
            out.append(p)
            seen.add(p)
    extras = sorted(set(values) - seen)
    return out + extras


# to_week lives in src/common/eras.py and is imported above.
# A local _to_week alias keeps the call sites tidy.
_to_week = to_week


# ----------------------------------------------------------------------
# Generic plot helpers
# ----------------------------------------------------------------------

def _weekly_stacked_bars(
    df: pd.DataFrame,
    *,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
    y_label: str,
) -> None:
    """Plot a weekly stacked-bar chart.

    ``df`` must be a wide DataFrame indexed by tz-aware weekly
    timestamps, with one column per producer.
    """
    if df.empty:
        log.warning("no data for %s", title)
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    producers = _ordered_producers(list(df.columns))
    df = df.reindex(columns=producers, fill_value=0)

    weeks = df.index
    weeks_native = weeks.to_pydatetime()

    fig, ax = plt.subplots(figsize=(13, 5))
    bottoms = np.zeros(len(weeks))
    width = 6.0  # days; bar covers most of the week
    for p in producers:
        vals = df[p].values.astype(float)
        ax.bar(weeks_native, vals, width=width, bottom=bottoms,
               align="edge", label=p)
        bottoms = bottoms + vals
    ax.set_xlabel("week")
    ax.set_ylabel(y_label)
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3, axis="y")
    if len(weeks) > 0:
        annotate_matplotlib(
            ax, xlim=(weeks.min(), weeks.max() + pd.Timedelta(days=7)),
        )
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_png)

    pfig = go.Figure()
    for p in producers:
        pfig.add_trace(go.Bar(
            x=list(weeks),
            y=df[p].values.tolist(),
            name=p,
            hovertemplate="%{x|%Y-%m-%d}<br>" + p + ": %{y}<extra></extra>",
        ))
    pfig.update_layout(
        title=title,
        xaxis_title="week",
        yaxis_title=y_label,
        barmode="stack",
        template="plotly_white",
    )
    if len(weeks) > 0:
        annotate_plotly(
            pfig, xlim=(weeks.min(), weeks.max() + pd.Timedelta(days=7)),
        )
    write_html_with_title(pfig, out_html, tab_title)
    log.info("wrote %s", out_html)


def _weekly_per_producer_lines(
    df: pd.DataFrame,
    *,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
    y_label: str,
    log_y: bool = True,
) -> None:
    """Plot a weekly per-producer line chart of a statistic.

    ``df`` must be indexed by tz-aware weekly timestamps with one
    column per producer; cells are the statistic value (median or mean
    minutes) for that (week, producer). Missing weeks for a producer
    are NaN and not plotted.
    """
    if df.empty:
        log.warning("no data for %s", title)
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    producers = _ordered_producers(list(df.columns))
    df = df.reindex(columns=producers)

    weeks = df.index

    fig, ax = plt.subplots(figsize=(13, 5))
    for p in producers:
        vals = df[p].values
        if np.all(pd.isna(vals)):
            continue
        ax.plot(weeks.to_pydatetime(), vals, marker="o",
                markersize=4, linewidth=1.0, label=p)
    if log_y:
        ax.set_yscale("log")
    ax.set_xlabel("week")
    ax.set_ylabel(y_label)
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=8, ncol=2)
    ax.grid(True, alpha=0.3, axis="both", which="both")
    if len(weeks) > 0:
        annotate_matplotlib(
            ax, xlim=(weeks.min(), weeks.max() + pd.Timedelta(days=7)),
        )
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_png)

    pfig = go.Figure()
    for p in producers:
        vals = df[p]
        if vals.dropna().empty:
            continue
        pfig.add_trace(go.Scatter(
            x=list(weeks), y=vals.tolist(), name=p, mode="lines+markers",
            hovertemplate=("%{x|%Y-%m-%d}<br>" + p + ": %{y:.1f}"
                           "<extra></extra>"),
        ))
    pfig.update_layout(
        title=title, xaxis_title="week", yaxis_title=y_label,
        template="plotly_white",
        yaxis_type="log" if log_y else "linear",
    )
    if len(weeks) > 0:
        annotate_plotly(
            pfig, xlim=(weeks.min(), weeks.max() + pd.Timedelta(days=7)),
        )
    write_html_with_title(pfig, out_html, tab_title)
    log.info("wrote %s", out_html)


# ----------------------------------------------------------------------
# Closing-PR merged_at lookup: given an issue, the merged_at of the
# most recently merged linked PR (or NULL).
# ----------------------------------------------------------------------

def _closing_pr_merged_at(
    conn: sqlite3.Connection, repo_id: int
) -> dict[int, str]:
    """For each issue in repo_id with at least one linked merged PR,
    return issue_id -> ISO timestamp of that PR's merged_at (taking
    the most recently-merged PR if more than one is linked).
    """
    rows = conn.execute(
        """
        SELECT
            lp.issue_id,
            MAX(pr.merged_at) AS pr_merged_at
        FROM linked_pr lp
        JOIN issue i_iss ON i_iss.issue_id = lp.issue_id
        JOIN issue i_pr  ON i_pr.issue_id  = lp.pr_id
        JOIN pull_request pr ON pr.issue_id = i_pr.issue_id
        WHERE i_iss.repo_id = ?
          AND i_iss.is_pr   = 0
          AND i_pr.is_pr    = 1
          AND pr.merged     = 1
          AND pr.merged_at IS NOT NULL
        GROUP BY lp.issue_id
        """,
        (repo_id,),
    ).fetchall()
    return {r["issue_id"]: r["pr_merged_at"] for r in rows}


# ----------------------------------------------------------------------
# Issue -> first linked PR merge (weekly median by issue producer)
# ----------------------------------------------------------------------

def _issue_to_first_linked_pr(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    """Per-issue: created_at -> first linked PR's merged_at, plus
    issue-author producer. Bin column 'bin_ts' is the first-linked
    PR's merged_at (no fallback needed; an issue without a linked
    merged PR has no value here)."""
    rows = conn.execute(
        """
        SELECT
            i.issue_id  AS issue_id,
            i.created_at AS issue_created_at,
            a_iss.login  AS issue_author_login,
            MIN(pr.merged_at) AS first_pr_merged_at
        FROM linked_pr lp
        JOIN issue i ON i.issue_id = lp.issue_id
        JOIN issue ipr ON ipr.issue_id = lp.pr_id
        JOIN pull_request pr ON pr.issue_id = ipr.issue_id
        LEFT JOIN actor a_iss ON a_iss.actor_id = i.author_id
        WHERE i.repo_id = ?
          AND i.is_pr = 0
          AND pr.merged = 1
          AND pr.merged_at IS NOT NULL
        GROUP BY i.issue_id, i.created_at, a_iss.login
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["created_at_dt"] = pd.to_datetime(df["issue_created_at"], utc=True, errors="coerce")
    df["merged_at_dt"]  = pd.to_datetime(df["first_pr_merged_at"], utc=True, errors="coerce")
    df = df.dropna(subset=["created_at_dt", "merged_at_dt"])
    df = df[df["merged_at_dt"] >= df["created_at_dt"]]
    df["minutes"] = (df["merged_at_dt"] - df["created_at_dt"]).dt.total_seconds() / 60.0
    df["issue_producer"] = df["issue_author_login"].apply(_classify_login)
    df["bin_ts"] = df["merged_at_dt"]
    return df


# ----------------------------------------------------------------------
# PR open to merge (weekly median by PR producer)
# ----------------------------------------------------------------------

def _pr_open_to_merge(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    rows = conn.execute(
        """
        SELECT
            i.issue_id   AS pr_id,
            i.created_at AS pr_created_at,
            pr.merged_at AS pr_merged_at,
            a.login      AS pr_author_login
        FROM pull_request pr
        JOIN issue i ON i.issue_id = pr.issue_id
        LEFT JOIN actor a ON a.actor_id = i.author_id
        WHERE i.repo_id = ?
          AND pr.merged = 1
          AND pr.merged_at IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["created_at_dt"] = pd.to_datetime(df["pr_created_at"], utc=True, errors="coerce")
    df["merged_at_dt"]  = pd.to_datetime(df["pr_merged_at"], utc=True, errors="coerce")
    df = df.dropna(subset=["created_at_dt", "merged_at_dt"])
    df = df[df["merged_at_dt"] >= df["created_at_dt"]]
    df["minutes"] = (df["merged_at_dt"] - df["created_at_dt"]).dt.total_seconds() / 60.0
    df["pr_producer"] = df["pr_author_login"].apply(_classify_login)
    df["bin_ts"] = df["merged_at_dt"]
    return df


# ----------------------------------------------------------------------
# Fast-close data (issues closed within threshold)
# ----------------------------------------------------------------------

def _fast_close_data(
    conn: sqlite3.Connection,
    repo_id: int,
    threshold_minutes: int,
    *,
    closing_merged_at: dict[int, str],
) -> pd.DataFrame:
    """Per-issue: created_at, closed_at, closer producer, minutes.
    Restricted to issues closed within ``threshold_minutes``. Bin is
    the closing PR's merged_at if linked, else the issue's closed_at.

    ``closing_merged_at`` is an issue_id -> ISO-string dict supplied by
    the caller (run_for_repo computes it once and shares with
    _mttr_data).
    """
    rows = conn.execute(
        """
        SELECT
            i.issue_id AS issue_id,
            i.number   AS number,
            i.created_at AS created_at,
            i.closed_at  AS closed_at,
            a_clo.login  AS closer_login
        FROM issue i
        LEFT JOIN actor a_clo ON a_clo.actor_id = i.closed_by_id
        WHERE i.repo_id = ?
          AND i.is_pr = 0
          AND i.closed_at IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["created_at_dt"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df["closed_at_dt"]  = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
    df = df.dropna(subset=["created_at_dt", "closed_at_dt"])
    df = df[df["closed_at_dt"] >= df["created_at_dt"]]
    df["minutes"] = (df["closed_at_dt"] - df["created_at_dt"]).dt.total_seconds() / 60.0
    # Cache classify-by-login over distinct values; avoids one
    # classify(Record(...)) call per row when most rows repeat a small
    # set of bot logins (github-actions, kubestellar-hive, ...).
    closer_producer_by_login = {
        login: _classify_login(login)
        for login in df["closer_login"].drop_duplicates().tolist()
    }
    df["closer_producer"] = df["closer_login"].map(closer_producer_by_login)

    df = df[df["minutes"] <= threshold_minutes]
    if df.empty:
        return df

    bin_strs = df["issue_id"].map(closing_merged_at)
    bin_ts_from_pr = pd.to_datetime(bin_strs, utc=True, errors="coerce")
    df["bin_ts"] = bin_ts_from_pr.where(
        bin_ts_from_pr.notna(), df["closed_at_dt"]
    )
    return df


# ----------------------------------------------------------------------
# MTTR
# ----------------------------------------------------------------------

def _mttr_data(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    closing_merged_at: dict[int, str],
) -> pd.DataFrame:
    """Per-issue MTTR methodologies (cumulative_open and final_close;
    first_close is not produced in this rewrite).

    For each closed issue, we collect:
      - created_at
      - final_close: latest 'closed' event, or closed_at if no events
      - cumulative_open_minutes: sum of (open->close) intervals over
        the issue's history
      - reopen_count: number of 'reopened' events
      - closer_producer: last closer (matches issue.closed_by_id)
      - bin_ts: closing PR's merged_at if linked, else closed_at

    ``closing_merged_at`` is an issue_id -> ISO-string dict supplied by
    the caller (run_for_repo computes it once and shares with
    _fast_close_data).
    """
    issues = pd.DataFrame([dict(r) for r in conn.execute(
        """
        SELECT
            i.issue_id   AS issue_id,
            i.created_at AS created_at,
            i.closed_at  AS closed_at,
            a.login      AS closer_login
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.closed_by_id
        WHERE i.repo_id = ?
          AND i.is_pr = 0
          AND i.closed_at IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()])
    if issues.empty:
        return issues

    events = pd.DataFrame([dict(r) for r in conn.execute(
        """
        SELECT
            ie.issue_id   AS issue_id,
            ie.event_type AS event_type,
            ie.created_at AS created_at
        FROM issue_event ie
        JOIN issue i ON i.issue_id = ie.issue_id
        WHERE i.repo_id = ?
          AND i.is_pr = 0
          AND ie.event_type IN ('closed', 'reopened')
        ORDER BY ie.issue_id, ie.created_at
        """,
        (repo_id,),
    ).fetchall()])

    by_issue: dict[int, list[tuple[str, pd.Timestamp]]] = {}
    issues["created_at_dt"] = pd.to_datetime(issues["created_at"], utc=True, errors="coerce")
    issues["closed_at_dt"]  = pd.to_datetime(issues["closed_at"],  utc=True, errors="coerce")
    issues = issues.dropna(subset=["created_at_dt", "closed_at_dt"])

    if not events.empty:
        events["created_at_dt"] = pd.to_datetime(events["created_at"], utc=True, errors="coerce")
        events = events.dropna(subset=["created_at_dt"])
        for _, ev in events.iterrows():
            by_issue.setdefault(ev["issue_id"], []).append(
                (ev["event_type"], ev["created_at_dt"])
            )

    # Precompute closer-producer lookup once per unique login (cheap
    # dict lookup per row instead of one classify(Record) call per row).
    closer_producer_by_login = {
        login: _classify_login(login)
        for login in issues["closer_login"].drop_duplicates().tolist()
    }

    out_rows = []
    for _, iss in issues.iterrows():
        ev_list = by_issue.get(iss["issue_id"], [])
        ev_list = sorted(ev_list, key=lambda x: x[1])
        closes = [t for et, t in ev_list if et == "closed"]
        reopens = [t for et, t in ev_list if et == "reopened"]
        final_close = closes[-1] if closes else iss["closed_at_dt"]
        cum_seconds = 0.0
        state = "open"
        last_open_at = iss["created_at_dt"]
        for et, t in ev_list:
            if et == "closed" and state == "open":
                cum_seconds += (t - last_open_at).total_seconds()
                state = "closed"
            elif et == "reopened" and state == "closed":
                last_open_at = t
                state = "open"
        if state == "open":
            # Issue is currently closed (i.closed_at IS NOT NULL) but the
            # event stream ends with a 'reopened'. Close it with the
            # issue's actual current close time, not closes[-1] (which
            # is an EARLIER 'closed' event preceding the reopen and
            # would yield a negative duration).
            cum_seconds += (iss["closed_at_dt"] - last_open_at).total_seconds()
        if not ev_list:
            cum_seconds = (iss["closed_at_dt"] - iss["created_at_dt"]).total_seconds()

        out_rows.append({
            "issue_id":     iss["issue_id"],
            "final_close_minutes": (final_close - iss["created_at_dt"]).total_seconds() / 60.0,
            "cumulative_open_minutes": cum_seconds / 60.0,
            "reopen_count": len(reopens),
            "closer_producer": closer_producer_by_login[iss["closer_login"]],
        })
    df = pd.DataFrame(out_rows)
    if df.empty:
        return df
    # Build bin_ts as a single Series so its dtype is uniform
    # ([us, UTC] like every other to_datetime call against SQLite ISO
    # strings), avoiding the object-dtype hazard of mixing per-row
    # scalar Timestamps from two different parse paths.
    bin_ts_from_pr = pd.to_datetime(
        df["issue_id"].map(closing_merged_at), utc=True, errors="coerce",
    )
    closed_at_by_issue = issues.set_index("issue_id")["closed_at_dt"]
    df["bin_ts"] = bin_ts_from_pr.fillna(
        df["issue_id"].map(closed_at_by_issue),
    )
    return df


# ----------------------------------------------------------------------
# Aggregation helpers: weekly counts and weekly statistics.
# ----------------------------------------------------------------------

def _weekly_counts_by_producer(
    df: pd.DataFrame, *, producer_col: str
) -> pd.DataFrame:
    """Wide DataFrame indexed by week with one column per producer
    holding counts of rows in that (week, producer) cell.

    Rows whose bin_ts is NaT are dropped explicitly so the silent
    drop in groupby is not the only place they disappear.
    """
    if df.empty:
        return pd.DataFrame()
    work = pd.DataFrame({
        "week": _to_week(df["bin_ts"]),
        "producer": df[producer_col],
    }).dropna(subset=["week"])
    if work.empty:
        return pd.DataFrame()
    return (
        work.groupby(["week", "producer"]).size()
            .unstack(fill_value=0)
            .sort_index()
    )


def _weekly_stat_by_producer(
    df: pd.DataFrame, *, producer_col: str, value_col: str, stat: str
) -> pd.DataFrame:
    """Wide DataFrame indexed by week with one column per producer
    holding the named statistic (median|mean) of value_col in that
    (week, producer) cell. NaN where no values.

    Rows whose bin_ts is NaT are dropped explicitly. ``stat`` is
    passed through to ``Series.agg`` so any pandas-recognized
    aggregation name works (median, mean, p90 via callables, etc.)."""
    if df.empty:
        return pd.DataFrame()
    work = pd.DataFrame({
        "week": _to_week(df["bin_ts"]),
        "producer": df[producer_col],
        "value": df[value_col],
    }).dropna(subset=["week"])
    if work.empty:
        return pd.DataFrame()
    return (
        work.groupby(["week", "producer"])["value"].agg(stat)
            .unstack().sort_index()
    )


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def run_for_repo(
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    output_dir: Path,
    *,
    fast_close_threshold_minutes: int,
) -> None:
    repo_row = conn.execute(
        "SELECT repo_id FROM repo WHERE owner = ? AND name = ?",
        (owner, name),
    ).fetchone()
    if repo_row is None:
        log.warning("repo %s/%s not in database; skipping", owner, name)
        return
    repo_id = repo_row["repo_id"]

    safe_slug = f"{owner}_{name}"
    plots = output_dir / "plots" / safe_slug
    htmls = output_dir / "html" / safe_slug
    csvs  = output_dir / "csv"  / safe_slug
    csvs.mkdir(parents=True, exist_ok=True)

    # The closing-PR merged_at lookup is needed by both fast_close
    # (for binning) and mttr (for binning); compute once per repo and
    # share to avoid running the identical SQL twice.
    closing_merged_at = _closing_pr_merged_at(conn, repo_id)

    # --- Issue -> first linked PR merge: weekly median by issue producer ---
    df_i2p = _issue_to_first_linked_pr(conn, repo_id)
    if not df_i2p.empty:
        df_i2p[[
            "issue_id", "issue_created_at", "first_pr_merged_at",
            "minutes", "issue_producer", "issue_author_login",
            "bin_ts",
        ]].to_csv(csvs / "speed_issue_to_first_linked_pr.csv", index=False)
        wk = _weekly_stat_by_producer(
            df_i2p, producer_col="issue_producer",
            value_col="minutes", stat="median",
        )
        _weekly_per_producer_lines(
            wk,
            title=(f"Issue open -> first linked PR merge, "
                   f"weekly median by issue producer ({owner}/{name})"),
            out_png=plots / "speed_issue_to_first_linked_pr.png",
            out_html=htmls / "speed_issue_to_first_linked_pr.html",
            tab_title=f"{name}: issue->first linked PR merge (weekly)",
            y_label="median minutes (log)",
        )

    # --- PR open to merge: weekly median by PR producer ---
    df_pr = _pr_open_to_merge(conn, repo_id)
    if not df_pr.empty:
        df_pr[[
            "pr_id", "pr_created_at", "pr_merged_at",
            "minutes", "pr_producer", "pr_author_login",
            "bin_ts",
        ]].to_csv(csvs / "speed_pr_open_to_merge.csv", index=False)
        wk = _weekly_stat_by_producer(
            df_pr, producer_col="pr_producer",
            value_col="minutes", stat="median",
        )
        _weekly_per_producer_lines(
            wk,
            title=(f"PR open -> merge, weekly median by PR producer "
                   f"({owner}/{name})"),
            out_png=plots / "speed_pr_open_to_merge.png",
            out_html=htmls / "speed_pr_open_to_merge.html",
            tab_title=f"{name}: PR open->merge (weekly)",
            y_label="median minutes (log)",
        )

    # --- Fast close: weekly count of close-within-threshold, stacked by closer ---
    df_fc = _fast_close_data(
        conn, repo_id, fast_close_threshold_minutes,
        closing_merged_at=closing_merged_at,
    )
    log.info(
        "[%s/%s] fast-close (<= %d min): %d issues",
        owner, name, fast_close_threshold_minutes, len(df_fc),
    )
    if not df_fc.empty:
        df_fc[[
            "issue_id", "number", "created_at", "closed_at",
            "minutes", "closer_producer", "closer_login",
            "bin_ts",
        ]].to_csv(csvs / "speed_fast_close_issues.csv", index=False)
        counts = _weekly_counts_by_producer(
            df_fc, producer_col="closer_producer",
        )
        counts.to_csv(csvs / "speed_fast_close.csv")
        _weekly_stacked_bars(
            counts,
            title=(f"Fast-close: issues closed <= "
                   f"{fast_close_threshold_minutes} min after open, "
                   f"weekly count by closer producer ({owner}/{name})"),
            out_png=plots / "speed_fast_close.png",
            out_html=htmls / "speed_fast_close.html",
            tab_title=f"{name}: fast-close (weekly)",
            y_label="issues closed within threshold",
        )

    # --- MTTR: four charts (cumulative_open|final_close x median|mean) ---
    df_mttr = _mttr_data(conn, repo_id, closing_merged_at=closing_merged_at)
    if not df_mttr.empty:
        df_mttr.to_csv(csvs / "speed_mttr_per_issue.csv", index=False)
        for value_col, slug, label in [
            ("cumulative_open_minutes", "cumulative_open", "cumulative-open"),
            ("final_close_minutes",     "final_close",     "final-close"),
        ]:
            for stat in ("median", "mean"):
                wk = _weekly_stat_by_producer(
                    df_mttr, producer_col="closer_producer",
                    value_col=value_col, stat=stat,
                )
                wk.to_csv(csvs / f"speed_mttr_{slug}_{stat}.csv")
                _weekly_per_producer_lines(
                    wk,
                    title=(f"MTTR ({label}, weekly {stat}) "
                           f"by closer producer ({owner}/{name})"),
                    out_png=plots / f"speed_mttr_{slug}_{stat}.png",
                    out_html=htmls / f"speed_mttr_{slug}_{stat}.html",
                    tab_title=f"{name}: MTTR {label} {stat} (weekly)",
                    y_label=f"{stat} minutes (log)",
                )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, help="path to config.yaml")
    p.add_argument("--verbose", action="store_true")
    p.add_argument(
        "--fast-close-threshold-minutes",
        type=int,
        default=DEFAULT_FAST_CLOSE_THRESHOLD_MIN,
        help=(
            "Fast-close threshold in minutes (default "
            f"{DEFAULT_FAST_CLOSE_THRESHOLD_MIN}). Issues closed within "
            "this many minutes of being opened are counted in the "
            "weekly fast-close time series."
        ),
    )
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    cfg = load_config(args.config)
    db_path = Path(cfg.data_dir) / "db.sqlite"
    output_dir = Path(cfg.output_dir)

    conn = connect_readonly(db_path)
    try:
        for repo in cfg.repos:
            if repo.role not in ("subject", "both"):
                continue
            log.info("speed: %s/%s", repo.owner, repo.name)
            run_for_repo(
                conn, repo.owner, repo.name, output_dir,
                fast_close_threshold_minutes=args.fast_close_threshold_minutes,
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
