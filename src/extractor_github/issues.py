# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Extract issues and pull requests.

Strategy:

* Use the issues endpoint with ``state=all`` and ``since=<watermark>``
  for incremental fetching. The watermark is the highest ``updated_at``
  we've previously stored for the repo. ``since`` is inclusive at the
  second, so we may re-fetch the boundary issue; upserts make this
  harmless.
* The issues endpoint returns both issues and PRs; PRs have a
  ``pull_request`` field. For PRs we additionally fetch the PR-specific
  endpoint to get merge-state, head/base refs, and changed-file counts.
* Per-PR file lists, comments, reviews, reactions, and timeline events
  live in adjacent modules.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any, Optional

import requests

from ..common.db import HourlyChecker, get_state, set_state, transaction
from ..common.github_client import GitHubClient
from ..common.registries import upsert_actor_from_api, upsert_label


log = logging.getLogger(__name__)


_FIXES_KEYWORD_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s*"
    r"(?:(?P<owner>[\w.-]+)/(?P<name>[\w.-]+))?#(?P<num>\d+)",
    re.IGNORECASE,
)


def extract_issues(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
) -> int:
    """Pull issues and PRs since the last watermark.

    Returns the number of issues/PRs upserted.
    """
    state_key = f"{owner}/{name}:issues:since"
    since = get_state(conn, state_key)
    params: dict[str, Any] = {
        "state": "all",
        "sort": "updated",
        "direction": "asc",  # ascending so we can advance the watermark monotonically
        "per_page": 100,
    }
    if since:
        params["since"] = since
        log.info("fetching %s/%s issues since %s", owner, name, since)
    else:
        log.info("fetching %s/%s issues from beginning", owner, name)

    count = 0
    max_updated_at: Optional[str] = since

    for issue in gh.paginate(f"/repos/{owner}/{name}/issues", params=params):
        with transaction(conn):
            issue_id = _upsert_issue(conn, repo_id, issue)
            _replace_issue_labels(conn, repo_id, issue_id, issue.get("labels", []))
            if issue.get("pull_request") is not None:
                _upsert_pull_request_stub(conn, issue_id, issue)
                # We'll fetch full PR details separately so we can do it in
                # batches with awareness of merge state etc.
            _record_linked_prs_from_body(conn, repo_id, issue_id, issue)

        u = issue.get("updated_at")
        if u and (max_updated_at is None or u > max_updated_at):
            max_updated_at = u
        count += 1

    if max_updated_at and max_updated_at != since:
        set_state(conn, state_key, max_updated_at)

    log.info("upserted %d issues/PRs for %s/%s", count, owner, name)
    return count


# ----------------------------------------------------------------------
# PR detail enrichment
# ----------------------------------------------------------------------

