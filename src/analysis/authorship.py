# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Authorship and orchestration mix: Issue -> PR producer mapping.

For each (issue, closing-PR) pair, classify the issue's author and the
closing PR's author by producer, then cross-tabulate.

Edge sources for the issue->PR relationship:

- ``linked_pr`` rows with ``link_source = 'pr_body_keyword'`` (populated
  by the GitHub extractor from ``closes``/``fixes``/``resolves``
  keywords in PR bodies).
- An in-memory heuristic added at analysis time: where an issue's
  ``closed_at`` is within HEURISTIC_WINDOW_MINUTES of a PR's
  ``merged_at`` and the issue's closer overlaps with the PR's merger
  or author. This catches issues closed by PRs that did not use the
  closing keywords.

The heuristic edges are kept distinct from the ``linked_pr`` ones in
the per-edge CSV so the reader can see how much of the cross-tab comes
from each source.

Outputs to ``output/{plots,html,csv}/<repo>/authorship_*``:

- ``authorship_issue_to_pr_crosstab.csv`` -- the cross-tab counts.
- ``authorship_issue_to_pr_crosstab.png`` and ``.html`` -- heatmap
  rendering of the cross-tab.
- ``authorship_issue_to_pr_edges.csv`` -- per-edge dump (one row per
  (issue, pr, link_source) tuple) for follow-up analysis.

Run as
``python -m src.analysis.authorship --config /config/config.yaml``.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path
from typing import Optional

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import plotly.graph_objects as go  # noqa: E402

from ..classifier.record import Record
from ..classifier.rules import (
    PRODUCER_HUMAN,
    PRODUCER_UNKNOWN,
    classify,
)
from ..common.config import load_config
from ..common.db import connect_readonly
from ._plotly_html import write_html_with_title


log = logging.getLogger(__name__)


# Window for the close-time heuristic: an issue closed within this many
# minutes of a PR being merged is a candidate edge if the closer
# matches the merger or author.
HEURISTIC_WINDOW_MINUTES = 5

# Display order for the producer axis. Producers not listed here are
# appended after these in alphabetical order.
PRODUCER_DISPLAY_ORDER = (
    PRODUCER_HUMAN,
    "copilot",
    "claude-app",
    "hive-scanner",
    "hive-reviewer",
    "hive-merger",
    "prow",
    "project-bot",
    "netlify",
    "dependabot",
    "other-bot-app",
    PRODUCER_UNKNOWN,
)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


def _classify_login(login: Optional[str]) -> str:
    """Return the producer label for a GitHub login."""
    rec = Record(
        target_kind="issue",
        target_id=0,
        author_login=login,
        author_email=None,
        author_name=None,
        created_at="",
    )
    verdict, _ = classify(rec)
    return verdict.producer


def _ordered_producers(values: list[str]) -> list[str]:
    """Return the unique values in PRODUCER_DISPLAY_ORDER first, then any
    extras alphabetically."""
    seen = set()
    out = []
    for p in PRODUCER_DISPLAY_ORDER:
        if p in values and p not in seen:
            out.append(p)
            seen.add(p)
    extras = sorted(set(values) - seen)
    return out + extras


# ----------------------------------------------------------------------
# Edge construction
# ----------------------------------------------------------------------

