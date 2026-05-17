# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Walk a local git clone, populating ``commit``, ``commit_file``, and
``workflow_file_state``.

Strategy:

* Per repo, the watermark is the last sha we processed on the default
  branch. On a fresh run this is empty, and we walk the full history
  reachable from the default branch tip. On a re-run we walk
  ``<watermark>..HEAD``.
* For each commit (oldest first), insert into ``commit_``, then per-file
  rows into ``commit_file``. For workflow files (path under
  ``.github/workflows/``), additionally snapshot post-commit content
  into ``workflow_file_state``.
* If the default branch was force-pushed (the watermark sha is not
  reachable from HEAD), we log and rewalk from the beginning.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
from pathlib import Path
from typing import Optional

from ..common.db import checkpoint, get_state, set_state, transaction
from ..common.registries import upsert_actor, upsert_repo
from . import git_cli


log = logging.getLogger(__name__)


# Path prefix that triggers workflow_file_state snapshotting.
WORKFLOW_PATH_PREFIX = ".github/workflows/"

# Checkpoint cadence during a long history walk.
CHECKPOINT_EVERY_N_COMMITS = 500


def walk_repo(
    conn: sqlite3.Connection,
    repo_owner: str,
    repo_name: str,
    repo_role: str,
    repo_path: Path,
) -> int:
    """Walk a local git clone, upserting commits and workflow file states.

    Returns the number of commits processed (newly added in this pass).
    """
    if not repo_path.exists():
        log.warning("repo path %s does not exist; skipping", repo_path)
        return 0

    repo_id = upsert_repo(conn, repo_owner, repo_name, repo_role)

    branch = git_cli.default_branch(repo_path)
    head_sha = git_cli.rev_parse(repo_path, branch)
    if head_sha is None:
        log.warning("cannot resolve %s in %s; skipping", branch, repo_path)
        return 0

    state_key = f"{repo_owner}/{repo_name}:git:last_walked_sha"
    last_sha = get_state(conn, state_key)

    rev_range = _resolve_rev_range(repo_path, last_sha, head_sha)
    log.info(
        "walking %s/%s rev range %s (head=%s)",
        repo_owner, repo_name, rev_range, head_sha[:8],
    )

    count = 0
    last_processed_sha = last_sha
    for commit in git_cli.iter_commits(repo_path, rev_range):
        _process_commit(conn, repo_id, repo_path, commit)
        last_processed_sha = commit["sha"]
        count += 1
        # Persist the watermark and checkpoint periodically so an
        # interrupted walk doesn't have to redo the entire history.
        if count % CHECKPOINT_EVERY_N_COMMITS == 0:
            set_state(conn, state_key, last_processed_sha)
            checkpoint(conn)

    if last_processed_sha and last_processed_sha != last_sha:
        set_state(conn, state_key, last_processed_sha)

    log.info("processed %d commits for %s/%s", count, repo_owner, repo_name)
    return count


def _resolve_rev_range(
    repo_path: Path,
    last_sha: Optional[str],
    head_sha: str,
) -> str:
    """Decide what range to walk.

    If ``last_sha`` is set and reachable from HEAD, walk
    ``<last_sha>..HEAD``. If unreachable (force-push or rebase), walk
    everything reachable from HEAD.
    """
    if not last_sha:
        return head_sha
    # Check that last_sha is an ancestor of HEAD.
    try:
        result = git_cli.run_git(
            repo_path,
            ["merge-base", "--is-ancestor", last_sha, head_sha],
            check=False,
        )
    except Exception:
        log.warning(
            "couldn't verify ancestry of %s; rewalking full history",
            last_sha[:8],
        )
        return head_sha
    if result.returncode == 0:
        return f"{last_sha}..{head_sha}"
    log.warning(
        "watermark %s is not an ancestor of %s; rewalking from the beginning",
        last_sha[:8], head_sha[:8],
    )
    return head_sha


