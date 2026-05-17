# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Extract issue timeline events.

Strategy: per-issue, fetch the timeline endpoint and store events. The
timeline endpoint does not support a ``since`` parameter, so we fetch
the full timeline for any issue whose ``updated_at`` is more recent than
when we last fetched its timeline.

We track per-issue last-fetch in ``extraction_state`` keyed by
``{owner}/{repo}:timeline:{number}``. To avoid an unbounded number of
state keys, we use a single key per repo storing a watermark and trust
that issues updated after the watermark are the ones to refetch.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

import requests

from ..common.db import HourlyChecker, get_state, set_state, transaction
from ..common.github_client import GitHubClient
from ..common.registries import upsert_actor_from_api, upsert_label


log = logging.getLogger(__name__)


# Timeline event types we care to record explicitly. Other types are
# stored with their raw payload in ``extra_json`` for forensic value.
KNOWN_EVENT_TYPES = {
    "labeled",
    "unlabeled",
    "closed",
    "reopened",
    "merged",
    "commented",
    "reviewed",
    "review_requested",
    "review_request_removed",
    "cross-referenced",
    "renamed",
    "assigned",
    "unassigned",
    "milestoned",
    "demilestoned",
    "head_ref_force_pushed",
    "head_ref_deleted",
    "head_ref_restored",
    "ready_for_review",
    "convert_to_draft",
    "locked",
    "unlocked",
    "pinned",
    "unpinned",
    "transferred",
    "auto_merge_enabled",
    "auto_merge_disabled",
    "deployed",
    "deployment_environment_changed",
    "marked_as_duplicate",
    "unmarked_as_duplicate",
    "connected",
    "disconnected",
    "subscribed",
    "unsubscribed",
    "mentioned",
    "referenced",
}


