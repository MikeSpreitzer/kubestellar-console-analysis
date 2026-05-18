# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Classifier orchestrator.

Walks the configured repos, applies the rule list to every issue, PR,
commit, comment, and review, and writes a row per (target,
classifier_version, source) into ``producer_classification``.

Source is currently always ``marker`` (the only signal layer
implemented). The ``journal`` and ``workflow_run`` source values are
reserved in the schema for future enrichment.

Re-running with the same ``CLASSIFIER_VERSION`` replaces existing rows
of that version. Re-running with a new version (after bumping below)
adds new rows alongside the old, so verdicts can be compared.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Iterator

from ..common.db import transaction
from .adapters import ADAPTERS
from .record import Record
from .rules import classify, rules_signature


log = logging.getLogger(__name__)


# Bump when rules change. Use a short tag identifying the change.
# rules_signature() guards against forgetting; see check_signature().
#
# Version history:
#   v1 -- initial rule set: copilot, hive-{scanner,reviewer,merger},
#         project-bot, other-bot-app catch-all, default-human, unknown.
#   v2 -- add explicit producers for prow, netlify, dependabot, and
#         claude-app; add copilot-swe-agent[bot] under copilot; add
#         kubestellar-console-bot[bot] under project-bot. github-actions
#         remains in other-bot-app pending content-based splitting.
CLASSIFIER_VERSION = "v2"

# Source value written to producer_classification. The schema reserves
# 'journal' and 'workflow_run' for future enrichment from those data
# sources, but the current classifier reads only artifact-level markers.
SOURCE_MARKER = "marker"


def check_signature(conn: sqlite3.Connection) -> None:
    """Detect "rules changed but classifier_version did not."

    Stores ``classifier_version -> rules_signature`` in
    extraction_state. On a fresh classifier_version, records the
    current signature. On a re-run with the same version, asserts the
    signature matches; if not, raises with a clear error.
    """
    sig = rules_signature()
    key = f"classifier:{CLASSIFIER_VERSION}:rules_signature"
    row = conn.execute(
        "SELECT value FROM extraction_state WHERE key = ?", (key,),
    ).fetchone()
    if row is None:
        conn.execute(
            """
            INSERT INTO extraction_state (key, value, updated_at)
            VALUES (?, ?, datetime('now'))
            """,
            (key, sig),
        )
        log.info(
            "first run for classifier_version=%s; recorded signature %s",
            CLASSIFIER_VERSION, sig,
        )
        return
    if row["value"] != sig:
        raise RuntimeError(
            f"rules signature mismatch for classifier_version="
            f"{CLASSIFIER_VERSION}: stored signature is {row['value']}, "
            f"current rules hash to {sig}. Either revert your rule "
            f"changes or bump CLASSIFIER_VERSION to a new value."
        )


def classify_repo(
    conn: sqlite3.Connection,
    repo_id: int,
    repo_slug: str,
) -> dict[str, int]:
    """Classify all artifact kinds for a single subject repo.

    Returns a mapping of target_kind -> count of rows written.
    """
    # Delete prior rows for this version on artifacts of THIS repo, so
    # a re-run of the same version on the same repo cleanly replaces.
    # We can't delete by repo directly (producer_classification has no
    # repo_id), so we delete by target_kind+target_id pairs scoped to
    # the repo. SQLite handles this with subselects.
    with transaction(conn):
        for kind in ADAPTERS:
            if kind in ("issue", "pr"):
                conn.execute(
                    f"""
                    DELETE FROM producer_classification
                    WHERE classifier_version = ?
                      AND target_kind = ?
                      AND target_id IN (
                          SELECT issue_id FROM issue
                          WHERE repo_id = ? AND is_pr = ?
                      )
                    """,
                    (CLASSIFIER_VERSION, kind, repo_id, 1 if kind == "pr" else 0),
                )
            elif kind == "commit":
                conn.execute(
                    """
                    DELETE FROM producer_classification
                    WHERE classifier_version = ?
                      AND target_kind = 'commit'
                      AND target_id IN (
                          SELECT commit_id FROM commit_ WHERE repo_id = ?
                      )
                    """,
                    (CLASSIFIER_VERSION, repo_id),
                )
            elif kind == "comment":
                conn.execute(
                    """
                    DELETE FROM producer_classification
                    WHERE classifier_version = ?
                      AND target_kind = 'comment'
                      AND target_id IN (
                          SELECT c.comment_id FROM comment c
                          JOIN issue i ON i.issue_id = c.issue_id
                          WHERE i.repo_id = ?
                      )
                    """,
                    (CLASSIFIER_VERSION, repo_id),
                )
            elif kind == "review":
                conn.execute(
                    """
                    DELETE FROM producer_classification
                    WHERE classifier_version = ?
                      AND target_kind = 'review'
                      AND target_id IN (
                          SELECT r.review_id FROM review r
                          JOIN issue i ON i.issue_id = r.issue_id
                          WHERE i.repo_id = ?
                      )
                    """,
                    (CLASSIFIER_VERSION, repo_id),
                )

    counts: dict[str, int] = {}
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for kind, adapter in ADAPTERS.items():
        n = 0
        with transaction(conn):
            for record in adapter(conn, repo_id):
                verdict, _matched_rule = classify(record)
                conn.execute(
                    """
                    INSERT INTO producer_classification (
                        target_kind, target_id, source,
                        producer, sub_producer, basis,
                        classified_at, classifier_version
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.target_kind, record.target_id, SOURCE_MARKER,
                        verdict.producer, verdict.sub_producer, verdict.basis,
                        now, CLASSIFIER_VERSION,
                    ),
                )
                n += 1
        counts[kind] = n
        log.info("[%s] classified %d %s records", repo_slug, n, kind)
    return counts


def classify_all(
    conn: sqlite3.Connection,
    repo_filter: set[str] | None = None,
) -> dict[str, dict[str, int]]:
    """Classify every subject repo. Returns repo_slug -> kind -> count.

    Support repos are skipped: classification is only meaningful for
    artifacts that are subjects of analysis.
    """
    check_signature(conn)
    rows = conn.execute(
        """
        SELECT repo_id, owner || '/' || name AS slug
        FROM repo
        WHERE role IN ('subject', 'both')
        ORDER BY repo_id
        """
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        slug = r["slug"]
        if repo_filter and slug not in repo_filter:
            continue
        log.info("=== classifying %s ===", slug)
        out[slug] = classify_repo(conn, r["repo_id"], slug)
    return out
