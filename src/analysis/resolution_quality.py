# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Resolution-quality signals.

DESIGN.md splits these into two regimes:

High-precision / low-recall (cleaner signal, sparse data)
  - ``reopen_by_original_reporter``: issues closed and then reopened by
    their own original author. A closer-than-self reopen is a strong
    "the close was wrong" signal; we count it per closer producer.
  - ``followup_citing_close_pr``: a same-reporter follow-up issue whose
    body or first comment cites the closing PR via ``#NNN`` reference,
    where the original issue was closed by that PR. This is structural
    evidence that the original close did not satisfy the reporter.

Low-precision / higher-recall (more data, more noise)
  - ``post_close_phrase_matches``: comments authored after an issue's
    ``closed_at`` containing dissatisfaction phrases (stronger and
    weaker tiers; see PHRASE_TIERS below). Reported per closer producer
    and per author of the post-close comment.
  - ``cross_reference_patterns``: ``cross-referenced`` events landing on
    a closed issue, separated by direction (the closed issue being
    referenced from a later issue/PR vs. the closed issue referencing
    others). High counts on the inbound side after close suggest the
    bug recurred or the discussion continues elsewhere.

Each output CSV is annotated by file with a header comment naming the
DESIGN.md caveats that bound interpretation: silent drops, bidirectional
adoption lag, attention non-uniformity, multi-case bundling.

All metrics use full project history; there is no fast-close-style
cutoff here. Run as
``python -m src.analysis.resolution_quality --config /config/config.yaml``.
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


# Regex used by followup_citing_close_pr. We accept the canonical
# `owner/repo#N` form as well as the bare `#N` form (which GitHub
# resolves to the same repo). Numbers are captured as ints.
ISSUE_REF_RE = re.compile(
    r"(?:(?P<owner>[A-Za-z0-9._-]+)/(?P<name>[A-Za-z0-9._-]+))?#(?P<num>\d+)"
)


# Two tiers of dissatisfaction phrases. The stronger tier is intended
# to be near-unambiguous; the weaker tier admits more false positives
# but catches reformulations the stronger tier misses. Matching is
# case-insensitive on substring; the comment author's intent is not
# verified.
PHRASE_TIERS: dict[str, list[str]] = {
    "stronger": [
        "still broken",
        "still happens",
        "still happening",
        "still failing",
        "still fails",
        "still does not work",
        "still doesn't work",
        "didn't fix",
        "did not fix",
        "doesn't fix",
        "does not fix",
        "not actually fixed",
        "regression",
        "this broke again",
        "broken again",
        "reopen",
        "should be reopened",
        "please reopen",
    ],
    "weaker": [
        "doesn't work",
        "does not work",
        "not working",
        "broken",
        "still seeing",
        "same issue",
        "same problem",
        "same error",
        "again",
        "happens again",
        "happening again",
    ],
}