def extract_timelines(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    hourly_checker: Optional[HourlyChecker] = None,
) -> int:
    """Refresh timelines for issues updated since the last watermark.

    Returns the number of issues whose timelines were refetched.
    """
    state_key = f"{owner}/{name}:timelines:watermark"
    since = get_state(conn, state_key)

    if since:
        rows = conn.execute(
            """
            SELECT issue_id, number, updated_at
            FROM issue
            WHERE repo_id = ? AND updated_at > ?
            ORDER BY updated_at ASC
            """,
            (repo_id, since),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT issue_id, number, updated_at
            FROM issue
            WHERE repo_id = ?
            ORDER BY updated_at ASC
            """,
            (repo_id,),
        ).fetchall()

    log.info("refreshing timelines for %d issues in %s/%s", len(rows), owner, name)

    count = 0
    skipped = 0
    max_updated_at: Optional[str] = since

    for row in rows:
        try:
            _refetch_timeline(
                conn, gh, owner, name, repo_id, row["issue_id"], row["number"]
            )
        except requests.exceptions.HTTPError as exc:
            log.warning(
                "skipping timeline for issue #%d in %s/%s: %s",
                row["number"], owner, name, exc,
            )
            skipped += 1
            # Do NOT advance the watermark past this row's updated_at,
            # so a future run will retry it.
            continue

        u = row["updated_at"]
        if u and (max_updated_at is None or u > max_updated_at):
            max_updated_at = u
        count += 1
        if hourly_checker is not None and hourly_checker.maybe_check(conn):
            log.info(
                "[%s/%s] hourly integrity check passed during timelines "
                "(%d / %d done)", owner, name, count, len(rows),
            )
            if max_updated_at and max_updated_at != since:
                set_state(conn, state_key, max_updated_at)

    if max_updated_at and max_updated_at != since:
        set_state(conn, state_key, max_updated_at)

    if skipped:
        log.warning("skipped %d timelines in %s/%s", skipped, owner, name)
    return count


def _refetch_timeline(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    issue_id: int,
    number: int,
) -> None:
    """Replace the issue's timeline with a fresh fetch.

    We delete and re-insert because individual events can in principle be
    edited or removed; idempotent re-fetch is simpler than diffing.
    """
    events_path = f"/repos/{owner}/{name}/issues/{number}/timeline"
    new_events: list[dict[str, Any]] = list(
        gh.paginate(
            events_path,
            accept="application/vnd.github.mockingbird-preview+json",
        )
    )

    with transaction(conn):
        conn.execute("DELETE FROM issue_event WHERE issue_id = ?", (issue_id,))
        for ev in new_events:
            _insert_event(conn, repo_id, issue_id, ev)


def _insert_event(
    conn: sqlite3.Connection,
    repo_id: int,
    issue_id: int,
    ev: dict[str, Any],
) -> None:
    event_type = ev.get("event") or "unknown"
    actor_id = upsert_actor_from_api(conn, ev.get("actor"))
    created_at = ev.get("created_at") or ev.get("submitted_at")
    if created_at is None:
        # Some embedded comment/review events use different field names
        created_at = (ev.get("commit") or {}).get("date")
    if created_at is None:
        # Skip events without timestamps; they're rare and uninformative
        return

    label_id = None
    if event_type in ("labeled", "unlabeled"):
        lbl = ev.get("label") or {}
        if lbl.get("name"):
            label_id = upsert_label(
                conn,
                repo_id=repo_id,
                name=lbl["name"],
                color=lbl.get("color"),
            )

    referenced_issue_id = None
    if event_type == "cross-referenced":
        src = ev.get("source") or {}
        if src.get("type") == "issue":
            ref_issue = src.get("issue") or {}
            ref_repo = (ref_issue.get("repository") or {})
            ref_owner = (ref_repo.get("owner") or {}).get("login")
            ref_name = ref_repo.get("name")
            ref_number = ref_issue.get("number")
            if ref_owner and ref_name and ref_number:
                row = conn.execute(
                    """
                    SELECT i.issue_id
                    FROM issue i
                    JOIN repo r ON r.repo_id = i.repo_id
                    WHERE r.owner = ? AND r.name = ? AND i.number = ?
                    """,
                    (ref_owner, ref_name, ref_number),
                ).fetchone()
                if row:
                    referenced_issue_id = row["issue_id"]

    review_state = None
    review_id = None
    comment_id = None
    if event_type == "reviewed":
        review_id = ev.get("id")
        review_state = ev.get("state")
        if actor_id is None:
            actor_id = upsert_actor_from_api(conn, ev.get("user"))
    elif event_type == "commented":
        comment_id = ev.get("id")
        if actor_id is None:
            actor_id = upsert_actor_from_api(conn, ev.get("user"))

    old_value = None
    new_value = None
    if event_type == "renamed":
        rename = ev.get("rename") or {}
        old_value = rename.get("from")
        new_value = rename.get("to")

    extra_json: Optional[str] = None
    if event_type not in KNOWN_EVENT_TYPES:
        try:
            extra_json = json.dumps(ev, default=str)[:65000]
        except (TypeError, ValueError):
            extra_json = None

    conn.execute(
        """
        INSERT INTO issue_event (
            issue_id, gh_event_id, event_type, actor_id, created_at,
            label_id, comment_id, review_id, review_state,
            referenced_issue_id, old_value, new_value, extra_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            issue_id,
            ev.get("id"),
            event_type,
            actor_id,
            created_at,
            label_id,
            comment_id,
            review_id,
            review_state,
            referenced_issue_id,
            old_value,
            new_value,
            extra_json,
        ),
    )

    # If this event corresponds to GitHub's "linked" notion (the
    # connected/disconnected events have rich source data), record it in
    # linked_pr too.
    if event_type in ("connected", "disconnected") and referenced_issue_id is not None:
        # We don't know which side is the issue and which is the PR
        # without inspecting both rows.
        a = conn.execute("SELECT is_pr FROM issue WHERE issue_id = ?", (issue_id,)).fetchone()
        b = conn.execute(
            "SELECT is_pr FROM issue WHERE issue_id = ?", (referenced_issue_id,)
        ).fetchone()
        if a and b:
            if a["is_pr"] and not b["is_pr"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO linked_pr (issue_id, pr_id, link_source)
                    VALUES (?, ?, 'event')
                    """,
                    (referenced_issue_id, issue_id),
                )
            elif not a["is_pr"] and b["is_pr"]:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO linked_pr (issue_id, pr_id, link_source)
                    VALUES (?, ?, 'event')
                    """,
                    (issue_id, referenced_issue_id),
                )
