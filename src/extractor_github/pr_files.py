# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Extract PR file lists.

Per-PR endpoint; refetched for PRs updated since the last watermark.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Optional

import requests

from ..common.db import HourlyChecker, get_state, set_state, transaction
from ..common.github_client import GitHubClient


log = logging.getLogger(__name__)


def extract_pr_files(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    hourly_checker: Optional[HourlyChecker] = None,
) -> int:
    """Fetch file lists for PRs updated since the last watermark."""
    state_key = f"{owner}/{name}:pr_files:watermark"
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

    log.info("refreshing pr_files for %d PRs in %s/%s", len(rows), owner, name)
    count = 0
    skipped = 0
    max_updated_at: Optional[str] = since

    for row in rows:
        try:
            files = list(
                gh.paginate(f"/repos/{owner}/{name}/pulls/{row['number']}/files")
            )
        except requests.exceptions.HTTPError as exc:
            log.warning(
                "skipping pr_files for PR #%d in %s/%s: %s",
                row["number"], owner, name, exc,
            )
            skipped += 1
            continue

        with transaction(conn):
            conn.execute("DELETE FROM pr_file WHERE issue_id = ?", (row["issue_id"],))
            for f in files:
                conn.execute(
                    """
                    INSERT INTO pr_file (
                        issue_id, path, status, additions, deletions, changes
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["issue_id"],
                        f["filename"],
                        f.get("status"),
                        f.get("additions"),
                        f.get("deletions"),
                        f.get("changes"),
                    ),
                )
        u = row["updated_at"]
        if u and (max_updated_at is None or u > max_updated_at):
            max_updated_at = u
        count += 1
        if hourly_checker is not None and hourly_checker.maybe_check(conn):
            log.info(
                "[%s/%s] hourly integrity check passed during pr_files "
                "(%d / %d done)", owner, name, count, len(rows),
            )
            if max_updated_at and max_updated_at != since:
                set_state(conn, state_key, max_updated_at)

    if max_updated_at and max_updated_at != since:
        set_state(conn, state_key, max_updated_at)

    if skipped:
        log.warning("skipped %d pr_files in %s/%s", skipped, owner, name)
    return count
