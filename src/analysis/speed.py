# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Speed and cadence metrics.

Per DESIGN.md these are speed metrics, not quality metrics. A high
throughput system can be shipping bad work quickly; nothing here makes
a quality claim.

Metrics produced (all per subject repo):

- ``speed_issue_to_first_linked_pr``: distribution of the interval
  between an issue's ``created_at`` and the ``merged_at`` of the
  earliest PR linked to it (via ``linked_pr`` only). Reported as
  histogram (PNG + HTML) and CSV, with breakdown by issue-author
  producer.

- ``speed_pr_open_to_merge``: distribution of the interval between a
  PR's ``created_at`` and ``merged_at``. Reported as histogram (PNG
  + HTML) and CSV, with breakdown by PR-author producer.

- ``speed_fast_close``: post-cutoff (default 2026-05-03,
  ``--start-date`` overrides) histogram of the interval between an
  issue's ``created_at`` and ``closed_at``. Plus a threshold table at
  {1, 5, 15, 60} minutes giving counts and producer breakdown of the
  closer at each threshold.

- ``speed_mttr``: per-issue Mean Time To Resolution methodologies
  reported side by side, broken out by closer producer:

  * first-close interval: created_at -> first observed close
  * final-close interval: created_at -> last observed close (or
    closed_at if no events recorded)
  * cumulative-open time: sum of (open -> close) intervals across
    the issue's life

  The gap between cumulative-open and final-close is itself reported
  per the DESIGN.md recommendation; an issue closed once and never
  reopened has cumulative-open == first-close == final-close.

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
from ._plotly_html import write_html_with_title


log = logging.getLogger(__name__)


DEFAULT_FAST_CLOSE_START = "2026-05-03"
FAST_CLOSE_THRESHOLDS_MINUTES = (1, 5, 15, 60)

# Histogram bin edges in minutes, log-spaced. 1 min .. 90 days.
HIST_BIN_EDGES_MINUTES = np.array([
    0.5, 1, 2, 5, 10, 30, 60, 120,
    60 * 6, 60 * 24, 60 * 24 * 3, 60 * 24 * 7,
    60 * 24 * 14, 60 * 24 * 30, 60 * 24 * 90,
])

PRODUCER_DISPLAY_ORDER = (
    PRODUCER_HUMAN,
    "copilot",
    "claude-app",
    "hive-scanner",
    "hive-reviewer",
    "hive-merger",
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
    seen = set()
    out = []
    for p in PRODUCER_DISPLAY_ORDER:
        if p in values and p not in seen:
            out.append(p)
            seen.add(p)
    extras = sorted(set(values) - seen)
    return out + extras


def _format_minutes(m: float) -> str:
    """Pretty-print a minute count (used for histogram tick labels)."""
    if m < 1:
        return f"{int(m * 60)}s"
    if m < 60:
        return f"{int(m)}m"
    if m < 60 * 24:
        return f"{int(m / 60)}h"
    return f"{int(m / 60 / 24)}d"


# ----------------------------------------------------------------------
# Histogram helper
# ----------------------------------------------------------------------

def _plot_log_histogram(
    minutes: pd.Series,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
    *,
    by_producer: Optional[pd.DataFrame] = None,
) -> None:
    """Plot histogram on a log-minute x-axis. If ``by_producer`` is
    given (DataFrame indexed like ``minutes`` with a 'producer' column
    aligned to it), produce a stacked-by-producer view; otherwise a
    single-bar view.

    Args:
        minutes: Series of latencies in minutes.
        by_producer: DataFrame with 'producer' column, same index as
            ``minutes``.
    """
    if minutes.empty:
        log.warning("no data for %s", title)
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    bins = HIST_BIN_EDGES_MINUTES
    if by_producer is None:
        counts, _ = np.histogram(minutes.values, bins=bins)
        producers = ["all"]
        per_producer = {"all": counts}
    else:
        df = by_producer.copy()
        df["minutes"] = minutes.values
        producers = _ordered_producers(list(df["producer"].unique()))
        per_producer = {}
        for p in producers:
            sub = df[df["producer"] == p]["minutes"].values
            per_producer[p], _ = np.histogram(sub, bins=bins)

    centers = (bins[:-1] * bins[1:]) ** 0.5  # geometric centers

    # PNG (matplotlib): stacked bars
    fig, ax = plt.subplots(figsize=(13, 5))
    bottoms = np.zeros(len(centers))
    for p in producers:
        ax.bar(centers, per_producer[p], width=np.diff(bins) * 0.9,
               bottom=bottoms, align="center", label=p)
        bottoms = bottoms + per_producer[p]
    ax.set_xscale("log")
    ax.set_xlabel("latency (log scale)")
    ax.set_ylabel("count")
    ax.set_title(title)
    edge_labels = [_format_minutes(b) for b in bins]
    ax.set_xticks(bins)
    ax.set_xticklabels(edge_labels, rotation=45, ha="right", fontsize=8)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3, which="both", axis="y")
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_png)

    # HTML (plotly): stacked bars
    pfig = go.Figure()
    edge_labels_str = [_format_minutes(b) for b in bins]
    for p in producers:
        pfig.add_trace(go.Bar(
            x=[edge_labels_str[i] + "-" + edge_labels_str[i + 1]
               for i in range(len(bins) - 1)],
            y=per_producer[p],
            name=p,
            hovertemplate="bin: %{x}<br>" + p + ": %{y}<extra></extra>",
        ))
    pfig.update_layout(
        title=title,
        xaxis_title="latency bin",
        yaxis_title="count",
        barmode="stack",
        template="plotly_white",
    )
    write_html_with_title(pfig, out_html, tab_title)
    log.info("wrote %s", out_html)


