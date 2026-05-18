# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Extract issue and PR comments.

Uses the repo-wide comments endpoint with ``since`` for incremental
fetching. The endpoint returns comments across all issues in the repo,
sorted by ``updated_at``.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Optional

from ..common.db import get_state, set_state, transaction
from ..common.github_client import GitHubClient
from ..common.registries import upsert_actor_from_api


log = logging.getLogger(__name__)


def extract_comments(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
) -> int:
    """Pull issue/PR comments since the last watermark.

    Returns the number of comments upserted. Comments on issues whose
    rows we don't have yet are skipped (they should appear once the
    issue extraction catches up).
    """
    state_key = f"{owner}/{name}:comments:since"
    since = get_state(conn, state_key)
    params: dict[str, Any] = {
        "sort": "updated",
        "direction": "asc",
        "per_page": 100,
    }
    if since:
        params["since"] = since

    count = 0
    skipped_orphan = 0
    max_updated_at: Optional[str] = since

    for c in gh.paginate(f"/repos/{owner}/{name}/issues/comments", params=params):
        issue_url = c.get("issue_url") or ""
        # issue_url format: .../repos/{owner}/{repo}/issues/{number}
        try:
            issue_number = int(issue_url.rsplit("/", 1)[-1])
        except ValueError:
            continue

        issue_row = conn.execute(
            "SELECT issue_id FROM issue WHERE repo_id = ? AND number = ?",
            (repo_id, issue_number),
        ).fetchone()
        if issue_row is None:
            skipped_orphan += 1
            continue

        with transaction(conn):
            author_id = upsert_actor_from_api(conn, c.get("user"))
            conn.execute(
                """
                INSERT INTO comment (
                    comment_id, issue_id, author_id, body, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(comment_id) DO UPDATE SET
                    body = excluded.body,
                    updated_at = excluded.updated_at
                """,
                (
                    c["id"],
                    issue_row["issue_id"],
                    author_id,
                    c.get("body") or "",
                    c["created_at"],
                    c.get("updated_at"),
                ),
            )

        u = c.get("updated_at") or c["created_at"]
        if u and (max_updated_at is None or u > max_updated_at):
            max_updated_at = u
        count += 1

    if max_updated_at and max_updated_at != since:
        set_state(conn, state_key, max_updated_at)

    if skipped_orphan:
        log.warning(
            "skipped %d comments whose parent issue is not yet in the db", skipped_orphan
        )
    log.info("upserted %d comments for %s/%s", count, owner, name)
    return count
