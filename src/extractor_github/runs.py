# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Extract GitHub Actions workflow runs.

Two-phase approach:

1. **Metadata pass**: walk the runs endpoint paginated by ``created``
   ascending, upserting metadata. Cheap and the metadata is assumed to
   persist indefinitely.
2. **Logs/artifacts pass**: for runs whose ``logs_status`` is ``pending``
   and whose ``created_at`` is within the 90-day retention window,
   download the logs (and optionally artifacts) to disk.

Concurrency for the logs pass is bounded by ``log_fetch_concurrency``
to avoid GitHub's secondary rate limits.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from ..common.db import HourlyChecker, checkpoint, get_state, set_state, transaction
from ..common.github_client import GitHubClient
from ..common.registries import upsert_actor_from_api


log = logging.getLogger(__name__)


# GitHub's documented retention for logs and artifacts on public repos.
LOGS_RETENTION_DAYS = 90

# How often to checkpoint the WAL during the fetch loop. The fetch
# loop runs many UPDATEs in tight succession; without intermediate
# checkpoints the WAL grows indefinitely until phase end.
CHECKPOINT_EVERY_N = 200


def extract_workflow_runs(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
) -> int:
    """Pull workflow run metadata since the last watermark.

    Returns count of runs upserted.
    """
    state_key = f"{owner}/{name}:runs:created_after"
    since = get_state(conn, state_key)
    params: dict[str, Any] = {"per_page": 100}
    if since:
        # GitHub's runs endpoint accepts "created" as a search filter
        # using >= prefix.
        params["created"] = f">={since}"

    count = 0
    max_created_at: Optional[str] = since

    for run in gh.paginate_envelope(
        f"/repos/{owner}/{name}/actions/runs",
        items_key="workflow_runs",
        params=params,
    ):
        with transaction(conn):
            _upsert_run(conn, repo_id, run)
        c = run.get("created_at")
        if c and (max_created_at is None or c > max_created_at):
            max_created_at = c
        count += 1

    if max_created_at and max_created_at != since:
        set_state(conn, state_key, max_created_at)

    log.info("upserted %d workflow runs for %s/%s", count, owner, name)
    return count


def fetch_run_logs_and_artifacts(
    conn: sqlite3.Connection,
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    gh_runs_dir: Path,
    *,
    fetch_logs: bool = True,
    fetch_artifacts: bool = True,
    concurrency: int = 5,
    hourly_checker: Optional[HourlyChecker] = None,
) -> int:
    """Download logs and artifacts for runs that don't yet have them.

    Skips runs older than the retention window. Returns count of
    downloads attempted (successes + failures).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOGS_RETENTION_DAYS)
    cutoff_iso = cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")

    # First, mark too-old pending runs as expired so we don't keep
    # trying to fetch them.
    with transaction(conn):
        if fetch_logs:
            conn.execute(
                """
                UPDATE workflow_run
                SET logs_status = 'expired'
                WHERE repo_id = ? AND logs_status = 'pending' AND created_at < ?
                """,
                (repo_id, cutoff_iso),
            )
        if fetch_artifacts:
            conn.execute(
                """
                UPDATE workflow_run
                SET artifacts_status = 'expired'
                WHERE repo_id = ? AND artifacts_status = 'pending' AND created_at < ?
                """,
                (repo_id, cutoff_iso),
            )

    # Collect work items.
    log_targets = []
    artifact_targets = []
    if fetch_logs:
        log_targets = conn.execute(
            """
            SELECT run_id FROM workflow_run
            WHERE repo_id = ? AND logs_status = 'pending' AND created_at >= ?
            ORDER BY created_at ASC
            """,
            (repo_id, cutoff_iso),
        ).fetchall()
    if fetch_artifacts:
        artifact_targets = conn.execute(
            """
            SELECT run_id FROM workflow_run
            WHERE repo_id = ? AND artifacts_status = 'pending' AND created_at >= ?
            ORDER BY created_at ASC
            """,
            (repo_id, cutoff_iso),
        ).fetchall()

    log.info(
        "%s/%s: %d log targets, %d artifact targets within %d-day retention",
        owner, name, len(log_targets), len(artifact_targets), LOGS_RETENTION_DAYS,
    )

    attempted = 0
    if log_targets:
        attempted += _fetch_concurrently(
            gh, conn, owner, name, repo_id, gh_runs_dir,
            run_ids=[r["run_id"] for r in log_targets],
            kind="logs",
            concurrency=concurrency,
            hourly_checker=hourly_checker,
        )
    if artifact_targets:
        attempted += _fetch_concurrently(
            gh, conn, owner, name, repo_id, gh_runs_dir,
            run_ids=[r["run_id"] for r in artifact_targets],
            kind="artifacts",
            concurrency=concurrency,
            hourly_checker=hourly_checker,
        )
    return attempted


# ----------------------------------------------------------------------
# Internals
# ----------------------------------------------------------------------

def _upsert_run(
    conn: sqlite3.Connection,
    repo_id: int,
    run: dict[str, Any],
) -> None:
    actor_id = upsert_actor_from_api(conn, run.get("actor"))
    triggering_actor_id = upsert_actor_from_api(conn, run.get("triggering_actor"))

    conn.execute(
        """
        INSERT INTO workflow_run (
            run_id, repo_id, workflow_path, workflow_name,
            run_number, run_attempt, event, status, conclusion,
            head_sha, head_branch, actor_id, triggering_actor_id,
            created_at, run_started_at, updated_at,
            logs_status, artifacts_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 'pending')
        ON CONFLICT(run_id) DO UPDATE SET
            workflow_path = excluded.workflow_path,
            workflow_name = excluded.workflow_name,
            run_number = excluded.run_number,
            run_attempt = excluded.run_attempt,
            event = excluded.event,
            status = excluded.status,
            conclusion = excluded.conclusion,
            head_sha = excluded.head_sha,
            head_branch = excluded.head_branch,
            actor_id = COALESCE(excluded.actor_id, workflow_run.actor_id),
            triggering_actor_id =
                COALESCE(excluded.triggering_actor_id, workflow_run.triggering_actor_id),
            updated_at = excluded.updated_at
        """,
        (
            run["id"],
            repo_id,
            run.get("path") or "",
            run.get("name"),
            run.get("run_number"),
            run.get("run_attempt"),
            run.get("event"),
            run.get("status"),
            run.get("conclusion"),
            run.get("head_sha"),
            run.get("head_branch"),
            actor_id,
            triggering_actor_id,
            run["created_at"],
            run.get("run_started_at"),
            run.get("updated_at"),
        ),
    )


def _fetch_concurrently(
    gh: GitHubClient,
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    repo_id: int,
    gh_runs_dir: Path,
    *,
    run_ids: list[int],
    kind: str,  # 'logs' | 'artifacts'
    concurrency: int,
    hourly_checker: Optional[HourlyChecker] = None,
) -> int:
    """Fetch logs or artifacts for a list of run_ids in parallel.

    HTTP downloads run in worker threads via the pool. DB writes are
    serialized through the main thread (the body of as_completed) to
    keep all SQLite access on a single thread.
    """
    attempted = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _fetch_one,
                gh, owner, name, repo_id, run_id, gh_runs_dir, kind,
            ): run_id for run_id in run_ids
        }
        for fut in as_completed(futures):
            run_id = futures[fut]
            try:
                status, rel_path = fut.result()
            except Exception as exc:
                log.warning("%s fetch failed for run %d: %s", kind, run_id, exc)
                status, rel_path = ("error", None)
            with transaction(conn):
                if kind == "logs":
                    conn.execute(
                        """
                        UPDATE workflow_run
                        SET logs_status = ?, logs_path = ?
                        WHERE run_id = ?
                        """,
                        (status, rel_path, run_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE workflow_run
                        SET artifacts_status = ?, artifacts_path = ?
                        WHERE run_id = ?
                        """,
                        (status, rel_path, run_id),
                    )
            attempted += 1
            if attempted % CHECKPOINT_EVERY_N == 0:
                checkpoint(conn)
            if hourly_checker is not None and hourly_checker.maybe_check(conn):
                log.info(
                    "[%s/%s] hourly integrity check passed during %s fetch "
                    "(%d / %d done)",
                    owner, name, kind, attempted, len(run_ids),
                )
    return attempted


