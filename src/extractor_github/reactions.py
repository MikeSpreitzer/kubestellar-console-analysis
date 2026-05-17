# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Extract reactions on issues, comments, and reviews.

Reactions are per-target. We fetch them for issues/PRs updated since the
last watermark, and for all comments/reviews on those issues. Reactions
are unsigned 64-bit ids and we replace the full set per target on
refresh, since deletions on GitHub side don't otherwise propagate.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import requests

from ..common.db import HourlyChecker, get_state, set_state, transaction
from ..common.github_client import GitHubClient
from ..common.registries import upsert_actor_from_api


log = logging.getLogger(__name__)


def extract_reactions(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    hourly_checker: Optional[HourlyChecker] = None,
) -> int:
    """Fetch reactions for issues/PRs (and their comments/reviews) updated
    since the last watermark.
    """
    state_key = f"{owner}/{name}:reactions:watermark"
    since = get_state(conn, state_key)

    if since:
        rows = conn.execute(
            """
            SELECT issue_id, number, updated_at, is_pr
            FROM issue
            WHERE repo_id = ? AND updated_at > ?
            ORDER BY updated_at ASC
            """,
            (repo_id, since),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT issue_id, number, updated_at, is_pr
            FROM issue
            WHERE repo_id = ?
            ORDER BY updated_at ASC
            """,
            (repo_id,),
        ).fetchall()

    log.info("refreshing reactions for %d issues/PRs in %s/%s", len(rows), owner, name)
    count = 0
    skipped = 0
    max_updated_at: Optional[str] = since

    for row in rows:
        try:
            _refresh_issue_reactions(
                conn, gh, owner, name, row["issue_id"], row["number"]
            )
            _refresh_comment_reactions(conn, gh, owner, name, row["issue_id"])
            if row["is_pr"]:
                _refresh_review_reactions(
                    conn, gh, owner, name, row["issue_id"], row["number"]
                )
        except requests.exceptions.HTTPError as exc:
            log.warning(
                "skipping reactions for #%d in %s/%s: %s",
                row["number"], owner, name, exc,
            )
            skipped += 1
            continue

        u = row["updated_at"]
        if u and (max_updated_at is None or u > max_updated_at):
            max_updated_at = u
        count += 1
        if hourly_checker is not None and hourly_checker.maybe_check(conn):
            log.info(
                "[%s/%s] hourly integrity check passed during reactions "
                "(%d / %d done)", owner, name, count, len(rows),
            )
            if max_updated_at and max_updated_at != since:
                set_state(conn, state_key, max_updated_at)

    if max_updated_at and max_updated_at != since:
        set_state(conn, state_key, max_updated_at)

    if skipped:
        log.warning("skipped %d reactions sets in %s/%s", skipped, owner, name)
    return count


def _refresh_issue_reactions(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    issue_id: int,
    number: int,
) -> None:
    new = list(
        gh.paginate(
            f"/repos/{owner}/{name}/issues/{number}/reactions",
            accept="application/vnd.github.squirrel-girl-preview+json",
        )
    )
    with transaction(conn):
        conn.execute(
            "DELETE FROM reaction WHERE target_kind = 'issue' AND target_id = ?",
            (issue_id,),
        )
        for r in new:
            actor_id = upsert_actor_from_api(conn, r.get("user"))
            conn.execute(
                """
                INSERT OR IGNORE INTO reaction (
                    reaction_id, target_kind, target_id, content, actor_id, created_at
                )
                VALUES (?, 'issue', ?, ?, ?, ?)
                """,
                (r["id"], issue_id, r["content"], actor_id, r["created_at"]),
            )


def _refresh_comment_reactions(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    issue_id: int,
) -> None:
    comment_rows = conn.execute(
        "SELECT comment_id FROM comment WHERE issue_id = ?", (issue_id,)
    ).fetchall()
    for cr in comment_rows:
        comment_id = cr["comment_id"]
        new = list(
            gh.paginate(
                f"/repos/{owner}/{name}/issues/comments/{comment_id}/reactions",
                accept="application/vnd.github.squirrel-girl-preview+json",
            )
        )
        with transaction(conn):
            conn.execute(
                "DELETE FROM reaction WHERE target_kind = 'comment' AND target_id = ?",
                (comment_id,),
            )
            for r in new:
                actor_id = upsert_actor_from_api(conn, r.get("user"))
                conn.execute(
                    """
                    INSERT OR IGNORE INTO reaction (
                        reaction_id, target_kind, target_id, content, actor_id, created_at
                    )
                    VALUES (?, 'comment', ?, ?, ?, ?)
                    """,
                    (r["id"], comment_id, r["content"], actor_id, r["created_at"]),
                )


def _refresh_review_reactions(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    issue_id: int,
    number: int,
) -> None:
    # Review reactions are accessed via the pulls reviews endpoint per
    # review id. We have review ids stored already.
    review_rows = conn.execute(
        "SELECT review_id FROM review WHERE issue_id = ?", (issue_id,)
    ).fetchall()
    for rr in review_rows:
        review_id = rr["review_id"]
        # Note: GitHub's review-reactions endpoint is on
        # /pulls/{number}/reviews/{review_id}/reactions but is less
        # commonly used; we fetch defensively and ignore 404.
        try:
            new = list(
                gh.paginate(
                    f"/repos/{owner}/{name}/pulls/{number}/reviews/{review_id}/reactions",
                    accept="application/vnd.github.squirrel-girl-preview+json",
                )
            )
        except Exception:
            log.debug("review %d has no reactions endpoint", review_id)
            continue
        with transaction(conn):
            conn.execute(
                "DELETE FROM reaction WHERE target_kind = 'review' AND target_id = ?",
                (review_id,),
            )
            for r in new:
                actor_id = upsert_actor_from_api(conn, r.get("user"))
                conn.execute(
                    """
                    INSERT OR IGNORE INTO reaction (
                        reaction_id, target_kind, target_id, content, actor_id, created_at
                    )
                    VALUES (?, 'review', ?, ?, ?, ?)
                    """,
                    (r["id"], review_id, r["content"], actor_id, r["created_at"]),
                )