def enrich_pull_requests(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    hourly_checker: Optional[HourlyChecker] = None,
) -> int:
    """Fetch PR-specific fields (merge state, head/base refs, file counts).

    Strategy: select PRs that either (a) have never been enriched
    (``base_ref`` is still NULL because only the stub from the issues
    pass exists) or (b) have a more recent ``updated_at`` than the
    enrichment watermark. The watermark is per-repo and advances to the
    highest ``updated_at`` of successfully-enriched PRs in this pass,
    so the next run skips PRs that haven't changed.
    """
    state_key = f"{owner}/{name}:enrich_prs:watermark"
    since = get_state(conn, state_key)

    if since:
        rows = conn.execute(
            """
            SELECT i.issue_id, i.number, i.updated_at
            FROM issue i
            LEFT JOIN pull_request pr ON pr.issue_id = i.issue_id
            WHERE i.repo_id = ?
              AND i.is_pr = 1
              AND (pr.base_ref IS NULL OR i.updated_at > ?)
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

    log.info("enriching %d PRs in %s/%s", len(rows), owner, name)

    count = 0
    skipped = 0
    max_updated_at: Optional[str] = since
    for row in rows:
        try:
            pr_data = gh.get_json(f"/repos/{owner}/{name}/pulls/{row['number']}")
        except requests.exceptions.HTTPError as exc:
            log.warning(
                "skipping PR detail for #%d in %s/%s: %s",
                row["number"], owner, name, exc,
            )
            skipped += 1
            continue
        if pr_data is None:
            continue
        with transaction(conn):
            _upsert_pull_request_full(conn, row["issue_id"], pr_data)
        u = row["updated_at"]
        if u and (max_updated_at is None or u > max_updated_at):
            max_updated_at = u
        count += 1
        if hourly_checker is not None and hourly_checker.maybe_check(conn):
            log.info(
                "[%s/%s] hourly integrity check passed during PR enrichment "
                "(%d / %d done)", owner, name, count, len(rows),
            )
            if max_updated_at and max_updated_at != since:
                set_state(conn, state_key, max_updated_at)

    if max_updated_at and max_updated_at != since:
        set_state(conn, state_key, max_updated_at)

    if skipped:
        log.warning("skipped %d PR detail enrichments in %s/%s", skipped, owner, name)
    return count


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------

def _upsert_issue(
    conn: sqlite3.Connection,
    repo_id: int,
    issue: dict[str, Any],
) -> int:
    author_id = upsert_actor_from_api(conn, issue.get("user"))
    closed_by_id = upsert_actor_from_api(conn, issue.get("closed_by"))
    is_pr = issue.get("pull_request") is not None

    row = conn.execute(
        "SELECT issue_id FROM issue WHERE repo_id = ? AND number = ?",
        (repo_id, issue["number"]),
    ).fetchone()

    if row is None:
        cur = conn.execute(
            """
            INSERT INTO issue (
                repo_id, number, gh_node_id, title, body, author_id,
                state, state_reason, created_at, updated_at,
                closed_at, closed_by_id, is_pr, last_observed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                repo_id,
                issue["number"],
                issue.get("node_id"),
                issue["title"],
                issue.get("body"),
                author_id,
                issue["state"],
                issue.get("state_reason"),
                issue["created_at"],
                issue["updated_at"],
                issue.get("closed_at"),
                closed_by_id,
                is_pr,
            ),
        )
        return cur.lastrowid

    issue_id = row["issue_id"]
    conn.execute(
        """
        UPDATE issue SET
            title = ?, body = ?, author_id = COALESCE(?, author_id),
            state = ?, state_reason = ?, updated_at = ?,
            closed_at = ?, closed_by_id = COALESCE(?, closed_by_id),
            last_observed_at = datetime('now')
        WHERE issue_id = ?
        """,
        (
            issue["title"],
            issue.get("body"),
            author_id,
            issue["state"],
            issue.get("state_reason"),
            issue["updated_at"],
            issue.get("closed_at"),
            closed_by_id,
            issue_id,
        ),
    )
    return issue_id


