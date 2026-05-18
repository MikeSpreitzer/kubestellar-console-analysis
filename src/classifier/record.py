# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""The Record dataclass -- a uniform input shape for the classifier.

Each artifact (issue, PR, commit, comment, review) is presented to the
classifier as a Record. Adapters in ``adapters.py`` know the SQL to
fetch each kind from the database and yield Records; the classifier
core doesn't.

Fields are deliberately broader than any single artifact type needs --
``message`` is populated only for commits, ``labels`` only for
issues/PRs, etc. -- so a uniform predicate signature works across all
kinds. Predicates that branch on artifact-specific fields can guard
themselves with ``if r.target_kind == ...``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


VALID_TARGET_KINDS = ("issue", "pr", "commit", "comment", "review")


@dataclass(frozen=True)
class Record:
    target_kind: str               # one of VALID_TARGET_KINDS
    target_id: int                 # primary-key value in the source table

    # Identity. Any field may be None depending on availability.
    author_login: Optional[str]
    author_email: Optional[str]
    author_name: Optional[str]

    # Time of the artifact (creation for issue/pr/commit/comment/review).
    # Stored as the ISO 8601 string we keep in the database, not parsed
    # into datetime, since rule predicates compare on string ordering.
    created_at: str

    # Type-specific text. Only populated for the kinds that have them.
    title: Optional[str] = None    # issue, pr
    body: Optional[str] = None     # issue, pr, comment, review
    message: Optional[str] = None  # commit only

    # Labels. Empty for non-issue/pr.
    labels: tuple[str, ...] = ()

    # Review state. Only populated for kind='review'
    # ('APPROVED' | 'CHANGES_REQUESTED' | 'COMMENTED' | 'DISMISSED' | etc.)
    review_state: Optional[str] = None
