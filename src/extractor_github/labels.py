# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Extract repository labels.

Labels are repo-scoped and small. We refresh the full set on each run;
upserts make this idempotent.
"""

from __future__ import annotations

import logging
import sqlite3

from ..common.db import transaction
from ..common.github_client import GitHubClient
from ..common.registries import upsert_label


log = logging.getLogger(__name__)


def extract_labels(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
) -> int:
    """Pull all labels for the repo. Returns count upserted."""
    count = 0
    with transaction(conn):
        for label in gh.paginate(f"/repos/{owner}/{name}/labels"):
            upsert_label(
                conn,
                repo_id=repo_id,
                name=label["name"],
                description=label.get("description"),
                color=label.get("color"),
            )
            count += 1
    log.info("upserted %d labels for %s/%s", count, owner, name)
    return count
