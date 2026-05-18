# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Commit-authorship analysis.

Five artifacts:

1. Per-commit credential mix over time, daily-binned. Parallels
   first_look's PR/issue plots but at commit granularity. PNG + HTML
   + CSV per repo.

2. PR-vs-commit-author cross-tab. For each PR, lists the PR's author
   credential, the merger credential, and the credential of the
   merge-commit's author. Note: the merge-commit author is a coarse
   proxy for "who produced the PR's changes" -- for squash merges it
   is the merger, not the feature-branch authors. Treated as
   exploratory, not authoritative.

3. Per-bot-email producer plot. Daily counts of commits authored under
   each known bot login or bot email. Surfaces when each automation
   came online and how much of the commit stream each accounts for.

4. ``Co-Authored-By`` trailer enumeration. Every commit whose message
   contains at least one Co-Authored-By trailer, with the full trailer
   text. Lets us discover which tools and identities have left
   disclosed traces of AI-assisted commits.

5. Disclosed-AI-collaboration time series. Daily counts of commits
   whose message discloses at least one AI tool via Co-Authored-By,
   versus those that do not. The disclosed share is a *lower bound*
   on AI-assisted commits within the human-credentialed bucket;
   commits where the trailer was omitted or stripped are
   indistinguishable from hand-typed commits at the metadata level.

Credential classification combines two signals:
- ``author_login`` ending in ``[bot]`` (derived from GitHub noreply
  emails by the git extractor).
- ``author_email`` matching a known bot-email allowlist.

