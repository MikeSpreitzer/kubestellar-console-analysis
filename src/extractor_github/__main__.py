# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0

"""Entry point for the GitHub extractor.

Run as ``python -m src.extractor_github --config /config/config.yaml``.

Orchestrates the per-source extraction passes for each subject
repository. ``support`` repos are skipped here; their content is
extracted only via the git-history extractor.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone

from ..common.config import Config, RepoConfig, load_config
from ..common.db import (
    HourlyChecker, IntegrityError, checkpoint, connect, init_schema,
    integrity_check_full, transaction, vacuum_into,
)
from ..common.github_client import GitHubClient
from ..common.registries import upsert_repo
from . import comments, issues, labels, pr_files, reactions, reviews, runs, timelines


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _start_run(conn) -> int:
    cur = conn.execute(
        "INSERT INTO extraction_run (started_at) VALUES (datetime('now'))"
    )
    return cur.lastrowid


def _finish_run(conn, run_id: int, *, status: str, gh: GitHubClient, notes: str = "") -> None:
    conn.execute(
        """
        UPDATE extraction_run
        SET ended_at = datetime('now'),
            exit_status = ?,
            notes = ?,
            api_calls_made = ?,
            rate_limit_waits = ?
        WHERE run_id = ?
        """,
        (status, notes, gh.stats.api_calls_made, gh.stats.rate_limit_waits, run_id),
    )


def _ensure_subject_repo(conn, gh: GitHubClient, repo: RepoConfig) -> int:
    """Add or update a row for a configured subject repo, populating
    default_branch from the API."""
    info = gh.get_json(f"/repos/{repo.owner}/{repo.name}")
    default_branch = info.get("default_branch") if info else None
    with transaction(conn):
        repo_id = upsert_repo(
            conn,
            owner=repo.owner,
            name=repo.name,
            role=repo.role,
            default_branch=default_branch,
        )
    return repo_id


def _snapshot_path(cfg: Config, repo_slug: str, phase_name: str) -> Path:
    """Path for a per-phase pre-snapshot of the database."""
    safe_slug = repo_slug.replace("/", "_")
    safe_phase = phase_name.replace(" ", "_").replace("/", "_")
    return cfg.data_dir / "snapshots" / f"{safe_slug}__before_{safe_phase}.sqlite"


def _phase_with_safety(
    conn,
    cfg: Config,
    repo_slug: str,
    phase_name: str,
    phase_fn,
) -> None:
    """Run one phase with snapshot-before, checkpoint-after, integrity-check.

    Raises IntegrityError (the underlying check raises) on corruption,
    halting the extractor. Snapshot is written first so recovery is
    possible if the phase corrupts the database. The snapshot file
    persists across the run so it can be inspected; only overwritten on
    the next run of the same phase.
    """
    log = logging.getLogger("extractor_github")
    snap_path = _snapshot_path(cfg, repo_slug, phase_name)
    log.info("[%s] snapshot before %s -> %s", repo_slug, phase_name, snap_path.name)
    vacuum_into(conn, snap_path)
    log.info("[%s] %s", repo_slug, phase_name)
    phase_fn()
    checkpoint(conn)
    log.info("[%s] integrity check after %s", repo_slug, phase_name)
    integrity_check_full(conn)


def run_for_subject(conn, gh: GitHubClient, repo: RepoConfig, cfg: Config) -> None:
    log = logging.getLogger("extractor_github")
    log.info("=== %s (%s) ===", repo.slug, repo.role)

    repo_id = _ensure_subject_repo(conn, gh, repo)

    hc = HourlyChecker(interval_seconds=3600.0)

    phases: list[tuple[str, callable]] = [
        ("labels",
         lambda: labels.extract_labels(conn, gh, repo.owner, repo.name, repo_id)),
        ("issues + PR stubs",
         lambda: issues.extract_issues(conn, gh, repo.owner, repo.name, repo_id)),
        ("PR detail enrichment",
         lambda: issues.enrich_pull_requests(
             conn, gh, repo.owner, repo.name, repo_id, hourly_checker=hc)),
        ("PR file lists",
         lambda: pr_files.extract_pr_files(
             conn, gh, repo.owner, repo.name, repo_id, hourly_checker=hc)),
        ("timelines",
         lambda: timelines.extract_timelines(
             conn, gh, repo.owner, repo.name, repo_id, hourly_checker=hc)),
        ("comments",
         lambda: comments.extract_comments(conn, gh, repo.owner, repo.name, repo_id)),
        ("reviews",
         lambda: reviews.extract_reviews(
             conn, gh, repo.owner, repo.name, repo_id, hourly_checker=hc)),
        ("reactions",
         lambda: reactions.extract_reactions(
             conn, gh, repo.owner, repo.name, repo_id, hourly_checker=hc)),
        ("workflow run metadata",
         lambda: runs.extract_workflow_runs(conn, gh, repo.owner, repo.name, repo_id)),
    ]
    for phase_name, phase_fn in phases:
        _phase_with_safety(conn, cfg, repo.slug, phase_name, phase_fn)

    if cfg.extraction.fetch_logs or cfg.extraction.fetch_artifacts:
        _phase_with_safety(
            conn, cfg, repo.slug, "workflow run logs and/or artifacts",
            lambda: runs.fetch_run_logs_and_artifacts(
                conn, gh, repo.owner, repo.name, repo_id,
                cfg.gh_runs_dir,
                fetch_logs=cfg.extraction.fetch_logs,
                fetch_artifacts=cfg.extraction.fetch_artifacts,
                concurrency=cfg.extraction.log_fetch_concurrency,
                hourly_checker=hc,
            ),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GitHub data extractor")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--repo",
        action="append",
        help="restrict to one subject repo (owner/name); may be repeated",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    log = logging.getLogger("extractor_github")

    cfg = load_config(args.config)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.gh_runs_dir.mkdir(parents=True, exist_ok=True)

    selected_slugs = set(args.repo) if args.repo else None
    targets = [
        r for r in cfg.repos
        if r.role in ("subject", "both")
        and (selected_slugs is None or r.slug in selected_slugs)
    ]
    if not targets:
        log.error("no subject repos configured (or filter excluded all of them)")
        return 2

    gh = GitHubClient(token=cfg.github_token)

    conn = connect(cfg.db_path)
    init_schema(conn)

    log.info("running full integrity check at startup")
    try:
        integrity_check_full(conn)
    except IntegrityError as exc:
        log.error("STARTUP INTEGRITY CHECK FAILED: %s", exc)
        log.error(
            "Database at %s appears to be corrupt. Investigate before "
            "re-running. Restoring from data/snapshots/ if available, or "
            "running 'sqlite3 <db> .recover' may help.",
            cfg.db_path,
        )
        conn.close()
        return 3
    log.info("startup integrity check passed")

    extraction_run_id = _start_run(conn)

    status = "success"
    notes_lines: list[str] = []
    halted_for_corruption = False
    try:
        for r in targets:
            try:
                run_for_subject(conn, gh, r, cfg)
            except IntegrityError as exc:
                log.error(
                    "INTEGRITY CHECK FAILED for %s: %s. Halting extractor.",
                    r.slug, exc,
                )
                status = "error"
                notes_lines.append(f"integrity_check failed during {r.slug}: {exc}")
                halted_for_corruption = True
                break
            except Exception as exc:
                log.exception("extraction failed for %s", r.slug)
                status = "partial"
                notes_lines.append(f"{r.slug}: {exc}")
    except Exception as exc:
        log.exception("fatal extractor error")
        status = "error"
        notes_lines.append(f"fatal: {exc}")
    finally:
        _finish_run(conn, extraction_run_id, status=status, gh=gh, notes="\n".join(notes_lines))
        conn.close()

    if halted_for_corruption:
        log.error(
            "Extractor halted due to integrity_check failure. "
            "Inspect data/snapshots/ for the most recent pre-phase backup; "
            "the snapshot taken before the failing phase is the best "
            "starting point for recovery."
        )

    log.info(
        "extractor finished status=%s api_calls=%d rate_waits=%d "
        "secondary_waits=%d network_retries=%d server_error_retries=%d",
        status, gh.stats.api_calls_made, gh.stats.rate_limit_waits,
        gh.stats.secondary_waits, gh.stats.network_retries,
        gh.stats.server_error_retries,
    )
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
