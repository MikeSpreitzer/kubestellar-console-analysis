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
import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402

from ..common.config import load_config
from ..common.db import connect_readonly
from ._plotly_html import write_html_with_title


log = logging.getLogger(__name__)


CREDENTIAL_HUMAN = "human-credentialed"
CREDENTIAL_BOT = "bot-credentialed"
CREDENTIAL_UNKNOWN = "unknown"


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
    if login is None:
        return CREDENTIAL_UNKNOWN
    if login.endswith("[bot]"):
        return CREDENTIAL_BOT
    return CREDENTIAL_HUMAN


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
        title=title,
        xaxis_title="date (UTC)",
        yaxis_title="count per day",
        hovermode="x unified",
        template="plotly_white",
    )
    write_html_with_title(plotly_fig, out_path_html, tab_title)
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
