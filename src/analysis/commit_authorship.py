# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Commit-authorship analysis.

Three artifacts:

1. Per-commit credential mix over time, daily-binned. Parallels
   first_look's PR/issue plots but at commit granularity. PNG + HTML
   + CSV per repo.

2. PR-vs-commit-author cross-tab. For each PR, lists the PR's author
   credential, the merger credential, and the set of distinct commit
   authors inside the PR's merge commit. Surfaces the case of a bot-
   opened PR whose commits were authored by a human (e.g. Andy using
   Claude Code locally and pushing commits, with a bot opening the
   PR).

3. Per-bot-email producer plot. Daily counts of commits authored under
   each known bot email (currently copilot@github.com,
   scanner@kubestellar.io, reviewer@claude-dev.local). Surfaces when
   each automation came online and how much of the commit stream each
   accounts for.

Credential classification combines two signals:
- ``author_login`` ending in ``[bot]`` (derived from GitHub noreply
  emails by the git extractor).
- ``author_email`` matching a known bot-email allowlist.

This is still a credential classification, not a producer
classification. Human-credentialed commits remain an upper bound on
actual hand-typed work; tools that commit under a developer's real
email are not detectable from email alone.
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


log = logging.getLogger(__name__)


# Credential classes used in this module's outputs. Same names as
# first_look so plots have consistent legends across modules.
CREDENTIAL_HUMAN = "human-credentialed"
CREDENTIAL_BOT = "bot-credentialed"
CREDENTIAL_UNKNOWN = "unknown"

COLORS = {
    CREDENTIAL_HUMAN: "#1f77b4",
    CREDENTIAL_BOT: "#d62728",
    CREDENTIAL_UNKNOWN: "#7f7f7f",
}

# Known bot-author emails. The list is grounded in the actual data:
# these are the emails we observed authoring commits that were not
# already classified as bot via the noreply-login path. Maintain
# additively as new automation appears.
BOT_AUTHOR_EMAILS = frozenset({
    "copilot@github.com",
    "scanner@kubestellar.io",
    "reviewer@claude-dev.local",
})


def _classify_commit(login: Optional[str], email: Optional[str]) -> str:
    # Coerce pandas NaN (a float) and any other non-string null value
    # to None. SQLite stores NULL; pandas reads it back as NaN by
    # default for object-dtype columns. The downstream str checks
    # below assume real strings or None.
    if not isinstance(login, str):
        login = None
    if not isinstance(email, str):
        email = None
    if login and login.endswith("[bot]"):
        return CREDENTIAL_BOT
    if email and email in BOT_AUTHOR_EMAILS:
        return CREDENTIAL_BOT
    if login or email:
        return CREDENTIAL_HUMAN
    return CREDENTIAL_UNKNOWN


# ----------------------------------------------------------------------
# Plot helpers (same convention as first_look)
# ----------------------------------------------------------------------

def _daily_counts(
    df: pd.DataFrame,
    timestamp_col: str,
    classification_col: str,
) -> pd.DataFrame:
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
    cols_in_pref = [
        c for c in [CREDENTIAL_HUMAN, CREDENTIAL_BOT, CREDENTIAL_UNKNOWN]
        if c in grouped.columns
    ]
    return grouped[cols_in_pref]


