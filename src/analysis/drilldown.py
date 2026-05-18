# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Drill-down follow-ups to ``first_look``.

The ``first_look`` plots showed three sharp findings:

* PR creation, PR merge, and issue closure all flip from
  nearly-all-human to nearly-all-bot at roughly the same date.
* Issue creation does NOT flip the same way; bot share rises gradually
  starting in early April and reaches roughly even later.
* Bot activity on the issue-creation side predates the broader
  transition by about a month.

This module produces follow-up artifacts to refine those findings:

1. Bot-opened-issue producers: which bot accounts file issues, and
   when did each become active. Surfaces auto-QA, link-checker, etc.
2. Post-cutoff human PR authors: who, if anyone, still opens PRs
   under a human credential after the transition.
3. PRs around the transition: a small table of PRs from a window
   before and after the apparent inflection date, showing author and
   merger.

The transition date is configurable via ``--cutoff`` (default
2026-05-03). The cutoff is informally derived from a pixellated
plot reading; the drill-down output makes it easy to refine.
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
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402

from ..common.config import load_config
from ..common.db import connect_readonly
from ._plotly_html import write_html_with_title


log = logging.getLogger(__name__)


DEFAULT_CUTOFF = "2026-05-03"
WINDOW_DAYS = 5  # for the around-the-transition table


# ----------------------------------------------------------------------
# 1. Bot-opened-issue producers
# ----------------------------------------------------------------------

