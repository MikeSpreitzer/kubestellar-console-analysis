# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""First-look analysis: bot-credentialed vs. human-credentialed activity
over time, using only the metadata already in the database.

Produces six plots, each accompanied by its underlying CSV:

1. Issues opened per day, by author credential class.
2. Issues closed per day, by closer credential class.
3. PRs opened per day, by author credential class.
4. PRs merged per day, by merger credential class.
5. Comments on issues per day, by commenter credential class.
6. Comments on PRs per day, by commenter credential class.

Classification is the simplest possible: an actor login ending in
``[bot]`` is bot-credentialed, anything else is human-credentialed. This
is a *credential* classification, not a producer classification --
human-credentialed work is an upper bound on actual human work, since
humans may also run automation under their own credentials. The
``producer_classification`` table is not used by this script; that's
task 7's responsibility, and this analysis is a first look that
deliberately precedes it.

Run as ``python -m src.analysis.first_look --config /config/config.yaml``.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")  # non-interactive backend; plot to file only
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
from plotly.subplots import make_subplots  # noqa: E402

from ..classifier.record import Record
from ..classifier.rules import (
    CREDENTIAL_BOT,
    CREDENTIAL_HUMAN,
    CREDENTIAL_UNKNOWN,
    PRODUCER_HUMAN,
    classify,
    credential_class_of,
)
from ..common.config import load_config
from ..common.db import connect_readonly
from ..common.eras import annotate_matplotlib, annotate_plotly, to_week
from ._plotly_html import write_html_with_title


log = logging.getLogger(__name__)


# Color choices: bot lines warmer, human cooler. Consistent across plots.
COLORS = {
    CREDENTIAL_HUMAN: "#1f77b4",  # blue
    CREDENTIAL_BOT: "#d62728",    # red
    CREDENTIAL_UNKNOWN: "#7f7f7f",  # gray
}


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _classify_login(login: Optional[str]) -> str:
    """Classify a GitHub login into a coarse credential class.

    Wraps the classifier: builds a minimal Record, runs the rule
    list, returns the credential class of the resulting producer.
    Used by analyses where the actor's login is the only available
    signal (issues, PRs, comments, reviews on the GitHub side).
    """
    rec = Record(
        target_kind="issue",  # kind doesn't affect credential classification
        target_id=0,
        author_login=login,
        author_email=None,
        author_name=None,
        created_at="",
    )
    verdict, _ = classify(rec)
    return credential_class_of(verdict.producer)


def _daily_counts(
    df: pd.DataFrame,
    timestamp_col: str,
    classification_col: str,
) -> pd.DataFrame:
    """Group rows by UTC day of timestamp_col and classification_col,
    yielding a wide DataFrame with one column per classification value."""
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df[timestamp_col] = pd.to_datetime(df[timestamp_col], utc=True, errors="coerce")
    df = df.dropna(subset=[timestamp_col])
    df["date"] = df[timestamp_col].dt.tz_convert("UTC").dt.floor("D")
    grouped = (
        df.groupby(["date", classification_col])
          .size()
          .unstack(fill_value=0)
          .sort_index()
    )
    # Ensure consistent column ordering for plotting.
    cols_in_pref = [
        c for c in [CREDENTIAL_HUMAN, CREDENTIAL_BOT, CREDENTIAL_UNKNOWN]
        if c in grouped.columns
    ]
    return grouped[cols_in_pref]