def _save_csv(daily: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    daily.to_csv(out_path)
    log.info("wrote %s", out_path)


def _plot_stacked(
    daily: pd.DataFrame,
    title: str,
    out_path_png: Path,
    out_path_html: Path,
    color_map: Optional[dict[str, str]] = None,
) -> None:
    if daily.empty:
        log.warning("no data to plot for %s", title)
        return
    out_path_png.parent.mkdir(parents=True, exist_ok=True)
    out_path_html.parent.mkdir(parents=True, exist_ok=True)

    cmap = color_map or COLORS
    colors = [cmap.get(c, "#999999") for c in daily.columns]

    # PNG
    fig, ax = plt.subplots(figsize=(12, 5))
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

    # HTML
    fig = go.Figure()
    for col in daily.columns:
        color = cmap.get(col, "#999999")
        fig.add_trace(go.Scatter(
            x=daily.index, y=daily[col], name=col, mode="lines",
            stackgroup="one",
            line=dict(width=0.5, color=color),
            fillcolor=color,
            hovertemplate=(
                "<b>%{x|%Y-%m-%d}</b><br>" + col + ": %{y}<extra></extra>"
            ),
        ))
    fig.update_layout(
        title=title,
        xaxis_title="date (UTC)",
        yaxis_title="count per day",
        hovermode="x unified",
        template="plotly_white",
    )
    fig.write_html(str(out_path_html), include_plotlyjs=True, full_html=True)
    log.info("wrote %s", out_path_html)


# ----------------------------------------------------------------------
# 1. Per-commit credential mix over time
# ----------------------------------------------------------------------

def commits_by_credential(
    conn: sqlite3.Connection,
    repo_id: int,
    output_dir: Path,
    safe_slug: str,
) -> None:
    rows = conn.execute(
        """
        SELECT authored_at, author_login, author_email
        FROM commit_
        WHERE repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        log.warning("no commits for %s", safe_slug)
        return
    df["credential"] = df.apply(
        lambda r: _classify_commit(r["author_login"], r["author_email"]),
        axis=1,
    )
    daily = _daily_counts(df, "authored_at", "credential")
    _save_csv(
        daily,
        output_dir / "csv" / safe_slug / "commits_by_credential.csv",
    )
    _plot_stacked(
        daily,
        f"{safe_slug} -- commits authored per day, by author credential",
        output_dir / "plots" / safe_slug / "commits_by_credential.png",
        output_dir / "html" / safe_slug / "commits_by_credential.html",
    )


# ----------------------------------------------------------------------
# 2. PR-vs-commit-author cross-tab
# ----------------------------------------------------------------------

def pr_vs_commit_authors(
    conn: sqlite3.Connection,
    repo_id: int,
    output_dir: Path,
    safe_slug: str,
) -> None:
    """For each merged PR with a known merge_commit_sha, list the PR's
    author and merger credentials and the set of commit-author
    credentials inside the PR.

    Methodology note: we identify the commits "in" a PR by walking
    from the merge_commit_sha back through ancestry. Without first-
    parent semantics this could over-attribute commits to the PR (it
    would pick up everything in the PR's history). For a feature-branch
    workflow that's typically OK. This is a heuristic; we treat it as
    such and surface the cross-tab as an exploratory artifact, not an
    authoritative measurement.

    Implementation: for simplicity, we approximate with the merge
    commit's *immediate* parent set: the merge commit's parents are
    typically [base_branch_tip, feature_branch_tip], so the
    feature_branch_tip's first-parent ancestry up to the merge base
    captures the PR's commits. We don't do that walk here; instead we
    simply list the merge commit's authorship (one row per merged PR,
    with the merge-commit author as a coarse proxy for "what produced
    these changes"). This is enough to surface the headline pattern --
    bot-merged PR with human commit-author -- without doing a full
    git-walk to enumerate every commit.

    A more thorough version that walks the PR's full commit set is a
    future refinement.
    """
    rows = conn.execute(
        """
        SELECT
            i.number AS pr_number,
            i.created_at AS pr_created_at,
            pr.merged_at,
            pr.merge_commit_sha,
            ai.login AS pr_author_login,
            am.login AS merger_login,
            c.author_login AS commit_author_login,
            c.author_email AS commit_author_email,
            c.author_name AS commit_author_name
        FROM issue i
        JOIN pull_request pr ON pr.issue_id = i.issue_id
        LEFT JOIN actor ai ON ai.actor_id = i.author_id
        LEFT JOIN actor am ON am.actor_id = pr.merged_by_id
        LEFT JOIN commit_ c
               ON c.repo_id = i.repo_id
              AND c.sha = pr.merge_commit_sha
        WHERE i.repo_id = ?
          AND pr.merged = 1
          AND pr.merge_commit_sha IS NOT NULL
        ORDER BY pr.merged_at ASC
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        log.warning("no merged PRs with merge_commit_sha for %s", safe_slug)
        return

    def _classify_login(x) -> str:
        if not isinstance(x, str) or not x:
            return CREDENTIAL_UNKNOWN
        if x.endswith("[bot]"):
            return CREDENTIAL_BOT
        return CREDENTIAL_HUMAN

    df["pr_author_credential"] = df["pr_author_login"].apply(_classify_login)
    df["merger_credential"] = df["merger_login"].apply(_classify_login)
    df["commit_author_credential"] = df.apply(
        lambda r: _classify_commit(r["commit_author_login"], r["commit_author_email"]),
        axis=1,
    )

    # Full per-PR table, useful for sampling
    csv_path = output_dir / "csv" / safe_slug / "pr_vs_commit_author.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    log.info("wrote %s (%d rows)", csv_path, len(df))

    # Cross-tab summary: counts by (pr_author_credential, commit_author_credential)
    summary = (
        df.groupby(["pr_author_credential", "commit_author_credential"])
          .size()
          .unstack(fill_value=0)
    )
    summary_path = output_dir / "csv" / safe_slug / "pr_vs_commit_author_crosstab.csv"
    summary.to_csv(summary_path)
    log.info("wrote %s", summary_path)

    # The interesting cell: PR-author=bot, commit-author=human.
    interesting = df[
        (df["pr_author_credential"] == CREDENTIAL_BOT)
        & (df["commit_author_credential"] == CREDENTIAL_HUMAN)
    ]
    interesting_path = (
        output_dir / "csv" / safe_slug / "pr_bot_authored_human_commits.csv"
    )
    interesting[[
        "pr_number", "pr_created_at", "merged_at",
        "pr_author_login", "merger_login",
        "commit_author_login", "commit_author_email", "commit_author_name",
    ]].to_csv(interesting_path, index=False)
    log.info("wrote %s (%d rows)", interesting_path, len(interesting))


# ----------------------------------------------------------------------
# 3. Per-bot-email producer plot
# ----------------------------------------------------------------------

def bot_email_producers(
    conn: sqlite3.Connection,
    repo_id: int,
    output_dir: Path,
    safe_slug: str,
) -> None:
    """Daily counts per bot email + per [bot]-suffixed login. Same
    shape as drilldown.bot_issue_producers but for commits.
    """
    rows = conn.execute(
        """
        SELECT authored_at, author_login, author_email
        FROM commit_
        WHERE repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return

    def _producer_key(row) -> Optional[str]:
        login = row["author_login"]
        email = row["author_email"]
        if not isinstance(login, str):
            login = None
        if not isinstance(email, str):
            email = None
        if login and login.endswith("[bot]"):
            return login
        if email and email in BOT_AUTHOR_EMAILS:
            return email
        return None

    df["producer"] = df.apply(_producer_key, axis=1)
    df = df.dropna(subset=["producer"])
    if df.empty:
        log.info("no bot-authored commits in %s", safe_slug)
        return

    df["authored_at"] = pd.to_datetime(df["authored_at"], utc=True, errors="coerce")
    df = df.dropna(subset=["authored_at"])

    # Per-producer summary
    summary = (
        df.groupby("producer")
          .agg(total=("authored_at", "size"),
               first_seen=("authored_at", "min"),
               last_seen=("authored_at", "max"))
          .sort_values("total", ascending=False)
    )
    csv_path = output_dir / "csv" / safe_slug / "bot_commit_producers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    summary.to_csv(csv_path)
    log.info("wrote %s", csv_path)

    # Daily stacked HTML
    df["date"] = df["authored_at"].dt.tz_convert("UTC").dt.floor("D")
    daily = (
        df.groupby(["date", "producer"])
          .size()
          .unstack(fill_value=0)
          .sort_index()
    )
    col_order = summary.index.tolist()
    daily = daily[[c for c in col_order if c in daily.columns]]

    html_path = output_dir / "html" / safe_slug / "bot_commit_producers.html"
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
        title="Bot-authored commits per day, by producer (login or email)",
        xaxis_title="date (UTC)",
        yaxis_title="count per day",
        hovermode="x unified",
        template="plotly_white",
    )
    fig.write_html(str(html_path), include_plotlyjs=True, full_html=True)
    log.info("wrote %s", html_path)


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

    log.info("[%s/%s] (1) commits by credential over time", owner, name)
    commits_by_credential(conn, repo_id, output_dir, safe_slug)

    log.info("[%s/%s] (2) PR vs commit-author cross-tab", owner, name)
    pr_vs_commit_authors(conn, repo_id, output_dir, safe_slug)

    log.info("[%s/%s] (3) bot-email commit producers", owner, name)
    bot_email_producers(conn, repo_id, output_dir, safe_slug)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Commit-authorship analysis")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
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
        if (selected is None or r.slug in selected)
    ]
    if not targets:
        log.error("no repos configured")
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
