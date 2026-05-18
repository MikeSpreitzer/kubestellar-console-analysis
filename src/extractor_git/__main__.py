# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Entry point for the git-history extractor.

Run as ``python -m src.extractor_git --config /config/config.yaml``.

Walks each configured repo with a ``local_clone`` set, populating
``commit_``, ``commit_file``, and ``workflow_file_state``. Repos
without a ``local_clone`` are skipped (they only get GitHub-API data).
Both ``subject`` and ``support`` repos are walked.
"""

from __future__ import annotations

import argparse
import logging
import sys

from pathlib import Path

from ..common.config import Config, load_config
from ..common.db import (
    HourlyChecker, IntegrityError, checkpoint, connect, init_schema,
    integrity_check_full, vacuum_into,
)
from . import walker


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


def _finish_run(conn, run_id: int, *, status: str, notes: str = "") -> None:
    conn.execute(
        """
        UPDATE extraction_run
        SET ended_at = datetime('now'),
            exit_status = ?,
            notes = ?
        WHERE run_id = ?
        """,
        (status, notes, run_id),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Git history extractor")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--repo",
        action="append",
        help="restrict to one repo (owner/name); may be repeated",
    )
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)
    log = logging.getLogger("extractor_git")

    cfg = load_config(args.config)
    cfg.data_dir.mkdir(parents=True, exist_ok=True)

    selected_slugs = set(args.repo) if args.repo else None
    targets = [
        r for r in cfg.repos
        if r.local_clone is not None
        and (selected_slugs is None or r.slug in selected_slugs)
    ]
    if not targets:
        log.error("no repos with local_clone configured (or filter excluded all)")
        return 2

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
                snap_path = (cfg.data_dir / "snapshots"
                             / f"{r.slug.replace('/', '_')}__before_git_walk.sqlite")
                log.info("[%s] snapshot before git walk -> %s", r.slug, snap_path.name)
                vacuum_into(conn, snap_path)

                walker.walk_repo(
                    conn,
                    repo_owner=r.owner,
                    repo_name=r.name,
                    repo_role=r.role,
                    repo_path=r.local_clone,
                )
                checkpoint(conn)
                log.info("[%s] integrity check after git walk", r.slug)
                integrity_check_full(conn)
            except IntegrityError as exc:
                log.error(
                    "INTEGRITY CHECK FAILED for %s: %s. Halting git extractor.",
                    r.slug, exc,
                )
                status = "error"
                notes_lines.append(f"integrity_check failed during {r.slug}: {exc}")
                halted_for_corruption = True
                break
            except Exception as exc:
                log.exception("git extraction failed for %s", r.slug)
                status = "partial"
                notes_lines.append(f"{r.slug}: {exc}")
    except Exception as exc:
        log.exception("fatal git extractor error")
        status = "error"
        notes_lines.append(f"fatal: {exc}")
    finally:
        _finish_run(conn, extraction_run_id, status=status, notes="\n".join(notes_lines))
        conn.close()

    if halted_for_corruption:
        log.error(
            "Git extractor halted due to integrity_check failure. "
            "Inspect data/snapshots/ for the most recent pre-walk backup."
        )

    log.info("git extractor finished status=%s", status)
    return 0 if status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