def _plot_stacked(
    daily: pd.DataFrame,
    title: str,
    out_path_png: Path,
    out_path_html: Path,
    tab_title: str,
) -> None:
    """Render daily counts as a stacked area plot in both PNG and HTML.

    PNG via matplotlib (portable, paste-into-docs).
    HTML via plotly (interactive hover for precise values).
    """
    if daily.empty:
        log.warning("no data to plot for %s", title)
        return
    out_path_png.parent.mkdir(parents=True, exist_ok=True)
    out_path_html.parent.mkdir(parents=True, exist_ok=True)

    # PNG via matplotlib
    fig, ax = plt.subplots(figsize=(12, 5))
    colors = [COLORS.get(c, "#999999") for c in daily.columns]
    ax.stackplot(daily.index, daily.T.values, labels=list(daily.columns), colors=colors, alpha=0.85)
    ax.set_title(title)
    ax.set_xlabel("date (UTC)")
    ax.set_ylabel("count per day")
    ax.legend(loc="upper left")
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_path_png)

    # HTML via plotly (interactive)
    plotly_fig = go.Figure()
    for col in daily.columns:
        plotly_fig.add_trace(go.Scatter(
            x=daily.index,
            y=daily[col],
            name=col,
            mode="lines",
            stackgroup="one",
            line=dict(width=0.5, color=COLORS.get(col, "#999999")),
            fillcolor=COLORS.get(col, "#999999"),
            hovertemplate=(
                "<b>%{x|%Y-%m-%d}</b><br>"
                + col + ": %{y}<extra></extra>"
            ),
        ))
    plotly_fig.update_layout(
        xaxis_title="date (UTC)",
        yaxis_title="count per day",
        hovermode="x unified",
        template="plotly_white",
    )
    write_html_with_title(plotly_fig, out_path_html, tab_title, page_heading=title)
    log.info("wrote %s", out_path_html)


