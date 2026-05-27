# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""One-off diagnostic: trace what happens to ``cross-referenced`` events
between the GitHub timeline API response and the issue_event INSERT in
``timelines.py``.

The database has zero rows with event_type='cross-referenced' even though
the API does return such events (confirmed for issue #2533 via gh).
This script walks the same code path the extractor walks but only logs;
it does not write to the database.

Run inside the container (no /output bind mount needed):

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -e GITHUB_TOKEN \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      console-analysis \
      diagnose_cross_referenced.py --config /config/config.yaml \
        --owner kubestellar --name console --number 2533
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.common.config import load_config
from src.common.db import connect_readonly
from src.common.github_client import GitHubClient


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--owner", default="kubestellar")
    p.add_argument("--name", default="console")
    p.add_argument(
        "--number",
        type=int,
        default=2533,
        help="GitHub-visible issue number to fetch the timeline for",
    )
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    token = cfg.require_github_token()
    gh = GitHubClient(token=token)

    print(f"--- fetching timeline for {args.owner}/{args.name}#{args.number} ---")
    print("using the same accept header the extractor uses:")
    print("  application/vnd.github.mockingbird-preview+json")

    events_path = f"/repos/{args.owner}/{args.name}/issues/{args.number}/timeline"
    events = list(
        gh.paginate(
            events_path,
            accept="application/vnd.github.mockingbird-preview+json",
        )
    )
    print(f"\nthe gh client returned {len(events)} events.")

    # Count event types as the client returned them.
    counts: dict[str, int] = {}
    for ev in events:
        et = ev.get("event") or "<missing>"
        counts[et] = counts.get(et, 0) + 1
    print("\nevent_type counts as returned by gh.paginate():")
    for et in sorted(counts, key=lambda k: -counts[k]):
        print(f"  {counts[et]:5d}  {et}")

    # Now walk just the cross-referenced events and report what
    # _insert_event would do with each one. We don't actually call
    # _insert_event because that opens a write connection; we
    # replicate the relevant logic.
    print("\n--- cross-referenced events in the response ---")
    cross = [ev for ev in events if ev.get("event") == "cross-referenced"]
    if not cross:
        print("(none)")
    else:
        # Open a read-only connection so we can replicate the lookup
        # the extractor does at timelines.py:220.
        db_path = Path(cfg.data_dir) / "db.sqlite"
        conn = connect_readonly(db_path)
        try:
            for i, ev in enumerate(cross):
                created_at = (
                    ev.get("created_at")
                    or ev.get("submitted_at")
                    or (ev.get("commit") or {}).get("date")
                )
                src = ev.get("source") or {}
                src_type = src.get("type")
                ref_issue = src.get("issue") or {}
                ref_repo = (ref_issue.get("repository") or {})
                ref_owner = (ref_repo.get("owner") or {}).get("login")
                ref_name = ref_repo.get("name")
                ref_number = ref_issue.get("number")
                print(f"\n  cross-referenced event #{i+1}:")
                print(f"    created_at:       {created_at!r}")
                print(f"    source.type:      {src_type!r}")
                print(f"    source.issue.repository.owner.login: {ref_owner!r}")
                print(f"    source.issue.repository.name:        {ref_name!r}")
                print(f"    source.issue.number:                 {ref_number!r}")
                if ref_owner and ref_name and ref_number:
                    row = conn.execute(
                        """
                        SELECT i.issue_id, i.is_pr, i.title
                        FROM issue i
                        JOIN repo r ON r.repo_id = i.repo_id
                        WHERE r.owner = ? AND r.name = ? AND i.number = ?
                        """,
                        (ref_owner, ref_name, ref_number),
                    ).fetchone()
                    if row:
                        print(
                            f"    -> local lookup HIT: issue_id={row['issue_id']}, "
                            f"is_pr={row['is_pr']}, title={row['title']!r}"
                        )
                    else:
                        print(
                            f"    -> local lookup MISS: "
                            f"{ref_owner}/{ref_name}#{ref_number} not in our issue table"
                        )
                else:
                    print("    -> source fields incomplete; lookup would be skipped")
                if created_at is None:
                    print("    *** would be silently dropped: created_at is None ***")

            # Also report whether THIS issue's row exists in the database,
            # so we can be sure the timeline-extraction phase ran for it.
            this_row = conn.execute(
                """
                SELECT i.issue_id
                FROM issue i
                JOIN repo r ON r.repo_id = i.repo_id
                WHERE r.owner = ? AND r.name = ? AND i.number = ?
                """,
                (args.owner, args.name, args.number),
            ).fetchone()
            print(f"\nlocal issue row for {args.owner}/{args.name}#{args.number}: "
                  f"{'present' if this_row else 'MISSING'}")
            if this_row:
                ev_count = conn.execute(
                    "SELECT COUNT(*) AS n FROM issue_event WHERE issue_id = ?",
                    (this_row["issue_id"],),
                ).fetchone()
                print(f"issue_event rows for this issue in our DB: {ev_count['n']}")
                stored_types = conn.execute(
                    """
                    SELECT event_type, COUNT(*) AS n
                    FROM issue_event
                    WHERE issue_id = ?
                    GROUP BY event_type
                    ORDER BY n DESC
                    """,
                    (this_row["issue_id"],),
                ).fetchall()
                print("event_type breakdown in our DB:")
                for r in stored_types:
                    print(f"  {r['n']:5d}  {r['event_type']}")
        finally:
            conn.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