def _linked_edges(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    """Edges from the ``linked_pr`` table.

    Returns one row per (issue_id, pr_id) pair, with the link source
    reported as 'linked_pr_keyword' (the only currently-populated
    source in this database).
    """
    rows = conn.execute(
        """
        SELECT
            lp.issue_id     AS issue_id,
            lp.pr_id        AS pr_id,
            lp.link_source  AS link_source,
            i_iss.author_id AS issue_author_id,
            a_iss.login     AS issue_author_login,
            i_pr.author_id  AS pr_author_id,
            a_pr.login      AS pr_author_login,
            pr.merged_by_id AS pr_merger_id,
            a_mer.login     AS pr_merger_login,
            i_iss.closed_by_id AS issue_closer_id,
            a_clo.login     AS issue_closer_login
        FROM linked_pr lp
        JOIN issue i_iss      ON i_iss.issue_id = lp.issue_id
        JOIN issue i_pr       ON i_pr.issue_id  = lp.pr_id
        LEFT JOIN actor a_iss ON a_iss.actor_id = i_iss.author_id
        LEFT JOIN actor a_pr  ON a_pr.actor_id  = i_pr.author_id
        LEFT JOIN pull_request pr ON pr.issue_id = i_pr.issue_id
        LEFT JOIN actor a_mer ON a_mer.actor_id = pr.merged_by_id
        LEFT JOIN actor a_clo ON a_clo.actor_id = i_iss.closed_by_id
        WHERE i_iss.repo_id = ?
          AND i_iss.is_pr   = 0
          AND i_pr.is_pr    = 1
        """,
        (repo_id,),
    ).fetchall()
    df = pd.DataFrame([dict(r) for r in rows])
    if df.empty:
        return df
    df["edge_source"] = "linked_pr_keyword"
    return df


def _heuristic_edges(
    conn: sqlite3.Connection,
    repo_id: int,
    *,
    already_linked: set[tuple[int, int]],
    window_minutes: int,
) -> pd.DataFrame:
    """In-memory heuristic edges: issue closed within ``window_minutes``
    of a PR being merged, where the closer matches the PR merger or
    author. Edges already present in ``already_linked`` are skipped.
    """
    rows = conn.execute(
        """
        SELECT
            i.issue_id     AS issue_id,
            i.closed_at    AS closed_at,
            i.closed_by_id AS issue_closer_id,
            a_clo.login    AS issue_closer_login,
            i.author_id    AS issue_author_id,
            a_iss.login    AS issue_author_login
        FROM issue i
        LEFT JOIN actor a_clo ON a_clo.actor_id = i.closed_by_id
        LEFT JOIN actor a_iss ON a_iss.actor_id = i.author_id
        WHERE i.repo_id = ?
          AND i.is_pr   = 0
          AND i.closed_at IS NOT NULL
          AND i.closed_by_id IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    issues = pd.DataFrame([dict(r) for r in rows])
    if issues.empty:
        return issues

    pr_rows = conn.execute(
        """
        SELECT
            i.issue_id     AS pr_id,
            pr.merged_at   AS merged_at,
            pr.merged_by_id AS pr_merger_id,
            a_mer.login    AS pr_merger_login,
            i.author_id    AS pr_author_id,
            a_pr.login     AS pr_author_login
        FROM pull_request pr
        JOIN issue i ON i.issue_id = pr.issue_id
        LEFT JOIN actor a_mer ON a_mer.actor_id = pr.merged_by_id
        LEFT JOIN actor a_pr  ON a_pr.actor_id  = i.author_id
        WHERE i.repo_id = ?
          AND pr.merged = 1
          AND pr.merged_at IS NOT NULL
        """,
        (repo_id,),
    ).fetchall()
    prs = pd.DataFrame([dict(r) for r in pr_rows])
    if prs.empty:
        return prs

    issues["closed_at_dt"] = pd.to_datetime(
        issues["closed_at"], utc=True, errors="coerce"
    )
    prs["merged_at_dt"] = pd.to_datetime(
        prs["merged_at"], utc=True, errors="coerce"
    )
    issues = issues.dropna(subset=["closed_at_dt"])
    prs = prs.dropna(subset=["merged_at_dt"])

    window_ns = pd.Timedelta(minutes=window_minutes).value
    candidates = []
    # PRs sorted by merge time so we can binary-search per issue.
    # We search in int64 nanoseconds-since-epoch to sidestep numpy's
    # tz-naive datetime64 (which would either warn or silently drop
    # the UTC tz on the pandas Timestamps).
    #
    # The intermediate cast to ``datetime64[ns]`` is load-bearing:
    # newer pandas preserves the source resolution (the SQLite-derived
    # column lands as ``datetime64[us, UTC]``), and a bare
    # ``astype('int64')`` would return microseconds-since-epoch while
    # ``Timestamp.value`` and ``Timedelta.value`` are unconditionally
    # nanoseconds. The two sides of the searchsorted comparison must
    # share a unit.
    prs_sorted = prs.sort_values("merged_at_dt").reset_index(drop=True)
    pr_times_ns = (
        prs_sorted["merged_at_dt"]
        .astype("datetime64[ns, UTC]")
        .astype("int64")
        .values
    )

    for _, issue in issues.iterrows():
        t_ns = issue["closed_at_dt"].value
        lo = np.searchsorted(pr_times_ns, t_ns - window_ns)
        hi = np.searchsorted(pr_times_ns, t_ns + window_ns)
        for j in range(lo, hi):
            pr = prs_sorted.iloc[j]
            pair = (issue["issue_id"], pr["pr_id"])
            if pair in already_linked:
                continue
            closer_id = issue["issue_closer_id"]
            if closer_id is None:
                continue
            if closer_id != pr["pr_merger_id"] and closer_id != pr["pr_author_id"]:
                continue
            candidates.append({
                "issue_id":           issue["issue_id"],
                "pr_id":              pr["pr_id"],
                "issue_author_login": issue["issue_author_login"],
                "pr_author_login":    pr["pr_author_login"],
                "pr_merger_login":    pr["pr_merger_login"],
                "issue_closer_login": issue["issue_closer_login"],
                "edge_source":        "heuristic_close_time",
            })

    return pd.DataFrame(candidates)


def _build_edges(
    conn: sqlite3.Connection, repo_id: int
) -> pd.DataFrame:
    """Build the combined edge list. Each row has issue/PR producer
    columns ready for cross-tabulation."""
    linked = _linked_edges(conn, repo_id)
    already = set(
        (r.issue_id, r.pr_id) for r in linked.itertuples()
    ) if not linked.empty else set()
    heuristic = _heuristic_edges(
        conn, repo_id,
        already_linked=already,
        window_minutes=HEURISTIC_WINDOW_MINUTES,
    )
    edges = pd.concat([linked, heuristic], ignore_index=True, sort=False)
    if edges.empty:
        return edges
    edges["issue_producer"] = edges["issue_author_login"].apply(_classify_login)
    edges["pr_producer"]    = edges["pr_author_login"].apply(_classify_login)
    return edges


# ----------------------------------------------------------------------
# Plotting
# ----------------------------------------------------------------------

def _crosstab(edges: pd.DataFrame) -> pd.DataFrame:
    """Counts of (issue_producer, pr_producer) pairs."""
    if edges.empty:
        return pd.DataFrame()
    ct = (
        edges.groupby(["issue_producer", "pr_producer"])
             .size()
             .unstack(fill_value=0)
             .sort_index()
    )
    rows = _ordered_producers(list(ct.index))
    cols = _ordered_producers(list(ct.columns))
    return ct.reindex(index=rows, columns=cols, fill_value=0)


def _plot_heatmap(
    ct: pd.DataFrame,
    title: str,
    out_png: Path,
    out_html: Path,
    tab_title: str,
) -> None:
    if ct.empty:
        log.warning("no data for %s", title)
        return

    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_html.parent.mkdir(parents=True, exist_ok=True)

    # PNG (matplotlib)
    fig, ax = plt.subplots(figsize=(max(7, len(ct.columns) * 0.9 + 3),
                                    max(5, len(ct.index) * 0.5 + 2)))
    values = ct.values
    im = ax.imshow(values, aspect="auto", cmap="viridis")
    ax.set_xticks(range(len(ct.columns)))
    ax.set_xticklabels(ct.columns, rotation=45, ha="right")
    ax.set_yticks(range(len(ct.index)))
    ax.set_yticklabels(ct.index)
    ax.set_xlabel("PR author producer")
    ax.set_ylabel("issue author producer")
    ax.set_title(title)
    for i in range(values.shape[0]):
        for j in range(values.shape[1]):
            v = values[i, j]
            if v == 0:
                continue
            color = "white" if v < values.max() / 2 else "black"
            ax.text(j, i, str(int(v)), ha="center", va="center",
                    color=color, fontsize=8)
    fig.colorbar(im, ax=ax, label="edge count")
    fig.tight_layout()
    fig.savefig(out_png, dpi=120)
    plt.close(fig)
    log.info("wrote %s", out_png)

    # HTML (plotly)
    pfig = go.Figure(data=go.Heatmap(
        z=values,
        x=list(ct.columns),
        y=list(ct.index),
        colorscale="Viridis",
        hovertemplate=(
            "issue producer: %{y}<br>"
            "PR producer: %{x}<br>"
            "count: %{z}<extra></extra>"
        ),
    ))
    pfig.update_layout(
        title=title,
        xaxis_title="PR author producer",
        yaxis_title="issue author producer",
        template="plotly_white",
    )
    write_html_with_title(pfig, out_html, tab_title)
    log.info("wrote %s", out_html)


# ----------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------

def run_for_repo(
    conn: sqlite3.Connection,
    owner: str,
    name: str,
    output_dir: Path,
) -> None:
    repo_row = conn.execute(
        "SELECT repo_id FROM repo WHERE owner = ? AND name = ?",
        (owner, name),
    ).fetchone()
    if repo_row is None:
        log.warning("repo %s/%s not in database; skipping", owner, name)
        return
    repo_id = repo_row["repo_id"]

    edges = _build_edges(conn, repo_id)
    if edges.empty:
        log.warning("no issue->PR edges for %s/%s", owner, name)
        return

    safe_slug = f"{owner}_{name}"
    plots = output_dir / "plots" / safe_slug
    htmls = output_dir / "html" / safe_slug
    csvs  = output_dir / "csv"  / safe_slug

    edges_out = edges[[
        "issue_id", "pr_id", "edge_source",
        "issue_author_login", "issue_producer",
        "pr_author_login", "pr_producer",
    ]].sort_values(["issue_id", "pr_id"])
    csvs.mkdir(parents=True, exist_ok=True)
    edges_path = csvs / "authorship_issue_to_pr_edges.csv"
    edges_out.to_csv(edges_path, index=False)
    log.info("wrote %s (%d edges)", edges_path, len(edges_out))

    ct = _crosstab(edges)
    crosstab_path = csvs / "authorship_issue_to_pr_crosstab.csv"
    ct.to_csv(crosstab_path)
    log.info("wrote %s", crosstab_path)

    title = f"Issue author producer x PR author producer ({owner}/{name})"
    _plot_heatmap(
        ct,
        title=title,
        out_png=plots / "authorship_issue_to_pr_crosstab.png",
        out_html=htmls / "authorship_issue_to_pr_crosstab.html",
        tab_title=f"{name}: issue->PR producer crosstab",
    )

    by_source = (
        edges.groupby("edge_source").size().rename("edges").reset_index()
    )
    log.info(
        "[%s/%s] edges by source: %s",
        owner, name, by_source.to_dict(orient="records"),
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", required=True, help="path to config.yaml")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    _setup_logging(args.verbose)

    cfg = load_config(args.config)
    db_path = Path(cfg.data_dir) / "db.sqlite"
    output_dir = Path(cfg.output_dir)

    conn = connect_readonly(db_path)
    try:
        for repo in cfg.repos:
            if repo.role not in ("subject", "both"):
                continue
            log.info("authorship: %s/%s", repo.owner, repo.name)
            run_for_repo(conn, repo.owner, repo.name, output_dir)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