def _save_csv(daily: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(out_path)
    log.info("wrote %s", out_path)


# ----------------------------------------------------------------------
# Per-graph data loaders
# ----------------------------------------------------------------------

def _issues_authored(conn: sqlite3.Connection, repo_id: int) -> pd.DataFrame:
    """Issues only (not PRs), with author credential classification."""
    rows = conn.execute(
        """
        SELECT i.created_at, a.login AS author_login
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.author_id
        WHERE i.repo_id = ? AND i.is_pr = 0
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["credential"] = df["author_login"].apply(_classify_login)
    return df


def _issues_closed(conn: sqlite3.Connection, repo_id: int) -> pd.DataFrame:
    """Issues only (not PRs) that have been closed, with closer
    credential classification.

    Note: GitHub's closed_by_id can be NULL even on closed issues, e.g.
    when closure is implied by an auto-closing linked PR or for some
    older closures. Those rows are classified as 'unknown'.
    """
    rows = conn.execute(
        """
        SELECT i.closed_at, a.login AS closer_login
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.closed_by_id
        WHERE i.repo_id = ?
          AND i.is_pr = 0
          AND i.state = 'closed'
          AND i.closed_at IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["credential"] = df["closer_login"].apply(_classify_login)
    return df


def _prs_authored(conn: sqlite3.Connection, repo_id: int) -> pd.DataFrame:
    """PRs, with author credential classification."""
    rows = conn.execute(
        """
        SELECT i.created_at, a.login AS author_login
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.author_id
        WHERE i.repo_id = ? AND i.is_pr = 1
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["credential"] = df["author_login"].apply(_classify_login)
    return df


def _comments_on(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    is_pr: bool,
) -> pd.DataFrame:
    """Comments on issues (is_pr=False) or PRs (is_pr=True), with
    commenter credential classification."""
    rows = conn.execute(
        """
        SELECT c.created_at, a.login AS author_login
        FROM comment c
        JOIN issue i ON i.issue_id = c.issue_id
        LEFT JOIN actor a ON a.actor_id = c.author_id
        WHERE i.repo_id = ? AND i.is_pr = ?
        """,
        (repo_id, 1 if is_pr else 0),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["credential"] = df["author_login"].apply(_classify_login)
    return df


def _prs_merged(conn: sqlite3.Connection, repo_id: int) -> pd.DataFrame:
    """PRs that were merged, with merger credential classification."""
    rows = conn.execute(
        """
        SELECT pr.merged_at, a.login AS merger_login
        FROM pull_request pr
        JOIN issue i ON i.issue_id = pr.issue_id
        LEFT JOIN actor a ON a.actor_id = pr.merged_by_id
        WHERE i.repo_id = ? AND pr.merged = 1 AND pr.merged_at IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["credential"] = df["merger_login"].apply(_classify_login)
    return df


# ----------------------------------------------------------------------
# Volume calibration: distribution of issues-closed-per-merged-PR,
# weekly stacked-bar over six buckets (0, 1, 2, 3, 4, 5+). PR-creation
# week anchor; merged-PRs only.
# ----------------------------------------------------------------------

# Bucket labels and the predicate that maps a PR's close-count to a
# bucket label. Order matters; this is also the stack order in the
# plot legend.
_PR_CLOSE_BUCKETS: list[tuple[str, callable]] = [
    ("0",  lambda n: n == 0),
    ("1",  lambda n: n == 1),
    ("2",  lambda n: n == 2),
    ("3",  lambda n: n == 3),
    ("4",  lambda n: n == 4),
    ("5+", lambda n: n >= 5),
]


def _merged_pr_close_counts(
    conn: sqlite3.Connection, repo_id: int,
) -> pd.DataFrame:
    """One row per merged PR with its created_at and the count of
    issues it closed (via linked_pr).

    "Closed" = any linked_pr row for this PR. That's PR-body
    keyword scanning + GitHub's linked-issue events; a PR that
    closed an issue without leaving either signal is undercounted.
    """
    rows = conn.execute(
        """
        SELECT
            pr.issue_id  AS pr_id,
            i.created_at AS created_at,
            (SELECT COUNT(*) FROM linked_pr lp
              WHERE lp.pr_id = pr.issue_id) AS n_closed
        FROM pull_request pr
        JOIN issue i ON i.issue_id = pr.issue_id
        WHERE i.repo_id = ?
          AND i.is_pr = 1
          AND pr.merged = 1
        """,
        (repo_id,),
    ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def _weekly_pr_close_distribution(df: pd.DataFrame) -> pd.DataFrame:
    """Return a DataFrame indexed by week with one column per bucket,
    holding the count of merged PRs created that week with that
    close-count."""
    if df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["created_at_dt"] = pd.to_datetime(
        df["created_at"], utc=True, errors="coerce",
    )
    df = df.dropna(subset=["created_at_dt"])
    if df.empty:
        return pd.DataFrame()
    df["week"] = to_week(df["created_at_dt"])
    df["bucket"] = df["n_closed"].apply(_bucket_label)
    counts = (
        df.groupby(["week", "bucket"]).size()
          .unstack(fill_value=0)
          .sort_index()
    )
    # Put bucket columns in stack order; missing buckets fill with 0.
    bucket_order = [label for label, _ in _PR_CLOSE_BUCKETS]
    return counts.reindex(columns=bucket_order, fill_value=0)


def _bucket_label(n: int) -> str:
    for label, predicate in _PR_CLOSE_BUCKETS:
        if predicate(n):
            return label
    return "5+"  # unreachable; guards against negative n


def _plot_pr_close_distribution(
    counts: pd.DataFrame,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
) -> None:
    if counts.empty:
        log.warning("no data for %s", title)
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    weeks = counts.index
    buckets = list(counts.columns)
    weeks_native = weeks.to_pydatetime()

    fig, ax = plt.subplots(figsize=(13, 5))
    bottoms = np.zeros(len(weeks))
    width = 6.0
    for b in buckets:
        vals = counts[b].values.astype(float)
        ax.bar(weeks_native, vals, width=width, bottom=bottoms,
               align="edge", label=f"closes {b}")
        bottoms = bottoms + vals
    ax.set_xlabel("week")
    ax.set_ylabel("merged PRs")
    ax.set_title(title)
    ax.legend(loc="upper left", fontsize=8)
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
    for b in buckets:
        pfig.add_trace(go.Bar(
            x=list(weeks),
            y=counts[b].values.tolist(),
            name=f"closes {b}",
            hovertemplate=(
                "%{x|%Y-%m-%d}<br>closes " + b + ": %{y}<extra></extra>"
            ),
        ))
    pfig.update_layout(
        xaxis_title="week",
        yaxis_title="merged PRs",
        barmode="stack",
        template="plotly_white",
    )
    if len(weeks) > 0:
        annotate_plotly(
            pfig, xlim=(weeks.min(), weeks.max() + pd.Timedelta(days=7)),
        )
    write_html_with_title(pfig, out_html, tab_title, page_heading=title)
    log.info("wrote %s", out_html)


# ----------------------------------------------------------------------
# Volume calibration: per-week distribution of issues-per-human-author,
# rendered as a tall column of one-panel-per-week histograms. Starts at
# the L5 boundary (2026-04-06) and grows unboundedly as new weeks are
# added.
# ----------------------------------------------------------------------

# Log-spaced bin edges over issue counts. Each bin spans [edges[i],
# edges[i+1]); the final bin catches the tail.
_HIST_BIN_EDGES = [1, 2, 3, 6, 11, 21, 51, 101, float("inf")]
_HIST_BIN_LABELS = [
    "1", "2", "3-5", "6-10", "11-20", "21-50", "51-100", "100+",
]
# First week to include: Monday on or after L5 boundary 2026-04-03.
_HIST_START_WEEK = pd.Timestamp("2026-04-06", tz="UTC")


def _human_issue_counts_per_week(
    conn: sqlite3.Connection, repo_id: int,
) -> pd.DataFrame:
    """For each (week, human author), count issues authored that week.
    Author is human-credentialed iff the v3 classifier produced
    PRODUCER_HUMAN for that issue; the same author may appear in
    multiple weeks. Restricted to issues (is_pr = 0)."""
    rows = conn.execute(
        """
        SELECT
            i.author_id  AS author_id,
            i.created_at AS created_at
        FROM issue i
        JOIN producer_classification pc
          ON pc.target_kind = 'issue'
         AND pc.target_id = i.issue_id
        WHERE i.repo_id = ?
          AND i.is_pr = 0
          AND pc.classifier_version = 'v3'
          AND pc.producer = ?
          AND i.author_id IS NOT NULL
        """,
        (repo_id, PRODUCER_HUMAN),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["created_at_dt"] = pd.to_datetime(
        df["created_at"], utc=True, errors="coerce",
    )
    df = df.dropna(subset=["created_at_dt"])
    df["week"] = to_week(df["created_at_dt"])
    return df.groupby(["week", "author_id"]).size().reset_index(name="n_issues")


def _human_issue_distribution_long(
    per_author: pd.DataFrame,
) -> pd.DataFrame:
    """Long-format DataFrame (week, bin_label, n_humans) where
    n_humans is the count of distinct authors whose week's
    issue-count fell into that bin."""
    if per_author.empty:
        return pd.DataFrame(
            columns=["week", "bin_label", "n_humans"]
        )
    per_author = per_author[
        per_author["week"] >= _HIST_START_WEEK
    ].copy()
    if per_author.empty:
        return pd.DataFrame(
            columns=["week", "bin_label", "n_humans"]
        )
    # np.digitize: bin index of value, with right=False so v in
    # [edges[i], edges[i+1]) yields i+1; subtract 1 to get a
    # zero-based label index.
    bin_idx = np.digitize(
        per_author["n_issues"].values, _HIST_BIN_EDGES, right=False,
    ) - 1
    bin_idx = np.clip(bin_idx, 0, len(_HIST_BIN_LABELS) - 1)
    per_author["bin_label"] = [_HIST_BIN_LABELS[i] for i in bin_idx]
    out = (
        per_author.groupby(["week", "bin_label"]).size()
                  .reset_index(name="n_humans")
    )
    return out


def _plot_human_issue_distribution(
    long_df: pd.DataFrame,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
) -> None:
    if long_df.empty:
        log.warning("no data for %s", title)
        return
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    weeks = sorted(long_df["week"].unique())
    n = len(weeks)
    # One panel per week, single column. Panel height stays constant
    # (1.6 inches) so the figure grows linearly with weeks.
    panel_h = 1.6
    fig, axes = plt.subplots(
        nrows=n, ncols=1, figsize=(8, max(2.0, panel_h * n)),
        sharex=True,
    )
    if n == 1:
        axes = [axes]
    x_pos = np.arange(len(_HIST_BIN_LABELS))
    for ax, week in zip(axes, weeks):
        sub = long_df[long_df["week"] == week].set_index("bin_label")
        vals = sub["n_humans"].reindex(_HIST_BIN_LABELS, fill_value=0).values
        ax.bar(x_pos, vals, color="steelblue")
        ax.set_ylabel(week.strftime("%Y-%m-%d"), rotation=0,
                      ha="right", va="center", fontsize=8)
        # Integer y-ticks only.
        ymax = max(int(vals.max()), 1)
        ax.set_yticks(range(0, ymax + 1, max(1, ymax // 4)))
        ax.grid(True, alpha=0.3, axis="y")
    axes[-1].set_xticks(x_pos)
    axes[-1].set_xticklabels(_HIST_BIN_LABELS, rotation=0, fontsize=8)
    axes[-1].set_xlabel("issues filed by one human author that week")
    fig.suptitle(title, fontsize=11, y=0.995)
    fig.tight_layout(rect=(0, 0, 1, 0.99))
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_png)

    # Plotly: vertical stack of one histogram per week, mirroring the
    # PNG layout. Each row is a single Bar trace over the bin labels;
    # y-axes are independent per row so a 1-author week and a 32-author
    # week each scale to their own data.
    week_labels = [w.strftime("%Y-%m-%d") for w in weeks]
    panel_px = 120  # pixels per row; figure height grows linearly
    pfig = make_subplots(
        rows=n, cols=1, shared_xaxes=True,
        vertical_spacing=min(0.04, 1.0 / max(n * 4, 4)),
        row_titles=week_labels,
    )
    for i, week in enumerate(weeks, start=1):
        sub = long_df[long_df["week"] == week].set_index("bin_label")
        vals = sub["n_humans"].reindex(_HIST_BIN_LABELS, fill_value=0).values
        pfig.add_trace(
            go.Bar(
                x=_HIST_BIN_LABELS,
                y=vals.tolist(),
                marker_color="steelblue",
                hovertemplate=(
                    week.strftime("%Y-%m-%d")
                    + "<br>%{x}: %{y}<extra></extra>"
                ),
                showlegend=False,
            ),
            row=i, col=1,
        )
    pfig.update_layout(
        height=max(240, panel_px * n),
        template="plotly_white",
        margin=dict(l=80, r=80, t=40, b=60),
    )
    pfig.update_xaxes(
        title_text="issues filed by one human author that week",
        row=n, col=1,
    )
    # Rotate the row titles to be readable on the left edge, matching
    # the matplotlib y-axis-label convention.
    for ann in pfig.layout.annotations:
        ann.update(textangle=0, xanchor="right", x=-0.01, font=dict(size=10))
    write_html_with_title(pfig, out_html, tab_title, page_heading=title)
    log.info("wrote %s", out_html)


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def run_for_repo(
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    output_dir: Path,
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

    plots_dir = output_dir / "plots" / safe_slug
    html_dir = output_dir / "html" / safe_slug
    csv_dir = output_dir / "csv" / safe_slug

    def _emit(
        stem: str, df: pd.DataFrame, ts_col: str,
        title: str, tab_title: str,
    ) -> None:
        daily = _daily_counts(df, ts_col, "credential")
        _save_csv(daily, csv_dir / f"{stem}.csv")
        _plot_stacked(
            daily, title,
            plots_dir / f"{stem}.png",
            html_dir / f"{stem}.html",
            tab_title,
        )

    repo_slug = f"{owner}/{name}"

    log.info("[%s] graph 1: issue authorship over time", repo_slug)
    _emit(
        "issues_opened_by_credential",
        _issues_authored(conn, repo_id), "created_at",
        f"{repo_slug} -- issues opened per day, by author credential",
        f"Issues opened ({name})",
    )

    log.info("[%s] graph 2: issue closer over time", repo_slug)
    _emit(
        "issues_closed_by_credential",
        _issues_closed(conn, repo_id), "closed_at",
        f"{repo_slug} -- issues closed per day, by closer credential",
        f"Issues closed ({name})",
    )

    log.info("[%s] graph 3: PR authorship over time", repo_slug)
    _emit(
        "prs_opened_by_credential",
        _prs_authored(conn, repo_id), "created_at",
        f"{repo_slug} -- PRs opened per day, by author credential",
        f"PRs opened ({name})",
    )

    log.info("[%s] graph 4: PR merger over time", repo_slug)
    _emit(
        "prs_merged_by_credential",
        _prs_merged(conn, repo_id), "merged_at",
        f"{repo_slug} -- PRs merged per day, by merger credential",
        f"PRs merged ({name})",
    )

    log.info("[%s] graph 5: comments on issues over time", repo_slug)
    _emit(
        "issue_comments_by_credential",
        _comments_on(conn, repo_id, is_pr=False), "created_at",
        f"{repo_slug} -- comments on issues per day, by commenter credential",
        f"Issue comments ({name})",
    )

    log.info("[%s] graph 6: comments on PRs over time", repo_slug)
    _emit(
        "pr_comments_by_credential",
        _comments_on(conn, repo_id, is_pr=True), "created_at",
        f"{repo_slug} -- comments on PRs per day, by commenter credential",
        f"PR comments ({name})",
    )

    log.info(
        "[%s] graph 7: merged-PR close-count distribution over time",
        repo_slug,
    )
    pr_close = _merged_pr_close_counts(conn, repo_id)
    pr_close_weekly = _weekly_pr_close_distribution(pr_close)
    if not pr_close_weekly.empty:
        pr_close_weekly.to_csv(
            csv_dir / "merged_pr_close_count_distribution.csv"
        )
        _plot_pr_close_distribution(
            pr_close_weekly,
            title=(
                f"{repo_slug} -- merged PRs created per week, "
                f"stacked by issues-closed bucket"
            ),
            out_png=plots_dir / "merged_pr_close_count_distribution.png",
            out_html=html_dir / "merged_pr_close_count_distribution.html",
            tab_title=f"Merged-PR close-count distribution ({name})",
        )

    log.info(
        "[%s] graph 8: human-author issues-per-week histograms (L5+)",
        repo_slug,
    )
    per_author = _human_issue_counts_per_week(conn, repo_id)
    long_df = _human_issue_distribution_long(per_author)
    if not long_df.empty:
        long_df.to_csv(
            csv_dir / "human_issue_distribution.csv", index=False,
        )
        _plot_human_issue_distribution(
            long_df,
            title=(
                f"{repo_slug} -- issues filed per human author per week, "
                f"L5 onward"
            ),
            out_png=plots_dir / "human_issue_distribution.png",
            out_html=html_dir / "human_issue_distribution.html",
            tab_title=f"Human author issue distribution ({name})",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="First-look analysis")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--repo",
        action="append",
        help="restrict to one repo (owner/name); may be repeated",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    cfg = load_config(args.config)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    selected = set(args.repo) if args.repo else None
    targets = [
        r for r in cfg.repos
        if r.role in ("subject", "both")
        and (selected is None or r.slug in selected)
    ]
    if not targets:
        log.error("no subject repos configured")
        return 2

    conn = connect_readonly(cfg.db_path)
    try:
        for r in targets:
            run_for_repo(conn, r.owner, r.name, cfg.output_dir)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