This is still a credential classification, not a producer
classification. Human-credentialed commits remain an upper bound on
actual hand-typed work; tools that commit under a developer's real
email (e.g. an agent running on a developer's workstation, with the
developer's git identity) are not detectable from email alone. The
``Co-Authored-By`` artifacts above produce a partial complementary
signal -- a lower bound on disclosed AI collaboration within the
human-credentialed bucket -- but cannot detect AI-assisted commits
that did not include a trailer.
"""

from __future__ import annotations

import argparse
import logging
import re
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

from ..classifier.record import Record
from ..classifier.rules import (
    CREDENTIAL_BOT,
    CREDENTIAL_HUMAN,
    CREDENTIAL_UNKNOWN,
    classify,
    credential_class_of,
)
from ..common.config import load_config
from ..common.db import connect_readonly
from ._plotly_html import write_html_with_title


log = logging.getLogger(__name__)


COLORS = {
    CREDENTIAL_HUMAN: "#1f77b4",
    CREDENTIAL_BOT: "#d62728",
    CREDENTIAL_UNKNOWN: "#7f7f7f",
}


# Regex for Co-Authored-By trailers. Git convention: one or more
# lines like ``Co-authored-by: Name <email>`` typically near the end
# of a commit message. Case-insensitive; allows both hyphen and space
# variants in case anyone deviates from the canonical form.
_COAUTHOR_RE = re.compile(
    r"^\s*co[-\s]authored[-\s]by\s*:\s*(?P<name>.+?)\s*<(?P<email>[^>]+)>\s*$",
    re.IGNORECASE | re.MULTILINE,
)


# Regex patterns that, when matched against a Co-Authored-By
# trailer's name or email (lowercased), indicate the trailer is
# disclosing an AI or automation tool.
#
# Maintained additively. Iterate by inspecting
# ``coauthored_by_summary.csv``: any trailer that should be flagged
# but isn't gets a pattern added here.
#
# We use word-boundary or suffix matching where the bare word would
# false-positive (``bot`` in ``robot``, ``agent`` in ``urgent``, etc.).
# Specific known identities (``ks-ci-bot``, ``kubestellar-hive``,
# ``scanner@kubestellar.io``) are listed explicitly.
_AI_TOOL_REGEXES = tuple(re.compile(p) for p in [
    # Specific tool/vendor names.
    r"\bclaude\b",          # Anthropic Claude
    r"\banthropic\b",       # noreply@anthropic.com
    r"\bcopilot\b",         # GitHub Copilot
    r"\bcursor\b",          # Cursor
    r"\bcody\b",            # Sourcegraph Cody
    r"\bcodeium\b",         # Codeium
    r"\baider\b",           # Aider
    # Bot/agent/assistant markers, bounded so we don't false-positive
    # on words like robot/agendas/assistance.
    r"\[bot\]",             # GitHub App login form
    r"\bbot\b",             # bare "Bot" word (e.g. "Auto-QA Bot")
    r"-bot\b",              # hyphen-suffix bots (e.g. "ks-ci-bot")
    r"\bagent\b",           # bare "Agent" word
    r"-agent\b",            # hyphen-suffix agents (e.g. "tester-agent")
    r"\bassistant\b",       # "AI Assistant"
    # Known kubestellar-org automation identities. Listed explicitly
    # because "kubestellar-hive" and "scanner@kubestellar.io" don't
    # match any of the generic patterns above.
    r"\bkubestellar-hive\b",
    r"\bkubestellar-bot\b",
    r"\bscanner@kubestellar\.io\b",
    r"\bauto-qa\b",
    r"\bgithub actions\b",  # the "GitHub Actions" actor name
    # Other known automation handles in this project. The trailer name
    # "Bob" alone would be too short and ambiguous as a generic pattern
    # (every "bobby" or "bob's email" would false-positive), so we
    # match a more specific form: the email "bob@example.com" with
    # which it has actually appeared.
    r"\bbob@example\.com\b",
])


def _trailer_names_ai_tool(name: str, email: str) -> bool:
    """Return True if a Co-Authored-By trailer's name/email suggests
    an AI or automation tool."""
    haystack = f"{name} {email}".lower()
    return any(p.search(haystack) for p in _AI_TOOL_REGEXES)


def _classify_commit(login: Optional[str], email: Optional[str]) -> str:
    """Coarse credential class for a commit, derived from the shared
    classifier rules.

    Pandas reads SQLite NULLs back as NaN for object-dtype columns;
    the classifier accepts only ``None`` or strings, so we coerce
    non-strings to ``None`` first.
    """
    if not isinstance(login, str):
        login = None
    if not isinstance(email, str):
        email = None
    rec = Record(
        target_kind="commit",
        target_id=0,
        author_login=login,
        author_email=email,
        author_name=None,
        created_at="",
    )
    verdict, _ = classify(rec)
    return credential_class_of(verdict.producer)


# ----------------------------------------------------------------------
# Plot helpers (same convention as first_look)
# ----------------------------------------------------------------------

def _daily_counts(
    df: pd.DataFrame,
    timestamp_col: str,
    classification_col: str,
    column_order: Optional[list[str]] = None,
) -> pd.DataFrame:
    """Group rows by UTC day of timestamp_col and classification_col.

    If ``column_order`` is given, output columns are placed in that
    order (omitting values not present in the data, never adding
    values not present in ``column_order``). If ``column_order`` is
    None, columns are returned in pandas' default order (which is
    alphabetical for unstacked groupby).
    """
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
    if column_order is None:
        return grouped
    cols_in_pref = [c for c in column_order if c in grouped.columns]
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
    tab_title: str,
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
    write_html_with_title(fig, out_path_html, tab_title)
    log.info("wrote %s", out_path_html)


# ----------------------------------------------------------------------
# 1. Per-commit credential mix over time
# ----------------------------------------------------------------------

def commits_by_credential(
    conn: sqlite3.Connection,
    repo_id: int,
    output_dir: Path,
    safe_slug: str,
    repo_name: str,
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
    daily = _daily_counts(
        df, "authored_at", "credential",
        column_order=[CREDENTIAL_HUMAN, CREDENTIAL_BOT, CREDENTIAL_UNKNOWN],
    )
    _save_csv(
        daily,
        output_dir / "csv" / safe_slug / "commits_by_credential.csv",
    )
    _plot_stacked(
        daily,
        f"{safe_slug} -- commits authored per day, by author credential",
        output_dir / "plots" / safe_slug / "commits_by_credential.png",
        output_dir / "html" / safe_slug / "commits_by_credential.html",
        tab_title=f"Commits ({repo_name})",
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
    repo_name: str,
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
        """Identify the bot producer of this commit by login or email.

        Runs the shared classifier and returns the verdict's
        ``sub_producer`` (which carries the matched login or email)
        when the resulting credential class is ``bot-credentialed``.
        Returns None for human-credentialed or unknown rows so they
        are excluded from this plot.
        """
        login = row["author_login"]
        email = row["author_email"]
        if not isinstance(login, str):
            login = None
        if not isinstance(email, str):
            email = None
        rec = Record(
            target_kind="commit",
            target_id=0,
            author_login=login,
            author_email=email,
            author_name=None,
            created_at="",
        )
        verdict, _ = classify(rec)
        if credential_class_of(verdict.producer) != CREDENTIAL_BOT:
            return None
        return verdict.sub_producer or verdict.producer

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
    write_html_with_title(
        fig, html_path, f"Bot commit producers ({repo_name})",
    )
    log.info("wrote %s", html_path)


# ----------------------------------------------------------------------
# 4. Co-Authored-By trailer enumeration
# ----------------------------------------------------------------------

def coauthored_by_trailers(
    conn: sqlite3.Connection,
    repo_id: int,
    output_dir: Path,
    safe_slug: str,
) -> None:
    """Scan commit messages for Co-Authored-By trailers, write one row
    per (commit, trailer) to a CSV. Multiple trailers on a single
    commit produce multiple rows.
    """
    rows = conn.execute(
        """
        SELECT sha, authored_at, author_email, message
        FROM commit_
        WHERE repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    out_rows: list[dict] = []
    for r in rows:
        msg = r["message"] or ""
        for m in _COAUTHOR_RE.finditer(msg):
            name = (m.group("name") or "").strip()
            email = (m.group("email") or "").strip()
            out_rows.append({
                "sha": r["sha"],
                "authored_at": r["authored_at"],
                "author_email": r["author_email"],
                "trailer_name": name,
                "trailer_email": email,
                "is_ai_tool": _trailer_names_ai_tool(name, email),
            })
    df = pd.DataFrame(out_rows)
    csv_path = output_dir / "csv" / safe_slug / "coauthored_by_trailers.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(csv_path, index=False)
    log.info("wrote %s (%d trailer rows from commits)", csv_path, len(df))

    if df.empty:
        return

    # Per-trailer-identity summary, sorted by frequency.
    summary = (
        df.groupby(["trailer_email", "trailer_name", "is_ai_tool"])
          .size().reset_index(name="total")
          .sort_values("total", ascending=False)
    )
    summary_path = output_dir / "csv" / safe_slug / "coauthored_by_summary.csv"
    summary.to_csv(summary_path, index=False)
    log.info("wrote %s (%d distinct trailer identities)", summary_path, len(summary))


