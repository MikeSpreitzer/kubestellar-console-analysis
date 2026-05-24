# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""One-off diagnostic: compare the old vs. new searchsorted implementations
inside ``_heuristic_edges`` to localize why the rerun lost all heuristic
edges. Prints counts at each stage so we can see where the path diverges.

Run inside the container:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      console-analysis \
      diagnose_authorship_heuristic.py --config /config/config.yaml

Reads the database read-only; writes nothing.
"""

from __future__ import annotations

import argparse
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from src.common.config import load_config
from src.common.db import connect_readonly


def fetch_inputs(conn, repo_id):
    """Fetch the same issue and PR rows _heuristic_edges sees."""
    issue_rows = conn.execute(
        """
        SELECT
            i.issue_id     AS issue_id,
            i.closed_at    AS closed_at,
            i.closed_by_id AS issue_closer_id,
            i.author_id    AS issue_author_id
        FROM issue i
        WHERE i.repo_id = ?
          AND i.is_pr   = 0
          AND i.closed_at IS NOT NULL
          AND i.closed_by_id IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    issues = pd.DataFrame([dict(r) for r in issue_rows])

    pr_rows = conn.execute(
        """
        SELECT
            i.issue_id     AS pr_id,
            pr.merged_at   AS merged_at,
            pr.merged_by_id AS pr_merger_id,
            i.author_id    AS pr_author_id
        FROM pull_request pr
        JOIN issue i ON i.issue_id = pr.issue_id
        WHERE i.repo_id = ?
          AND pr.merged = 1
          AND pr.merged_at IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    prs = pd.DataFrame([dict(r) for r in pr_rows])
    return issues, prs


def prep(issues, prs):
    issues = issues.copy()
    prs = prs.copy()
    issues["closed_at_dt"] = pd.to_datetime(
        issues["closed_at"], utc=True, errors="coerce"
    )
    prs["merged_at_dt"] = pd.to_datetime(
        prs["merged_at"], utc=True, errors="coerce"
    )
    issues = issues.dropna(subset=["closed_at_dt"])
    prs = prs.dropna(subset=["merged_at_dt"])
    prs = prs.sort_values("merged_at_dt").reset_index(drop=True)
    return issues, prs


def count_old(issues, prs, window_minutes=5):
    """Original implementation: np.datetime64(timestamp ± window)."""
    window = pd.Timedelta(minutes=window_minutes)
    pr_times = prs["merged_at_dt"].values
    n_in_window = 0
    n_passing_closer_check = 0
    examples = []
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # squash the tz-naive warning
        for _, issue in issues.iterrows():
            t = issue["closed_at_dt"]
            lo = np.searchsorted(pr_times, np.datetime64(t - window))
            hi = np.searchsorted(pr_times, np.datetime64(t + window))
            for j in range(lo, hi):
                n_in_window += 1
                pr = prs.iloc[j]
                closer_id = issue["issue_closer_id"]
                if closer_id is None:
                    continue
                if (
                    closer_id != pr["pr_merger_id"]
                    and closer_id != pr["pr_author_id"]
                ):
                    continue
                n_passing_closer_check += 1
                if len(examples) < 3:
                    examples.append((issue["issue_id"], pr["pr_id"]))
    return n_in_window, n_passing_closer_check, examples


def count_new(issues, prs, window_minutes=5):
    """Current implementation: int64 nanoseconds-since-epoch.

    Mirrors the fix in src/analysis/authorship.py: the intermediate
    cast to ``datetime64[ns, UTC]`` is load-bearing because newer
    pandas preserves the source resolution (here ``datetime64[us, UTC]``)
    and a bare ``astype('int64')`` would yield microseconds while
    ``Timestamp.value`` is unconditionally nanoseconds.
    """
    window_ns = pd.Timedelta(minutes=window_minutes).value
    pr_times_ns = (
        prs["merged_at_dt"]
        .astype("datetime64[ns, UTC]")
        .astype("int64")
        .values
    )
    n_in_window = 0
    n_passing_closer_check = 0
    examples = []
    for _, issue in issues.iterrows():
        t_ns = issue["closed_at_dt"].value
        lo = np.searchsorted(pr_times_ns, t_ns - window_ns)
        hi = np.searchsorted(pr_times_ns, t_ns + window_ns)
        for j in range(lo, hi):
            n_in_window += 1
            pr = prs.iloc[j]
            closer_id = issue["issue_closer_id"]
            if closer_id is None:
                continue
            if (
                closer_id != pr["pr_merger_id"]
                and closer_id != pr["pr_author_id"]
            ):
                continue
            n_passing_closer_check += 1
            if len(examples) < 3:
                examples.append((issue["issue_id"], pr["pr_id"]))
    return n_in_window, n_passing_closer_check, examples


def report_array_dtypes(prs):
    print("--- Array / dtype probe ---")
    print(f"prs['merged_at_dt'].dtype = {prs['merged_at_dt'].dtype}")
    print(f"prs['merged_at_dt'].values dtype = {prs['merged_at_dt'].values.dtype}")
    try:
        as_int = prs["merged_at_dt"].astype("int64")
        print(f"astype('int64') ok; first 3 values: {as_int.head(3).tolist()}")
    except Exception as e:
        print(f"astype('int64') raised: {type(e).__name__}: {e}")
    try:
        first = prs["merged_at_dt"].iloc[0]
        print(f"first value: {first!r}, .value = {first.value}")
    except Exception as e:
        print(f"accessing first .value raised: {type(e).__name__}: {e}")
    print(f"pandas version = {pd.__version__}")
    print(f"numpy version  = {np.__version__}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True)
    p.add_argument("--owner", default="kubestellar")
    p.add_argument("--name", default="console")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    db_path = Path(cfg.data_dir) / "db.sqlite"
    conn = connect_readonly(db_path)
    try:
        repo_row = conn.execute(
            "SELECT repo_id FROM repo WHERE owner = ? AND name = ?",
            (args.owner, args.name),
        ).fetchone()
        if repo_row is None:
            print(f"no such repo: {args.owner}/{args.name}", file=sys.stderr)
            return 1
        repo_id = repo_row["repo_id"]
        issues, prs = fetch_inputs(conn, repo_id)
        print(f"raw issue rows: {len(issues)}")
        print(f"raw PR rows:    {len(prs)}")
        issues, prs = prep(issues, prs)
        print(f"after dropna: issues={len(issues)}, prs={len(prs)}")
        report_array_dtypes(prs)
        print()

        old_in, old_pass, old_ex = count_old(issues, prs)
        print(f"OLD path: {old_in} in window, {old_pass} pass closer check")
        print(f"  examples: {old_ex}")

        new_in, new_pass, new_ex = count_new(issues, prs)
        print(f"NEW path: {new_in} in window, {new_pass} pass closer check")
        print(f"  examples: {new_ex}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