def _replace_issue_labels(
    conn: sqlite3.Connection,
    repo_id: int,
    issue_id: int,
    labels: list[dict[str, Any]],
) -> None:
    """Replace the issue's currently-applied label set."""
    conn.execute("DELETE FROM issue_label WHERE issue_id = ?", (issue_id,))
    for lbl in labels:
        label_id = upsert_label(
            conn,
            repo_id=repo_id,
            name=lbl["name"],
            description=lbl.get("description"),
            color=lbl.get("color"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO issue_label (issue_id, label_id) VALUES (?, ?)",
            (issue_id, label_id),
        )


def _upsert_pull_request_stub(
    conn: sqlite3.Connection,
    issue_id: int,
    issue: dict[str, Any],
) -> None:
    """Insert a minimal pull_request row from the issue-list payload.

    The issue list returns only ``pull_request: {url, ...}``; merge state
    and other PR-specific fields require fetching the PR endpoint. We
    insert with placeholder values so foreign keys to issue resolve, then
    enrich later.
    """
    conn.execute(
        """
        INSERT OR IGNORE INTO pull_request (issue_id, merged)
        VALUES (?, 0)
        """,
        (issue_id,),
    )


def _upsert_pull_request_full(
    conn: sqlite3.Connection,
    issue_id: int,
    pr: dict[str, Any],
) -> None:
    """Update pull_request row with full data from the PR endpoint."""
    merged_by_id = upsert_actor_from_api(conn, pr.get("merged_by"))
    head_repo_id = None
    head = pr.get("head") or {}
    head_repo = head.get("repo")
    if head_repo and head_repo.get("owner"):
        # We don't auto-add foreign repos as 'subject' or 'support'; we
        # add them with role='support' if they don't exist, since the
        # role enum requires a value. They function as references only.
        from ..common.registries import upsert_repo
        head_repo_id = upsert_repo(
            conn,
            owner=head_repo["owner"]["login"],
            name=head_repo["name"],
            role="support",
        )

    conn.execute(
        """
        INSERT INTO pull_request (
            issue_id, merged, merged_at, merged_by_id, merge_commit_sha,
            base_ref, head_ref, head_repo_id, draft,
            additions, deletions, changed_files, mergeable_state
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(issue_id) DO UPDATE SET
            merged = excluded.merged,
            merged_at = excluded.merged_at,
            merged_by_id = COALESCE(excluded.merged_by_id, pull_request.merged_by_id),
            merge_commit_sha = excluded.merge_commit_sha,
            base_ref = excluded.base_ref,
            head_ref = excluded.head_ref,
            head_repo_id = COALESCE(excluded.head_repo_id, pull_request.head_repo_id),
            draft = excluded.draft,
            additions = excluded.additions,
            deletions = excluded.deletions,
            changed_files = excluded.changed_files,
            mergeable_state = excluded.mergeable_state
        """,
        (
            issue_id,
            bool(pr.get("merged")),
            pr.get("merged_at"),
            merged_by_id,
            pr.get("merge_commit_sha"),
            (pr.get("base") or {}).get("ref"),
            head.get("ref"),
            head_repo_id,
            pr.get("draft"),
            pr.get("additions"),
            pr.get("deletions"),
            pr.get("changed_files"),
            pr.get("mergeable_state"),
        ),
    )


def _record_linked_prs_from_body(
    conn: sqlite3.Connection,
    repo_id: int,
    issue_id: int,
    issue: dict[str, Any],
) -> None:
    """Scan an issue/PR body for ``Fixes #N`` style references.

    For PR bodies, the references typically point at the issues the PR
    closes. For issue bodies, references can go either way. We record
    both directions if they're discoverable; the analysis layer can
    distinguish.

    Cross-repo references (``owner/name#N``) are recorded only when the
    target repo is one we already have rows for.
    """
    body = issue.get("body") or ""
    if not body:
        return

    is_pr = issue.get("pull_request") is not None

    for m in _FIXES_KEYWORD_RE.finditer(body):
        ref_owner = m.group("owner")
        ref_name = m.group("name")
        ref_num = int(m.group("num"))

        if ref_owner and ref_name:
            target_repo = conn.execute(
                "SELECT repo_id FROM repo WHERE owner = ? AND name = ?",
                (ref_owner, ref_name),
            ).fetchone()
            if target_repo is None:
                continue
            target_repo_id = target_repo["repo_id"]
        else:
            target_repo_id = repo_id

        target = conn.execute(
            "SELECT issue_id, is_pr FROM issue WHERE repo_id = ? AND number = ?",
            (target_repo_id, ref_num),
        ).fetchone()
        if target is None:
            # Forward reference; we'll catch it next time through if
            # the target is added later.
            continue

        if is_pr and not target["is_pr"]:
            # PR body referencing an issue: standard case.
            conn.execute(
                """
                INSERT OR IGNORE INTO linked_pr (issue_id, pr_id, link_source)
                VALUES (?, ?, 'pr_body_keyword')
                """,
                (target["issue_id"], issue_id),
            )
        elif not is_pr and target["is_pr"]:
            # Issue body referencing a PR.
            conn.execute(
                """
                INSERT OR IGNORE INTO linked_pr (issue_id, pr_id, link_source)
                VALUES (?, ?, 'pr_body_keyword')
                """,
                (issue_id, target["issue_id"]),
            )
        # Other combinations (PR↔PR, issue↔issue) are uninteresting for
        # this table's purpose.