# ----------------------------------------------------------------------
# 5. Disclosed AI-collaboration time series
# ----------------------------------------------------------------------

DISCLOSURE_AI = "discloses-ai-coauthor"
DISCLOSURE_NONE = "no-ai-coauthor"

DISCLOSURE_COLORS = {
    DISCLOSURE_AI: "#d62728",     # same red as bot-credentialed
    DISCLOSURE_NONE: "#1f77b4",   # same blue as human-credentialed
}


def disclosed_ai_collaboration(
    conn: sqlite3.Connection,
    repo_id: int,
    output_dir: Path,
    safe_slug: str,
    repo_name: str,
) -> None:
    """Daily count of commits split by whether the message discloses
    an AI tool via a Co-Authored-By trailer.

    Notes:
    - This counts each commit at most once. A commit with multiple
      Co-Authored-By trailers, at least one naming an AI tool, counts
      as ``DISCLOSURE_AI``.
    - The disclosed share is a *lower bound* on AI-assisted commits.
      Commits whose tool involvement was undisclosed (trailer omitted
      or stripped) appear in ``DISCLOSURE_NONE`` indistinguishably
      from hand-typed commits.
    """
    rows = conn.execute(
        """
        SELECT authored_at, message
        FROM commit_
        WHERE repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    if not rows:
        return

    records = []
    for r in rows:
        msg = r["message"] or ""
        discloses = False
        for m in _COAUTHOR_RE.finditer(msg):
            name = (m.group("name") or "").strip()
            email = (m.group("email") or "").strip()
            if _trailer_names_ai_tool(name, email):
                discloses = True
                break
        records.append({
            "authored_at": r["authored_at"],
            "disclosure": DISCLOSURE_AI if discloses else DISCLOSURE_NONE,
        })
    df = pd.DataFrame(records)
    daily = _daily_counts(
        df, "authored_at", "disclosure",
        column_order=[DISCLOSURE_NONE, DISCLOSURE_AI],
    )

    _save_csv(
        daily,
        output_dir / "csv" / safe_slug / "disclosed_ai_collaboration.csv",
    )
    _plot_stacked(
        daily,
        f"{safe_slug} -- commits per day, by AI-tool disclosure in Co-Authored-By",
        output_dir / "plots" / safe_slug / "disclosed_ai_collaboration.png",
        output_dir / "html" / safe_slug / "disclosed_ai_collaboration.html",
        tab_title=f"AI co-author disclosure ({repo_name})",
        color_map=DISCLOSURE_COLORS,
    )


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
    commits_by_credential(conn, repo_id, output_dir, safe_slug, name)

    log.info("[%s/%s] (2) PR vs commit-author cross-tab", owner, name)
    pr_vs_commit_authors(conn, repo_id, output_dir, safe_slug)

    log.info("[%s/%s] (3) bot-email commit producers", owner, name)
    bot_email_producers(conn, repo_id, output_dir, safe_slug, name)

    log.info("[%s/%s] (4) Co-Authored-By trailer enumeration", owner, name)
    coauthored_by_trailers(conn, repo_id, output_dir, safe_slug)

    log.info("[%s/%s] (5) disclosed-AI-collaboration time series", owner, name)
    disclosed_ai_collaboration(conn, repo_id, output_dir, safe_slug, name)


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