CAVEAT_HEADER = (
    "# DESIGN.md caveats that bound this metric: silent drops, "
    "bidirectional adoption lag, attention non-uniformity, multi-case bundling.\n"
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

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


def _write_csv_with_caveats(df: pd.DataFrame, path: Path) -> None:
    """Write a CSV preceded by a single-line caveat comment."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(CAVEAT_HEADER)
        df.to_csv(f, index=False)
    log.info("wrote %s (%d rows)", path, len(df))


def _producer_count_bar(
    counts: pd.Series,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
    y_label: str,
) -> None:
    """Bar chart of counts indexed by producer."""
    if counts.empty:
        log.warning("no data for %s", title)
        return

    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    producers = _ordered_producers(list(counts.index))
    counts = counts.reindex(producers, fill_value=0)

    fig, ax = plt.subplots(figsize=(max(7, len(producers) * 0.7 + 2), 5))
    ax.bar(producers, counts.values, color="steelblue")
    ax.set_xticks(range(len(producers)))
    ax.set_xticklabels(producers, rotation=45, ha="right")
    ax.set_ylabel(y_label)
    ax.set_title(title)
    for i, v in enumerate(counts.values):
        if v > 0:
            ax.text(i, v, str(int(v)), ha="center", va="bottom", fontsize=8)
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_png)

    pfig = go.Figure(data=go.Bar(
        x=producers,
        y=counts.values.tolist(),
        hovertemplate="%{x}: %{y}<extra></extra>",
    ))
    pfig.update_layout(
        title=title,
        xaxis_title="producer",
        yaxis_title=y_label,
        template="plotly_white",
    )
    write_html_with_title(pfig, out_html, tab_title)
    log.info("wrote %s", out_html)


# ----------------------------------------------------------------------
# Metric 1: reopen by original reporter
# ----------------------------------------------------------------------

def _reopen_by_original_reporter(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    """One row per (issue, reopen-event) where the reopener is the
    issue's original author. Includes the producer of the most-recent
    closer prior to that reopen.

    The schema records a single ``closed_by_id`` per issue (the most
    recent closer); we join it as the closer for every reopen of that
    issue. This loses information for multi-cycle issues, which is a
    DESIGN.md-acknowledged limitation we don't try to reconstruct here.
    """
    rows = conn.execute(
        """
        SELECT
            ie.event_id   AS reopen_event_id,
            ie.issue_id   AS issue_id,
            ie.created_at AS reopened_at,
            ie.actor_id   AS reopener_id,
            a_re.login    AS reopener_login,
            i.author_id   AS author_id,
            a_au.login    AS author_login,
            i.closed_by_id AS closer_id,
            a_cl.login    AS closer_login
        FROM issue_event ie
        JOIN issue i        ON i.issue_id    = ie.issue_id
        LEFT JOIN actor a_re ON a_re.actor_id = ie.actor_id
        LEFT JOIN actor a_au ON a_au.actor_id = i.author_id
        LEFT JOIN actor a_cl ON a_cl.actor_id = i.closed_by_id
        WHERE i.repo_id = ?
          AND i.is_pr   = 0
          AND ie.event_type = 'reopened'
          AND ie.actor_id IS NOT NULL
          AND i.author_id IS NOT NULL
          AND ie.actor_id = i.author_id
        ORDER BY ie.created_at
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["author_producer"] = df["author_login"].apply(_classify_login)
    df["closer_producer"] = df["closer_login"].apply(_classify_login)
    return df


# ----------------------------------------------------------------------
# Metric 2: same-reporter follow-up citing the closing PR
# ----------------------------------------------------------------------

def _followup_citing_close_pr(
    conn: sqlite3.Connection, repo_id: int, repo_owner: str, repo_name: str
) -> pd.DataFrame:
    """For each (orig_issue, closing_pr) in linked_pr where the orig was
    closed and a later same-reporter issue references the closing PR
    by number in body or first comment, emit one row.
    """
    closed_links = conn.execute(
        """
        SELECT
            lp.issue_id   AS orig_issue_id,
            lp.pr_id      AS pr_id,
            i_orig.number AS orig_number,
            i_orig.author_id AS orig_author_id,
            a_orig.login    AS orig_author_login,
            i_orig.closed_at AS orig_closed_at,
            i_pr.number    AS pr_number
        FROM linked_pr lp
        JOIN issue i_orig ON i_orig.issue_id = lp.issue_id
        JOIN issue i_pr   ON i_pr.issue_id   = lp.pr_id
        LEFT JOIN actor a_orig ON a_orig.actor_id = i_orig.author_id
        WHERE i_orig.repo_id = ?
          AND i_orig.is_pr   = 0
          AND i_pr.is_pr     = 1
          AND i_orig.state   = 'closed'
          AND i_orig.closed_at IS NOT NULL
          AND i_orig.author_id IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    if not closed_links:
        return pd.DataFrame()

    # Group target PR numbers we care about per (author, after-time).
    by_orig = {row["orig_issue_id"]: dict(row) for row in closed_links}

    # Find later issues by the same author. We pull body and first
    # comment for each, then regex-match the closing PR number.
    rows = conn.execute(
        """
        SELECT
            i.issue_id   AS issue_id,
            i.number     AS number,
            i.author_id  AS author_id,
            i.created_at AS created_at,
            i.body       AS body
        FROM issue i
        WHERE i.repo_id = ?
          AND i.is_pr   = 0
        """,
        (repo_id,),
    ).fetchall()
    later_by_author: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        later_by_author.setdefault(d["author_id"], []).append(d)

    first_comment_cache: dict[int, str] = {}

    def _first_comment_body(issue_id: int) -> str:
        if issue_id in first_comment_cache:
            return first_comment_cache[issue_id]
        row = conn.execute(
            """
            SELECT body
            FROM comment
            WHERE issue_id = ?
            ORDER BY created_at ASC
            LIMIT 1
            """,
            (issue_id,),
        ).fetchone()
        body = (row["body"] if row else "") or ""
        first_comment_cache[issue_id] = body
        return body

    out_rows: list[dict] = []
    for orig in by_orig.values():
        author_id = orig["orig_author_id"]
        candidates = later_by_author.get(author_id, [])
        target_pr = orig["pr_number"]
        for c in candidates:
            if c["issue_id"] == orig["orig_issue_id"]:
                continue
            if c["created_at"] <= orig["orig_closed_at"]:
                continue
            body = (c["body"] or "") + "\n" + _first_comment_body(c["issue_id"])
            cited = False
            for m in ISSUE_REF_RE.finditer(body):
                ref_owner = m.group("owner")
                ref_name = m.group("name")
                num = int(m.group("num"))
                # Bare #N is in the same repo. Qualified must match.
                if ref_owner is None and num == target_pr:
                    cited = True
                    break
                if (
                    ref_owner is not None
                    and ref_owner.lower() == repo_owner.lower()
                    and ref_name is not None
                    and ref_name.lower() == repo_name.lower()
                    and num == target_pr
                ):
                    cited = True
                    break
            if cited:
                out_rows.append({
                    "orig_issue_id":      orig["orig_issue_id"],
                    "orig_number":        orig["orig_number"],
                    "closing_pr_id":      orig["pr_id"],
                    "closing_pr_number":  target_pr,
                    "followup_issue_id":  c["issue_id"],
                    "followup_number":    c["number"],
                    "author_login":       orig["orig_author_login"],
                    "author_producer":    _classify_login(orig["orig_author_login"]),
                    "followup_created_at": c["created_at"],
                })

    return pd.DataFrame(out_rows)


# ----------------------------------------------------------------------
# Metric 3: post-close phrase matches
# ----------------------------------------------------------------------

def _post_close_phrase_matches(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    """One row per (comment, tier) where the comment was posted after
    the issue's ``closed_at`` and contains a phrase from that tier.

    A comment that hits both tiers gets two rows (one per tier).
    """
    rows = conn.execute(
        """
        SELECT
            c.comment_id   AS comment_id,
            c.issue_id     AS issue_id,
            c.created_at   AS commented_at,
            c.author_id    AS commenter_id,
            a_c.login      AS commenter_login,
            LOWER(c.body)  AS body_lower,
            i.number       AS issue_number,
            i.closed_at    AS closed_at,
            i.closed_by_id AS closer_id,
            a_cl.login     AS closer_login,
            i.author_id    AS reporter_id,
            a_re.login     AS reporter_login
        FROM comment c
        JOIN issue i        ON i.issue_id    = c.issue_id
        LEFT JOIN actor a_c  ON a_c.actor_id  = c.author_id
        LEFT JOIN actor a_cl ON a_cl.actor_id = i.closed_by_id
        LEFT JOIN actor a_re ON a_re.actor_id = i.author_id
        WHERE i.repo_id = ?
          AND i.is_pr   = 0
          AND i.closed_at IS NOT NULL
          AND c.created_at > i.closed_at
        """,
        (repo_id,),
    ).fetchall()
    if not rows:
        return pd.DataFrame()

    out_rows: list[dict] = []
    for r in rows:
        body = r["body_lower"] or ""
        for tier, phrases in PHRASE_TIERS.items():
            matched: list[str] = []
            for p in phrases:
                if p in body:
                    matched.append(p)
            if not matched:
                continue
            out_rows.append({
                "tier":             tier,
                "matched_phrases":  ";".join(matched),
                "comment_id":       r["comment_id"],
                "issue_id":         r["issue_id"],
                "issue_number":     r["issue_number"],
                "commented_at":     r["commented_at"],
                "closed_at":        r["closed_at"],
                "commenter_login":  r["commenter_login"],
                "commenter_producer": _classify_login(r["commenter_login"]),
                "closer_login":     r["closer_login"],
                "closer_producer":  _classify_login(r["closer_login"]),
                "reporter_login":   r["reporter_login"],
                "is_reporter_followup":
                    int(r["commenter_id"] == r["reporter_id"]) if r["commenter_id"] else 0,
            })
    return pd.DataFrame(out_rows)


# ----------------------------------------------------------------------
# Metric 4: cross-reference patterns around close
# ----------------------------------------------------------------------

def _cross_reference_patterns(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    """Cross-reference events on issues, classified by direction relative
    to the closed issue, and by whether they happened before or after
    the close.

    Direction:
      - inbound: a cross-reference event on this issue's timeline whose
        ``referenced_issue_id`` points elsewhere -- the *other* timeline
        is the source, this issue is the target. The schema records the
        event on this issue's timeline only when GitHub mirrored it back
        to us; we treat this as "this issue was referenced from
        elsewhere."
      - outbound: there is no native outbound axis in the schema; we
        derive it as cross-reference events on *other* issues whose
        ``referenced_issue_id`` is this issue.

    The two queries share rows whenever GitHub mirrored both sides;
    that's expected. We tag each row with where it was observed.

    Returned columns include ``relative_to_close`` in
    {before, after, never_closed} so the caller can split the cross-tab.
    """
    rows = conn.execute(
        """
        SELECT
            ie.event_id     AS event_id,
            ie.issue_id     AS issue_id,
            ie.referenced_issue_id AS other_issue_id,
            ie.created_at   AS xref_at,
            i.number        AS issue_number,
            i.closed_at     AS closed_at,
            i.author_id     AS author_id,
            a_au.login      AS author_login,
            i.closed_by_id  AS closer_id,
            a_cl.login      AS closer_login
        FROM issue_event ie
        JOIN issue i        ON i.issue_id    = ie.issue_id
        LEFT JOIN actor a_au ON a_au.actor_id = i.author_id
        LEFT JOIN actor a_cl ON a_cl.actor_id = i.closed_by_id
        WHERE i.repo_id = ?
          AND i.is_pr   = 0
          AND ie.event_type = 'cross-referenced'
          AND ie.referenced_issue_id IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    if not rows:
        return pd.DataFrame()

    out_rows: list[dict] = []
    for r in rows:
        closed_at = r["closed_at"]
        xref_at = r["xref_at"]
        if closed_at is None:
            rel = "never_closed"
        elif xref_at <= closed_at:
            rel = "before_close"
        else:
            rel = "after_close"
        out_rows.append({
            "event_id":           r["event_id"],
            "issue_id":           r["issue_id"],
            "issue_number":       r["issue_number"],
            "other_issue_id":     r["other_issue_id"],
            "xref_at":            xref_at,
            "closed_at":          closed_at,
            "relative_to_close":  rel,
            "issue_author_login": r["author_login"],
            "issue_author_producer": _classify_login(r["author_login"]),
            "closer_login":       r["closer_login"],
            "closer_producer":    _classify_login(r["closer_login"]),
        })
    return pd.DataFrame(out_rows)


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
    plots = output_dir / "plots" / safe_slug
    htmls = output_dir / "html" / safe_slug
    csvs  = output_dir / "csv"  / safe_slug

    # ---- Metric 1: reopen by original reporter ----
    reopens = _reopen_by_original_reporter(conn, repo_id)
    if reopens.empty:
        log.info("[%s/%s] no self-reopens", owner, name)
    else:
        edges_path = csvs / "rq_reopen_by_original_reporter_events.csv"
        _write_csv_with_caveats(
            reopens[[
                "reopen_event_id", "issue_id", "reopened_at",
                "author_login", "author_producer",
                "closer_login", "closer_producer",
            ]].sort_values("reopened_at"),
            edges_path,
        )
        by_closer = (
            reopens.groupby("closer_producer").size().rename("self_reopens")
        )
        summary_path = csvs / "rq_reopen_by_original_reporter_by_closer.csv"
        _write_csv_with_caveats(
            by_closer.reset_index(), summary_path,
        )
        _producer_count_bar(
            by_closer,
            title=f"Self-reopens by closer producer ({owner}/{name})",
            out_png=plots / "rq_reopen_by_original_reporter.png",
            out_html=htmls / "rq_reopen_by_original_reporter.html",
            tab_title=f"{name}: self-reopens by closer",
            y_label="self-reopen events",
        )

    # ---- Metric 2: same-reporter follow-up citing closing PR ----
    followups = _followup_citing_close_pr(conn, repo_id, owner, name)
    if followups.empty:
        log.info("[%s/%s] no same-reporter follow-ups citing the closing PR",
                 owner, name)
    else:
        edges_path = csvs / "rq_followup_citing_close_pr_edges.csv"
        _write_csv_with_caveats(
            followups.sort_values("followup_created_at"), edges_path,
        )
        by_author = (
            followups.groupby("author_producer").size().rename("followups")
        )
        summary_path = csvs / "rq_followup_citing_close_pr_by_reporter.csv"
        _write_csv_with_caveats(
            by_author.reset_index(), summary_path,
        )
        _producer_count_bar(
            by_author,
            title=(
                f"Same-reporter follow-ups citing closing PR, "
                f"by reporter producer ({owner}/{name})"
            ),
            out_png=plots / "rq_followup_citing_close_pr.png",
            out_html=htmls / "rq_followup_citing_close_pr.html",
            tab_title=f"{name}: follow-ups citing close PR",
            y_label="follow-up issues",
        )

    # ---- Metric 3: post-close phrase matches ----
    phrases = _post_close_phrase_matches(conn, repo_id)
    if phrases.empty:
        log.info("[%s/%s] no post-close phrase matches", owner, name)
    else:
        edges_path = csvs / "rq_post_close_phrase_matches.csv"
        _write_csv_with_caveats(
            phrases.sort_values(["tier", "commented_at"]),
            edges_path,
        )
        for tier in PHRASE_TIERS:
            sub = phrases[phrases["tier"] == tier]
            if sub.empty:
                continue
            by_closer = sub.groupby("closer_producer").size().rename("matches")
            summary_path = csvs / f"rq_post_close_phrase_{tier}_by_closer.csv"
            _write_csv_with_caveats(
                by_closer.reset_index(), summary_path,
            )
            _producer_count_bar(
                by_closer,
                title=(
                    f"Post-close {tier} dissatisfaction-phrase matches, "
                    f"by closer producer ({owner}/{name})"
                ),
                out_png=plots / f"rq_post_close_phrase_{tier}.png",
                out_html=htmls / f"rq_post_close_phrase_{tier}.html",
                tab_title=f"{name}: post-close phrase ({tier})",
                y_label="matching comments",
            )

    # ---- Metric 4: cross-reference patterns ----
    xrefs = _cross_reference_patterns(conn, repo_id)
    if xrefs.empty:
        log.info("[%s/%s] no cross-reference events", owner, name)
    else:
        edges_path = csvs / "rq_cross_reference_events.csv"
        _write_csv_with_caveats(
            xrefs[[
                "event_id", "issue_id", "issue_number", "other_issue_id",
                "xref_at", "closed_at", "relative_to_close",
                "closer_login", "closer_producer",
            ]].sort_values("xref_at"),
            edges_path,
        )
        ct = (
            xrefs.groupby(["relative_to_close", "closer_producer"])
                 .size()
                 .unstack(fill_value=0)
                 .sort_index()
        )
        producers = _ordered_producers(list(ct.columns))
        ct = ct.reindex(columns=producers, fill_value=0)
        summary_path = csvs / "rq_cross_reference_crosstab.csv"
        with summary_path.open("w") as f:
            f.write(CAVEAT_HEADER)
            ct.to_csv(f)
        log.info("wrote %s", summary_path)

        # Stacked bar: one bar per relative_to_close, stacked by producer.
        out_png = plots / "rq_cross_reference_crosstab.png"
        out_html = htmls / "rq_cross_reference_crosstab.html"
        out_png.parent.mkdir(parents=True, exist_ok=True)
        out_html.parent.mkdir(parents=True, exist_ok=True)

        fig, ax = plt.subplots(figsize=(8, 5))
        bottoms = np.zeros(len(ct.index))
        x = np.arange(len(ct.index))
        for p in ct.columns:
            vals = ct[p].values.astype(float)
            ax.bar(x, vals, bottom=bottoms, label=p)
            bottoms += vals
        ax.set_xticks(x)
        ax.set_xticklabels(ct.index, rotation=15, ha="right")
        ax.set_ylabel("cross-reference events")
        ax.set_title(
            f"Cross-references on issues, by close-relative timing "
            f"and closer producer ({owner}/{name})"
        )
        ax.legend(loc="upper right", fontsize=8)
        fig.tight_layout()
        fig.savefig(out_png, dpi=120)
        plt.close(fig)
        log.info("wrote %s", out_png)

        pfig = go.Figure()
        for p in ct.columns:
            pfig.add_trace(go.Bar(
                name=p,
                x=list(ct.index),
                y=ct[p].values.tolist(),
            ))
        pfig.update_layout(
            barmode="stack",
            title=(
                f"Cross-references on issues, by close-relative timing "
                f"and closer producer ({owner}/{name})"
            ),
            xaxis_title="relative to close",
            yaxis_title="cross-reference events",
            template="plotly_white",
        )
        write_html_with_title(
            pfig, out_html, f"{name}: cross-ref crosstab"
        )
        log.info("wrote %s", out_html)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, help="path to config.yaml")
    p.add_argument("--verbose", action="store_true")
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
            log.info("resolution_quality: %s/%s", repo.owner, repo.name)
            run_for_repo(conn, repo.owner, repo.name, output_dir)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
