# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Extract PR reviews.

The reviews endpoint is per-PR. We refetch reviews for each PR whose
issue ``updated_at`` exceeds the last reviews-watermark.
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


def extract_reviews(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    hourly_checker: Optional[HourlyChecker] = None,
) -> int:
    """Fetch reviews for PRs updated since the last watermark.

    Returns the number of PRs whose reviews were refetched.
    """
    state_key = f"{owner}/{name}:reviews:watermark"
    since = get_state(conn, state_key)

    if since:
        rows = conn.execute(
            """
            SELECT i.issue_id, i.number, i.updated_at
            FROM issue i
            WHERE i.repo_id = ? AND i.is_pr = 1 AND i.updated_at > ?
            ORDER BY i.updated_at ASC
            """,
            (repo_id, since),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT i.issue_id, i.number, i.updated_at
            FROM issue i
            WHERE i.repo_id = ? AND i.is_pr = 1
            ORDER BY i.updated_at ASC
            """,
            (repo_id,),
        ).fetchall()

    log.info("refreshing reviews for %d PRs in %s/%s", len(rows), owner, name)
    count = 0
    skipped = 0
    max_updated_at: Optional[str] = since

    for row in rows:
        try:
            reviews = list(
                gh.paginate(f"/repos/{owner}/{name}/pulls/{row['number']}/reviews")
            )
        except requests.exceptions.HTTPError as exc:
            log.warning(
                "skipping reviews for PR #%d in %s/%s: %s",
                row["number"], owner, name, exc,
            )
            skipped += 1
            continue
        with transaction(conn):
            conn.execute("DELETE FROM review WHERE issue_id = ?", (row["issue_id"],))
            for r in reviews:
                author_id = upsert_actor_from_api(conn, r.get("user"))
                conn.execute(
                    """
                    INSERT INTO review (
                        review_id, issue_id, author_id, state, body,
                        submitted_at, commit_sha
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r["id"],
                        row["issue_id"],
                        author_id,
                        r["state"],
                        r.get("body"),
                        r.get("submitted_at"),
                        r.get("commit_id"),
                    ),
                )
        u = row["updated_at"]
        if u and (max_updated_at is None or u > max_updated_at):
            max_updated_at = u
        count += 1
        if hourly_checker is not None and hourly_checker.maybe_check(conn):
            log.info(
                "[%s/%s] hourly integrity check passed during reviews "
                "(%d / %d done)", owner, name, count, len(rows),
            )
            if max_updated_at and max_updated_at != since:
                set_state(conn, state_key, max_updated_at)

    if max_updated_at and max_updated_at != since:
        set_state(conn, state_key, max_updated_at)

    if skipped:
        log.warning("skipped %d reviews in %s/%s", skipped, owner, name)
    return count