# ----------------------------------------------------------------------
# Issue -> first linked PR (full history)
# ----------------------------------------------------------------------

def _issue_to_first_linked_pr(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
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
    return df


# ----------------------------------------------------------------------
# PR open to merge (full history)
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
    return df


# ----------------------------------------------------------------------
# Fast-close (post-cutoff)
# ----------------------------------------------------------------------

def _fast_close_data(
    conn: sqlite3.Connection, repo_id: int, start_date: str
) -> pd.DataFrame:
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
          AND i.created_at >= ?
        """,
        (repo_id, start_date),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["created_at_dt"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df["closed_at_dt"]  = pd.to_datetime(df["closed_at"], utc=True, errors="coerce")
    df = df.dropna(subset=["created_at_dt", "closed_at_dt"])
    df = df[df["closed_at_dt"] >= df["created_at_dt"]]
    df["minutes"] = (df["closed_at_dt"] - df["created_at_dt"]).dt.total_seconds() / 60.0
    df["closer_producer"] = df["closer_login"].apply(_classify_login)
    return df


def _fast_close_threshold_table(df: pd.DataFrame) -> pd.DataFrame:
    """Counts at each threshold, broken out by closer producer."""
    rows = []
    for t in FAST_CLOSE_THRESHOLDS_MINUTES:
        sub = df[df["minutes"] <= t]
        per = sub.groupby("closer_producer").size()
        for producer, count in per.items():
            rows.append({
                "threshold_minutes": t,
                "closer_producer":   producer,
                "count":             int(count),
            })
        rows.append({
            "threshold_minutes": t,
            "closer_producer":   "TOTAL",
            "count":             int(len(sub)),
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# MTTR
# ----------------------------------------------------------------------

def _mttr_data(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    """Per-issue MTTR methodologies.

    For each closed issue, we collect:
      - created_at
      - first_close: earliest 'closed' event, or closed_at if no events
      - final_close: latest 'closed' event, or closed_at if no events
      - cumulative_open_minutes: sum of (open->close) intervals over
        the issue's history
      - reopen_count: number of 'reopened' events
      - closer_producer: last closer (matches issue.closed_by_id)
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

    # Build per-issue event list including the synthetic 'created' anchor.
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

    out_rows = []
    for _, iss in issues.iterrows():
        ev_list = by_issue.get(iss["issue_id"], [])
        ev_list = sorted(ev_list, key=lambda x: x[1])
        closes = [t for et, t in ev_list if et == "closed"]
        reopens = [t for et, t in ev_list if et == "reopened"]
        first_close = closes[0] if closes else iss["closed_at_dt"]
        final_close = closes[-1] if closes else iss["closed_at_dt"]
        # Cumulative open: walk a state machine over events.
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
            # Issue was reopened and never re-closed in events; close it
            # with the last-known close time.
            cum_seconds += (final_close - last_open_at).total_seconds()
        if not ev_list:
            cum_seconds = (iss["closed_at_dt"] - iss["created_at_dt"]).total_seconds()
        out_rows.append({
            "issue_id":     iss["issue_id"],
            "first_close_minutes": (first_close - iss["created_at_dt"]).total_seconds() / 60.0,
            "final_close_minutes": (final_close - iss["created_at_dt"]).total_seconds() / 60.0,
            "cumulative_open_minutes": cum_seconds / 60.0,
            "reopen_count": len(reopens),
            "closer_producer": _classify_login(iss["closer_login"]),
        })
    return pd.DataFrame(out_rows)


def _summarize_mttr(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    rows = []
    for producer, sub in df.groupby("closer_producer"):
        rows.append({
            "closer_producer": producer,
            "n":               int(len(sub)),
            "n_reopened":      int((sub["reopen_count"] > 0).sum()),
            "first_close_median_min":     float(sub["first_close_minutes"].median()),
            "final_close_median_min":     float(sub["final_close_minutes"].median()),
            "cumulative_open_median_min": float(sub["cumulative_open_minutes"].median()),
            "first_close_p90_min":        float(sub["first_close_minutes"].quantile(0.9)),
            "final_close_p90_min":        float(sub["final_close_minutes"].quantile(0.9)),
            "cumulative_open_p90_min":    float(sub["cumulative_open_minutes"].quantile(0.9)),
            "final_minus_cumulative_median_min":
                float((sub["final_close_minutes"] - sub["cumulative_open_minutes"]).median()),
        })
    out = pd.DataFrame(rows)
    out = out.set_index("closer_producer").reindex(
        _ordered_producers(out["closer_producer"].tolist())
    ).reset_index()
    return out


def _plot_mttr_summary(
    summary: pd.DataFrame,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
) -> None:
    if summary.empty:
        log.warning("no data for %s", title)
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    producers = summary["closer_producer"].tolist()
    methods = [
        ("first_close_median_min",     "first-close (median)"),
        ("cumulative_open_median_min", "cumulative-open (median)"),
        ("final_close_median_min",     "final-close (median)"),
    ]
    fig, ax = plt.subplots(figsize=(max(8, len(producers) * 1.3), 5))
    width = 0.27
    x = np.arange(len(producers))
    for i, (col, label) in enumerate(methods):
        ax.bar(x + (i - 1) * width, summary[col].values,
               width=width, label=label)
    ax.set_yscale("log")
    ax.set_xticks(x)
    ax.set_xticklabels(producers, rotation=30, ha="right")
    ax.set_ylabel("median minutes (log scale)")
    ax.set_title(title)
    ax.legend(loc="upper right")
    ax.grid(True, alpha=0.3, axis="y", which="both")
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_png)

    pfig = go.Figure()
    for col, label in methods:
        pfig.add_trace(go.Bar(
            x=producers, y=summary[col],
            name=label,
            hovertemplate="%{x}<br>" + label + ": %{y:.1f} min<extra></extra>",
        ))
    pfig.update_layout(
        title=title,
        xaxis_title="closer producer",
        yaxis_title="median minutes (log)",
        yaxis_type="log",
        barmode="group",
        template="plotly_white",
    )
    write_html_with_title(pfig, out_html, tab_title)
    log.info("wrote %s", out_html)


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def run_for_repo(
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    output_dir: Path,
    *,
    fast_close_start: str,
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

    # --- Issue -> first linked PR ---
    df_i2p = _issue_to_first_linked_pr(conn, repo_id)
    if not df_i2p.empty:
        df_i2p[[
            "issue_id", "issue_created_at", "first_pr_merged_at",
            "minutes", "issue_producer", "issue_author_login",
        ]].to_csv(csvs / "speed_issue_to_first_linked_pr.csv", index=False)
        _plot_log_histogram(
            df_i2p["minutes"],
            title=f"Issue open -> first linked PR merge ({owner}/{name})",
            out_png=plots / "speed_issue_to_first_linked_pr.png",
            out_html=htmls / "speed_issue_to_first_linked_pr.html",
            tab_title=f"{name}: issue->first linked PR",
            by_producer=df_i2p[["issue_producer"]].rename(
                columns={"issue_producer": "producer"}
            ),
        )

    # --- PR open to merge ---
    df_pr = _pr_open_to_merge(conn, repo_id)
    if not df_pr.empty:
        df_pr[[
            "pr_id", "pr_created_at", "pr_merged_at",
            "minutes", "pr_producer", "pr_author_login",
        ]].to_csv(csvs / "speed_pr_open_to_merge.csv", index=False)
        _plot_log_histogram(
            df_pr["minutes"],
            title=f"PR open -> merge ({owner}/{name})",
            out_png=plots / "speed_pr_open_to_merge.png",
            out_html=htmls / "speed_pr_open_to_merge.html",
            tab_title=f"{name}: PR open->merge",
            by_producer=df_pr[["pr_producer"]].rename(
                columns={"pr_producer": "producer"}
            ),
        )

    # --- Fast close (post-cutoff) ---
    df_fc = _fast_close_data(conn, repo_id, fast_close_start)
    if not df_fc.empty:
        df_fc[[
            "issue_id", "number", "created_at", "closed_at",
            "minutes", "closer_producer", "closer_login",
        ]].to_csv(csvs / "speed_fast_close_issues.csv", index=False)
        thresh = _fast_close_threshold_table(df_fc)
        thresh.to_csv(csvs / "speed_fast_close_thresholds.csv", index=False)
        _plot_log_histogram(
            df_fc["minutes"],
            title=(
                f"Issue close latency since {fast_close_start} "
                f"({owner}/{name})"
            ),
            out_png=plots / "speed_fast_close_distribution.png",
            out_html=htmls / "speed_fast_close_distribution.html",
            tab_title=f"{name}: fast-close distribution",
            by_producer=df_fc[["closer_producer"]].rename(
                columns={"closer_producer": "producer"}
            ),
        )
        log.info(
            "[%s/%s] fast-close (since %s): %d issues, %d <=5min, %d <=60min",
            owner, name, fast_close_start, len(df_fc),
            int((df_fc["minutes"] <= 5).sum()),
            int((df_fc["minutes"] <= 60).sum()),
        )

    # --- MTTR ---
    df_mttr = _mttr_data(conn, repo_id)
    if not df_mttr.empty:
        df_mttr.to_csv(csvs / "speed_mttr_per_issue.csv", index=False)
        summary = _summarize_mttr(df_mttr)
        summary.to_csv(csvs / "speed_mttr_summary.csv", index=False)
        _plot_mttr_summary(
            summary,
            title=f"MTTR (median) by closer producer ({owner}/{name})",
            out_png=plots / "speed_mttr_summary.png",
            out_html=htmls / "speed_mttr_summary.html",
            tab_title=f"{name}: MTTR summary",
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, help="path to config.yaml")
    p.add_argument("--verbose", action="store_true")
    p.add_argument(
        "--fast-close-start",
        default=DEFAULT_FAST_CLOSE_START,
        help=(
            "ISO date for the fast-close metric's lower bound (default "
            f"{DEFAULT_FAST_CLOSE_START}); only the fast-close metric "
            "uses this filter."
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
                fast_close_start=args.fast_close_start,
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
