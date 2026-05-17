# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Lazy upsert helpers for ``repo``, ``actor``, and ``label``.

Both extractors encounter actors and repos progressively. These helpers
take whatever GitHub-API or git-derived information is available, upsert
the row, and return its primary key. Subsequent calls for the same
identity return the existing row (with the option to enrich it if more
info has become available).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Optional


# ----------------------------------------------------------------------
# repo
# ----------------------------------------------------------------------

def upsert_repo(
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    role: str,
    default_branch: Optional[str] = None,
) -> int:
    """Insert or update a repo row. Returns repo_id."""
    row = conn.execute(
        "SELECT repo_id, role FROM repo WHERE owner = ? AND name = ?",
        (owner, name),
    ).fetchone()

    if row is None:
        cur = conn.execute(
            """
            INSERT INTO repo (owner, name, role, default_branch, first_seen_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (owner, name, role, default_branch),
        )
        return cur.lastrowid

    repo_id = row["repo_id"]
    new_role = _merge_roles(row["role"], role)
    if new_role != row["role"] or default_branch is not None:
        conn.execute(
            """
            UPDATE repo
            SET role = ?,
                default_branch = COALESCE(?, default_branch)
            WHERE repo_id = ?
            """,
            (new_role, default_branch, repo_id),
        )
    return repo_id


def _merge_roles(existing: str, incoming: str) -> str:
    if existing == incoming:
        return existing
    if {existing, incoming} == {"subject", "support"}:
        return "both"
    if existing == "both" or incoming == "both":
        return "both"
    return incoming


# ----------------------------------------------------------------------
# actor
# ----------------------------------------------------------------------

def upsert_actor(
    conn: sqlite3.Connection,
    login: str,
    gh_user_id: Optional[int] = None,
    gh_type: Optional[str] = None,
) -> int:
    """Insert or update an actor row. Returns actor_id."""
    row = conn.execute(
        "SELECT actor_id, gh_user_id, gh_type FROM actor WHERE login = ?",
        (login,),
    ).fetchone()

    is_bot_login = login.endswith("[bot]")

    if row is None:
        cur = conn.execute(
            """
            INSERT INTO actor (login, gh_user_id, gh_type, is_bot_login, first_seen_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            """,
            (login, gh_user_id, gh_type, is_bot_login),
        )
        return cur.lastrowid

    actor_id = row["actor_id"]
    if (gh_user_id is not None and row["gh_user_id"] is None) or (
        gh_type is not None and row["gh_type"] is None
    ):
        conn.execute(
            """
            UPDATE actor
            SET gh_user_id = COALESCE(?, gh_user_id),
                gh_type = COALESCE(?, gh_type)
            WHERE actor_id = ?
            """,
            (gh_user_id, gh_type, actor_id),
        )
    return actor_id


def upsert_actor_from_api(
    conn: sqlite3.Connection,
    user_obj: Optional[dict[str, Any]],
) -> Optional[int]:
    """Convenience for GitHub API user-shaped dicts.

    Returns None if ``user_obj`` is None (GitHub uses null users for some
    deleted/unknown identities).
    """
    if not user_obj:
        return None
    return upsert_actor(
        conn,
        login=user_obj["login"],
        gh_user_id=user_obj.get("id"),
        gh_type=user_obj.get("type"),
    )


# ----------------------------------------------------------------------
# label
# ----------------------------------------------------------------------

def upsert_label(
    conn: sqlite3.Connection,
    repo_id: int,
    name: str,
    description: Optional[str] = None,
    color: Optional[str] = None,
) -> int:
    """Insert or update a label row. Returns label_id."""
    row = conn.execute(
        "SELECT label_id FROM label WHERE repo_id = ? AND name = ?",
        (repo_id, name),
    ).fetchone()
    if row is None:
        cur = conn.execute(
            """
            INSERT INTO label (repo_id, name, description, color)
            VALUES (?, ?, ?, ?)
            """,
            (repo_id, name, description, color),
        )
        return cur.lastrowid

    label_id = row["label_id"]
    if description is not None or color is not None:
        conn.execute(
            """
            UPDATE label
            SET description = COALESCE(?, description),
                color = COALESCE(?, color)
            WHERE label_id = ?
            """,
            (description, color, label_id),
        )
    return label_id