def _process_commit(
    conn: sqlite3.Connection,
    repo_id: int,
    repo_path: Path,
    commit: dict,
) -> None:
    """Insert one commit and its file changes into the database."""
    # Resolve a possible login from the noreply email.
    author_login = git_cli.email_to_login(commit["author_email"])

    with transaction(conn):
        # Lazily register the author as an actor when we have a login.
        # Without a login we have only name/email, which we don't promote
        # to the ``actor`` table; commit_.author_login stays NULL.
        if author_login:
            upsert_actor(conn, login=author_login)

        # Idempotent insert: skip if we've seen this sha for this repo.
        existing = conn.execute(
            "SELECT commit_id FROM commit_ WHERE repo_id = ? AND sha = ?",
            (repo_id, commit["sha"]),
        ).fetchone()
        if existing:
            return

        cur = conn.execute(
            """
            INSERT INTO commit_ (
                repo_id, sha, parent_shas,
                author_name, author_email, author_login, authored_at,
                committer_name, committer_email, committed_at, message
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                repo_id,
                commit["sha"],
                "\n".join(commit["parent_shas"]) if commit["parent_shas"] else None,
                commit["author_name"],
                commit["author_email"],
                author_login,
                commit["authored_at"],
                commit["committer_name"],
                commit["committer_email"],
                commit["committed_at"],
                commit["message"],
            ),
        )
        commit_id = cur.lastrowid

        file_changes = git_cli.commit_file_changes(repo_path, commit["sha"])
        for f in file_changes:
            conn.execute(
                """
                INSERT INTO commit_file (
                    commit_id, path, old_path, change_type,
                    lines_added, lines_removed
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_id, f["path"], f["old_path"], f["change_type"],
                    f["lines_added"], f["lines_removed"],
                ),
            )
            if _is_workflow_path(f["path"]) or _is_workflow_path(f.get("old_path")):
                _snapshot_workflow_file_state(
                    conn, repo_id, repo_path, commit_id, commit["sha"], f,
                )


def _is_workflow_path(path: Optional[str]) -> bool:
    if not path:
        return False
    return path.startswith(WORKFLOW_PATH_PREFIX)


def _snapshot_workflow_file_state(
    conn: sqlite3.Connection,
    repo_id: int,
    repo_path: Path,
    commit_id: int,
    sha: str,
    change: dict,
) -> None:
    """Record the post-commit content of a workflow file (or its absence
    after deletion).

    For renames, both the old and new paths get a state row at this
    commit: the old path is recorded as deleted, the new path as
    existing with the post-rename content.
    """
    new_path = change["path"]
    old_path = change.get("old_path")
    change_type = change["change_type"]

    if change_type == "D":
        _insert_workflow_state(
            conn, repo_id, new_path, commit_id, content=None, exists=False,
        )
        return

    if change_type == "R" and old_path and _is_workflow_path(old_path):
        _insert_workflow_state(
            conn, repo_id, old_path, commit_id, content=None, exists=False,
        )

    if not _is_workflow_path(new_path):
        return

    blob = git_cli.show_file_at(repo_path, sha, new_path)
    if blob is None:
        # Could not read; record as nonexistent rather than silently
        # missing.
        _insert_workflow_state(
            conn, repo_id, new_path, commit_id, content=None, exists=False,
        )
        return

    try:
        content = blob.decode("utf-8")
    except UnicodeDecodeError:
        content = blob.decode("utf-8", errors="replace")
    content_sha = hashlib.sha256(blob).hexdigest()
    _insert_workflow_state(
        conn, repo_id, new_path, commit_id,
        content=content, content_sha=content_sha, exists=True,
    )


def _insert_workflow_state(
    conn: sqlite3.Connection,
    repo_id: int,
    path: str,
    commit_id: int,
    *,
    content: Optional[str],
    content_sha: Optional[str] = None,
    exists: bool,
) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO workflow_file_state (
            repo_id, path, commit_id, content, content_sha, exists_after
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (repo_id, path, commit_id, content, content_sha, exists),
    )