def _fetch_one(
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    run_id: int,
    gh_runs_dir: Path,
    kind: str,
) -> tuple[str, Optional[str]]:
    """Download logs or artifacts for one run.

    Returns (status, relative_path).
    """
    target_dir = gh_runs_dir / str(repo_id) / str(run_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    if kind == "logs":
        url = f"/repos/{owner}/{name}/actions/runs/{run_id}/logs"
        out_path = target_dir / "logs.zip"
        rel_path = str(Path(str(repo_id)) / str(run_id) / "logs.zip")
    else:
        # Artifacts: list, then download each one.
        return _fetch_artifacts(gh, owner, name, repo_id, run_id, target_dir)

    resp = gh.request("GET", url, stream=True, allow_redirects=True)
    if resp.status_code == 404:
        return ("unavailable", None)
    if resp.status_code == 410:
        return ("expired", None)
    resp.raise_for_status()

    tmp_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False, dir=target_dir, suffix=".tmp"
        ) as tmp:
            tmp_path = Path(tmp.name)
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    tmp.write(chunk)
        shutil.move(str(tmp_path), str(out_path))
        tmp_path = None
        return ("fetched", rel_path)
    finally:
        # Remove the temp file if we crashed before the rename. Avoids
        # accumulating *.tmp orphans across interrupted runs.
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _fetch_artifacts(
    gh: GitHubClient,
    owner: str,
    name: str,
    repo_id: int,
    run_id: int,
    target_dir: Path,
) -> tuple[str, Optional[str]]:
    """Download all artifacts for a run."""
    listing = gh.get_json(f"/repos/{owner}/{name}/actions/runs/{run_id}/artifacts")
    if listing is None:
        return ("unavailable", None)
    artifacts = listing.get("artifacts", [])
    if not artifacts:
        return ("fetched", None)  # no artifacts is success-with-empty

    artifacts_dir = target_dir / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)

    for art in artifacts:
        if art.get("expired"):
            continue
        art_id = art["id"]
        art_name = art.get("name", f"artifact-{art_id}").replace("/", "_")
        url = f"/repos/{owner}/{name}/actions/artifacts/{art_id}/zip"
        out_path = artifacts_dir / f"{art_id}-{art_name}.zip"
        resp = gh.request("GET", url, stream=True, allow_redirects=True)
        if resp.status_code in (404, 410):
            continue
        resp.raise_for_status()
        tmp_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False, dir=artifacts_dir, suffix=".tmp"
            ) as tmp:
                tmp_path = Path(tmp.name)
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        tmp.write(chunk)
            shutil.move(str(tmp_path), str(out_path))
            tmp_path = None
        finally:
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    rel_path = str(Path(str(repo_id)) / str(run_id) / "artifacts")
    return ("fetched", rel_path)