def bot_issue_producers(
    conn: sqlite3.Connection,
    repo_id: int,
    output_dir: Path,
    safe_slug: str,
    repo_name: str,
) -> None:
    """Group bot-opened issues by author login.

    Output:
    - CSV table of (login, total_count, first_seen, last_seen)
    - HTML stacked-area plot of per-login daily counts
    """
    rows = conn.execute(
        """
        SELECT i.created_at, a.login AS author_login
        FROM issue i
        JOIN actor a ON a.actor_id = i.author_id
        WHERE i.repo_id = ?
          AND i.is_pr = 0
          AND a.is_bot_login = 1
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        log.warning("no bot-opened issues in %s", safe_slug)
        return

    df["created_at"] = pd.to_datetime(df["created_at"], utc=True, errors="coerce")
    df = df.dropna(subset=["created_at"])

    # Summary table per login
    summary = (
        df.groupby("author_login")
          .agg(total=("created_at", "size"),
               first_seen=("created_at", "min"),
               last_seen=("created_at", "max"))
          .sort_values("total", ascending=False)
    )
    csv_path = output_dir / "csv" / safe_slug / "bot_issue_producers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(csv_path)
    log.info("wrote %s", csv_path)

    # Daily counts per login, stacked-area HTML plot
    df["date"] = df["created_at"].dt.tz_convert("UTC").dt.floor("D")
    daily = (
        df.groupby(["date", "author_login"])
          .size()
          .unstack(fill_value=0)
          .sort_index()
    )
    # Order columns by total volume desc so the heaviest contributor is at bottom
    col_order = summary.index.tolist()
    daily = daily[[c for c in col_order if c in daily.columns]]

    html_path = output_dir / "html" / safe_slug / "bot_issue_producers.html"
    html_path.parent.mkdir(parents=True, exist_ok=True)
    fig = go.Figure()
    for col in daily.columns:
        fig.add_trace(go.Scatter(
            x=daily.index, y=daily[col], name=col, mode="lines",
            stackgroup="one",
            line=dict(width=0.5),
            hovertemplate=(
                "<b>%{x|%Y-%m-%d}</b><br>" + col + ": %{y}<extra></extra>"
            ),
        ))
    fig.update_layout(
        title="Bot-opened issues per day, by author login",
        xaxis_title="date (UTC)",
        yaxis_title="count per day",
        hovermode="x unified",
        template="plotly_white",
    )
    write_html_with_title(
        fig, html_path, f"Bot issue producers ({repo_name})",
    )
    log.info("wrote %s", html_path)


# ----------------------------------------------------------------------
# 2. Post-cutoff human PR authors
# ----------------------------------------------------------------------

def post_cutoff_human_pr_authors(
    conn: sqlite3.Connection,
    repo_id: int,
    cutoff: str,
    output_dir: Path,
    safe_slug: str,
) -> None:
    """Who, if anyone, still opens PRs with a human credential after the
    cutoff date.
    """
    rows = conn.execute(
        """
        SELECT i.number, i.created_at, i.title, a.login AS author_login
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.author_id
        WHERE i.repo_id = ?
          AND i.is_pr = 1
          AND i.created_at >= ?
          AND (a.is_bot_login = 0 OR a.is_bot_login IS NULL)
        ORDER BY i.created_at ASC
        """,
        (repo_id, cutoff),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    csv_path = output_dir / "csv" / safe_slug / f"post_{cutoff}_human_pr_authors.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    log.info("wrote %s (%d rows)", csv_path, len(df))

    if not df.empty:
        # Per-author summary
        summary = (
            df.groupby("author_login")
              .agg(total=("number", "size"),
                   first_pr=("created_at", "min"),
                   last_pr=("created_at", "max"))
              .sort_values("total", ascending=False)
        )
        summary_path = output_dir / "csv" / safe_slug / f"post_{cutoff}_human_pr_authors_summary.csv"
        summary.to_csv(summary_path)
        log.info("wrote %s", summary_path)


# ----------------------------------------------------------------------
# 3. PRs around the transition
# ----------------------------------------------------------------------

def prs_around_transition(
    conn: sqlite3.Connection,
    repo_id: int,
    cutoff: str,
    window_days: int,
    output_dir: Path,
    safe_slug: str,
) -> None:
    """Sample PRs from a window of ``window_days`` before and after the
    cutoff, with author and merger logins.
    """
    cutoff_ts = pd.to_datetime(cutoff, utc=True)
    window = pd.Timedelta(days=window_days)
    start = (cutoff_ts - window).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (cutoff_ts + window).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = conn.execute(
        """
        SELECT i.number, i.created_at, pr.merged_at,
               a.login AS author_login,
               m.login AS merger_login,
               pr.merged
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.author_id
        LEFT JOIN pull_request pr ON pr.issue_id = i.issue_id
        LEFT JOIN actor m ON m.actor_id = pr.merged_by_id
        WHERE i.repo_id = ?
          AND i.is_pr = 1
          AND i.created_at >= ?
          AND i.created_at < ?
        ORDER BY i.created_at ASC
        """,
        (repo_id, start, end),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    csv_path = (
        output_dir / "csv" / safe_slug
        / f"prs_around_{cutoff}_window_{window_days}d.csv"
    )
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    log.info("wrote %s (%d rows)", csv_path, len(df))


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def run_for_repo(
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    output_dir: Path,
    cutoff: str,
    window_days: int,
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

    log.info("[%s/%s] (1) bot issue producers", owner, name)
    bot_issue_producers(conn, repo_id, output_dir, safe_slug, name)

    log.info("[%s/%s] (2) post-%s human PR authors", owner, name, cutoff)
    post_cutoff_human_pr_authors(conn, repo_id, cutoff, output_dir, safe_slug)

    log.info("[%s/%s] (3) PRs around %s (+/- %dd)", owner, name, cutoff, window_days)
    prs_around_transition(conn, repo_id, cutoff, window_days, output_dir, safe_slug)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Drill-down analysis")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--cutoff",
        default=DEFAULT_CUTOFF,
        help=(
            f"Apparent transition date (YYYY-MM-DD). Default {DEFAULT_CUTOFF}. "
            "Used to bound 'post-cutoff human PR authors' and the "
            "around-the-transition window."
        ),
    )
    parser.add_argument(
        "--window-days", type=int, default=WINDOW_DAYS,
        help=f"Half-width in days of the around-the-transition window (default {WINDOW_DAYS}).",
    )
    parser.add_argument(
        "--repo", action="append",
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
            run_for_repo(
                conn, r.owner, r.name, cfg.output_dir,
                cutoff=args.cutoff, window_days=args.window_days,
            )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
