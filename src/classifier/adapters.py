# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""SQL adapters: yield Record objects for each artifact kind.

One adapter per target_kind. Each adapter knows the SQL to fetch
records of its kind from a particular subject repo and joins to the
``actor`` table to surface the actor's login.

The adapters yield Records lazily; the classifier orchestrator
consumes the stream and writes verdicts. Total memory footprint stays
small even on large corpora.
"""

from __future__ import annotations

import sqlite3
from typing import Iterator

from .record import Record


def _row_labels(conn: sqlite3.Connection, issue_id: int) -> tuple[str, ...]:
    rows = conn.execute(
        """
        SELECT l.name
        FROM issue_label il
        JOIN label l ON l.label_id = il.label_id
        WHERE il.issue_id = ?
        ORDER BY l.name
        """,
        (issue_id,),
    ).fetchall()
    return tuple(r["name"] for r in rows)


def yield_issues(conn: sqlite3.Connection, repo_id: int) -> Iterator[Record]:
    """Yield issue Records (non-PR rows in the issue table)."""
    rows = conn.execute(
        """
        SELECT i.issue_id, i.title, i.body, i.created_at,
               a.login AS author_login
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.author_id
        WHERE i.repo_id = ? AND i.is_pr = 0
        """,
        (repo_id,),
    ).fetchall()
    for r in rows:
        yield Record(
            target_kind="issue",
            target_id=r["issue_id"],
            author_login=r["author_login"],
            author_email=None,   # GitHub issues don't carry an email
            author_name=None,
            created_at=r["created_at"],
            title=r["title"],
            body=r["body"],
            labels=_row_labels(conn, r["issue_id"]),
        )


def yield_prs(conn: sqlite3.Connection, repo_id: int) -> Iterator[Record]:
    """Yield PR Records."""
    rows = conn.execute(
        """
        SELECT i.issue_id, i.title, i.body, i.created_at,
               a.login AS author_login
        FROM issue i
        LEFT JOIN actor a ON a.actor_id = i.author_id
        WHERE i.repo_id = ? AND i.is_pr = 1
        """,
        (repo_id,),
    ).fetchall()
    for r in rows:
        yield Record(
            target_kind="pr",
            target_id=r["issue_id"],
            author_login=r["author_login"],
            author_email=None,
            author_name=None,
            created_at=r["created_at"],
            title=r["title"],
            body=r["body"],
            labels=_row_labels(conn, r["issue_id"]),
        )


def yield_commits(conn: sqlite3.Connection, repo_id: int) -> Iterator[Record]:
    """Yield commit Records.

    For commits we have author_email but the author_login may be NULL
    (since the noreply-email-derived login only resolves for emails
    matching the GitHub noreply pattern; many commits use real
    emails).
    """
    rows = conn.execute(
        """
        SELECT commit_id, sha, author_login, author_email, author_name,
               authored_at, message
        FROM commit_
        WHERE repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    for r in rows:
        yield Record(
            target_kind="commit",
            target_id=r["commit_id"],
            author_login=r["author_login"],
            author_email=r["author_email"],
            author_name=r["author_name"],
            created_at=r["authored_at"],
            message=r["message"],
        )


def yield_comments(conn: sqlite3.Connection, repo_id: int) -> Iterator[Record]:
    """Yield comment Records (on either issues or PRs of this repo)."""
    rows = conn.execute(
        """
        SELECT c.comment_id, c.body, c.created_at,
               a.login AS author_login
        FROM comment c
        JOIN issue i ON i.issue_id = c.issue_id
        LEFT JOIN actor a ON a.actor_id = c.author_id
        WHERE i.repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    for r in rows:
        yield Record(
            target_kind="comment",
            target_id=r["comment_id"],
            author_login=r["author_login"],
            author_email=None,
            author_name=None,
            created_at=r["created_at"],
            body=r["body"],
        )


def yield_reviews(conn: sqlite3.Connection, repo_id: int) -> Iterator[Record]:
    """Yield PR review Records."""
    rows = conn.execute(
        """
        SELECT r.review_id, r.body, r.state, r.submitted_at,
               a.login AS author_login
        FROM review r
        JOIN issue i ON i.issue_id = r.issue_id
        LEFT JOIN actor a ON a.actor_id = r.author_id
        WHERE i.repo_id = ?
        """,
        (repo_id,),
    ).fetchall()
    for r in rows:
        # A review's submitted_at can be NULL for very old reviews;
        # skip those rather than emitting a Record with no timestamp.
        ts = r["submitted_at"]
        if ts is None:
            continue
        yield Record(
            target_kind="review",
            target_id=r["review_id"],
            author_login=r["author_login"],
            author_email=None,
            author_name=None,
            created_at=ts,
            body=r["body"],
            review_state=r["state"],
        )


# Map target_kind -> adapter function. The orchestrator iterates this.
ADAPTERS = {
    "issue":   yield_issues,
    "pr":      yield_prs,
    "commit":  yield_commits,
    "comment": yield_comments,
    "review":  yield_reviews,
}
