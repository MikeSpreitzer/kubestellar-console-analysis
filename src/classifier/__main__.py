# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""CLI entry point for the classifier.

Run as ``python -m src.classifier --config /config/config.yaml``.

Reads the database (read-write, since we INSERT into
producer_classification) and applies rules to every subject repo's
artifacts. Output: rows in producer_classification, one per
(target_kind, target_id, source, classifier_version) tuple.
"""

from __future__ import annotations

import argparse
import logging
import sys

from ..common.config import load_config
from ..common.db import checkpoint, connect, init_schema
from .main import CLASSIFIER_VERSION, classify_all
from .rules import rules_signature


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Producer classifier")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--repo", action="append",
        help="restrict to one subject repo (owner/name); may be repeated",
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    log = logging.getLogger("classifier")

    cfg = load_config(args.config)
    conn = connect(cfg.db_path)
    init_schema(conn)

    log.info(
        "running classifier version=%s rules_signature=%s",
        CLASSIFIER_VERSION, rules_signature(),
    )

    selected = set(args.repo) if args.repo else None
    try:
        counts = classify_all(conn, repo_filter=selected)
    finally:
        checkpoint(conn)
        conn.close()

    total = sum(n for kinds in counts.values() for n in kinds.values())
    log.info("classifier finished; wrote %d total rows across %d repos",
             total, len(counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
