<!--
Copyright 2026 Mike Spreitzer
SPDX-License-Identifier: Apache-2.0
Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).
-->

# console-analysis

Analysis of [kubestellar/console](https://github.com/kubestellar/console)
(and later [kubestellar/docs](https://github.com/kubestellar/docs)) drawing
on artifacts available from Git and GitHub. The initial goal is to
characterize the split between human and agentic development over the
short, rapidly-evolving history of these repositories. A later goal,
to the degree the artifacts support it, is to characterize the quality
of the work being developed.

See [`DESIGN.md`](DESIGN.md) for the methodology, layered architecture,
and the explicit list of what this analysis cannot measure.
See [`SCHEMA.md`](SCHEMA.md) for the data model.

## Prerequisites

- A GitHub fine-grained personal access token with "Public Repositories
  (read-only)" access. Create it at
  https://github.com/settings/personal-access-tokens.
- Local git clones of the subject and support repositories
  (e.g. `kubestellar/console`, `kubestellar/infra`).
- Either Docker (recommended for sandboxing), or Python 3.11+ with `git`
  installed locally.

## Setup

Copy the example config and edit:

    cp config.yaml.example config.yaml
    # edit config.yaml to match your local layout

The paths in `config.yaml.example` (`/data`, `/output`, `/repos/...`)
are the paths the Python process sees **inside the container**. They
are the destination side of the bind mounts in the `docker run`
invocation below. If you intend to run on the host directly without
Docker, change these to host-side paths instead (see "Running directly
without Docker").

**Important**: create `config.yaml` as a regular file before the first
`docker run`. If you run docker with `-v .../config.yaml:...` against a
nonexistent host file, Docker silently creates an empty *directory*
there as the bind-mount source, and subsequent runs will fail with
`IsADirectoryError`. If that happens, `rmdir config.yaml` and re-create
it from the example.

Export the PAT:

    export GITHUB_TOKEN=github_pat_...

## Running in Docker (recommended)

Build the image:

    docker build -t console-analysis .

Run an extraction. Bind-mount the analysis repo (so the container sees
`config.yaml`), the data directory, and the parent of your local git
clones (read-only); pass the PAT. Use
`--user "$(id -u):$(id -g)"` so files written into the bind-mounted
directories are owned by your host user. The example assumes the
analysis repo and its sibling clones (`console`, `infra`, …) live
under a common parent directory.

The GitHub extractor pulls issues, PRs, timelines, reviews,
comments, reactions, and workflow run metadata via the GitHub REST
API:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -e GITHUB_TOKEN \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      console-analysis \
      -m src.extractor_github --config /config/config.yaml --verbose

`$(cd .. && pwd)` resolves to the absolute path of the parent of the
analysis repo. The names of the sibling clones inside that parent
(`console`, `infra`, etc.) must match the trailing path component of
each `local_clone` value in `config.yaml` (the example uses
`/repos/console`, `/repos/infra`).

The git extractor walks the local git clones and doesn't talk to
GitHub, so `GITHUB_TOKEN` is not needed; in exchange it needs the
`/repos` bind mount so the container can read those clones:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(cd .. && pwd):/repos:ro" \
      console-analysis \
      -m src.extractor_git --config /config/config.yaml --verbose

## Running the classifier

The classifier walks every subject repo's issues, PRs, commits,
comments, and reviews, applies a shared rule list (see
`src/classifier/rules.py`), and writes verdicts to the
`producer_classification` table. Required before analyses that join
to that table; the existing analysis modules also import from
`src/classifier/rules` directly during plotting -- `first_look`,
`drilldown`, and `commit_authorship` use it for the coarse
credential class, while `authorship`, `speed`, and
`resolution_quality` use the full producer taxonomy. Equivalent to
joining to `producer_classification` because the rule list is the
same; a query that wanted verdicts at classifier-version granularity
would need to read the table.

This is a write operation on the database, so the data mount is RW
and we use the same UID-mapping pattern as the extractors:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      console-analysis \
      -m src.classifier --config /config/config.yaml --verbose

Re-running with the same `CLASSIFIER_VERSION` (defined at the top of
`src/classifier/main.py`) replaces existing rows for that version.
Re-running with a new version adds rows alongside; old verdicts are
preserved so versions can be compared.

### Inspecting the classifier output

The classifier writes one row per `(target_kind, target_id, source,
classifier_version)` tuple to the
[`producer_classification`](SCHEMA.md#producer_classification) table.
A few example queries (run on the host with `sqlite3 data/db.sqlite`;
substitute `'v3'` with whatever version the classifier last ran with):

Producer breakdown across all artifact kinds:

    SELECT producer, COUNT(*) n
    FROM producer_classification
    WHERE classifier_version = 'v3'
    GROUP BY producer
    ORDER BY n DESC;

Producer breakdown for one kind:

    SELECT producer, COUNT(*) n
    FROM producer_classification
    WHERE classifier_version = 'v3' AND target_kind = 'pr'
    GROUP BY producer
    ORDER BY n DESC;

Sub-producer detail within `other-bot-app`:

    SELECT sub_producer, COUNT(*) n
    FROM producer_classification
    WHERE classifier_version = 'v3' AND producer = 'other-bot-app'
    GROUP BY sub_producer
    ORDER BY n DESC;

Compare two classifier versions on the same artifact:

    SELECT a.producer AS old_producer, b.producer AS new_producer, COUNT(*) n
    FROM producer_classification a
    JOIN producer_classification b
      ON a.target_kind = b.target_kind
     AND a.target_id = b.target_id
     AND a.source = b.source
    WHERE a.classifier_version = 'v2' AND b.classifier_version = 'v3'
    GROUP BY a.producer, b.producer
    ORDER BY n DESC;

Join classification back to the source artifact (PRs example):

    SELECT i.number, i.title, pc.producer, pc.sub_producer
    FROM issue i
    JOIN producer_classification pc
      ON pc.target_kind = 'pr' AND pc.target_id = i.issue_id
    WHERE i.is_pr = 1
      AND pc.classifier_version = 'v3'
      AND pc.producer = 'hive-bot'
    LIMIT 20;

The other artifact kinds use `target_kind = 'issue'`, `'commit'`,
`'comment'`, or `'review'` and join to the corresponding source
table (see [`SCHEMA.md`](SCHEMA.md) for which key field each
`target_id` references).

## Running the analysis layer

The analysis modules read from the local sqlite database (via a
read-only connection) and write to `output/`. They don't talk to
GitHub or to the git clones, so neither `GITHUB_TOKEN` nor the
`/repos` bind mount is needed.

`first_look` produces six daily-binned plots of bot- vs.
human-credentialed activity (issue creation, issue closure, PR
creation, PR merging, comments on issues, comments on PRs):

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(pwd)/output:/output" \
      console-analysis \
      -m src.analysis.first_look --config /config/config.yaml --verbose

`drilldown` produces follow-up artifacts: per-bot-account issue
producers, post-cutoff human PR authors, and a window of PRs spanning
an apparent transition date:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(pwd)/output:/output" \
      console-analysis \
      -m src.analysis.drilldown --config /config/config.yaml --verbose

The drilldown takes optional `--cutoff YYYY-MM-DD` (default
2026-05-03) and `--window-days N` (default 5).

`commit_authorship` analyzes credentials at commit granularity using
the git-extractor's `commit_` table. It produces a daily commits-by-
credential plot, a PR-vs-commit-author cross-tab (surfacing the
"bot-opened PR with human commit-author" pattern), and a per-bot-email
commit producer plot. Requires the git extractor to have run first:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(pwd)/output:/output" \
      console-analysis \
      -m src.analysis.commit_authorship --config /config/config.yaml --verbose

The three modules above use the coarse bot-vs-human credential
classification. The three modules below use the full producer
taxonomy from `src/classifier/rules.py` (`human-credentialed`,
`copilot`, `claude-app`, `hive-scanner`, `hive-reviewer`,
`hive-bot`, `prow`, `project-bot`, `netlify`, `dependabot`,
`other-bot-app`, `unknown`).

`authorship` produces two Issue→PR producer cross-tabs (each as a
heatmap PNG + Plotly HTML + CSV) plus a per-edge CSV. The first
cross-tab covers the full repo history; the second is restricted to
edges whose closing PR's `merged_at` is on or after a configurable
cutoff (`--start-date YYYY-MM-DD`, default `2026-05-03`), intended
to bracket the L5→L6 hive handoff so the post-handoff shape isn't
swamped by the pre-handoff history.

Edges come from `linked_pr` rows tagged `pr_body_keyword` (the
GitHub extractor's `closes`/`fixes`/`resolves` keyword scan) plus
an in-memory close-time heuristic where the issue's `closed_at` is
within 5 minutes of a PR's `merged_at` and the closer matches the
PR merger or author. Edge sources are kept distinct in the per-edge
CSV so the reader can see how much of either cross-tab comes from
each source:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(pwd)/output:/output" \
      console-analysis \
      -m src.analysis.authorship --config /config/config.yaml --verbose

To inspect the (issue, PR) tuples behind a particular cell in either
cross-tab, filter the per-edge CSV. Each row is one
(issue, PR, edge_source) tuple with both endpoints' producer
classifications already computed. Columns are: `issue_id`,
`issue_number`, `pr_id`, `pr_number`, `edge_source`,
`pr_merged_at`, `issue_author_login`, `issue_producer`,
`pr_author_login`, `pr_producer`. For example, to list edges where
the issue producer is `hive-bot` and the PR producer is `copilot`:

    awk -F, 'NR==1 || ($8=="hive-bot" && $10=="copilot")' \
        output/csv/kubestellar_console/authorship_issue_to_pr_edges.csv

The `edge_source` column distinguishes `linked_pr_keyword` edges
(from `closes`/`fixes`/`resolves` keywords in PR bodies) from
`heuristic_close_time` edges (the 5-minute-window heuristic).
`pr_merged_at` is the closing PR's merge timestamp, so the same
file can be re-windowed without re-running the module (e.g.,
`awk -F, '$6 >= "2026-04-01"' ...`).
`issue_number` and `pr_number` are the GitHub-visible numbers (so a
row can be looked up directly on the GitHub UI); `issue_id` and
`pr_id` are the analysis database's primary keys.

`speed` produces weekly time-series speed-and-cadence metrics. Per
DESIGN.md these are speed metrics, not quality metrics, and the
corpus is non-stationary on a timescale of weeks (six ACMM-paper
era transitions in five months), so all outputs are weekly time
series with era-boundary annotations rather than aggregates over
the full window. Per-issue / per-PR values are placed in a weekly
bin by the closing PR's `merged_at`, with a fallback to the issue's
`closed_at` for issues closed without a linked merged PR. Optional
`--fast-close-threshold-minutes` (default `5`) sets the threshold
for the fast-close count metric:

  - `speed_issue_to_first_linked_pr` — weekly median by issue
    producer of `issue.created_at -> first-linked-PR.merged_at`.
  - `speed_pr_open_to_merge` — weekly median by PR producer of
    `pr.created_at -> pr.merged_at`.
  - `speed_fast_close` — weekly count of issues closed within
    `--fast-close-threshold-minutes` minutes of being opened,
    stacked by closer producer.
  - `speed_mttr_{first_close,cumulative_open,final_close}_{median,mean}_{pr,no_pr}`
    — twelve charts (three methodologies × two statistics × two
    closure paths: issues closed by a linked merged PR vs. issues
    closed without one). Each is a weekly line per closer producer.
    The per-issue CSV `speed_mttr_per_issue.csv` carries all three
    methodology values plus `reopen_count` and `closed_by_pr` for
    follow-up analysis.

```
    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(pwd)/output:/output" \
      console-analysis \
      -m src.analysis.speed --config /config/config.yaml --verbose
```

### Volume calibration via direct SQL

Two ad-hoc SQL queries against `data/db.sqlite` characterize weekly
work volume — one for the issue lifecycle, one for the merged-PR
lifecycle. They aren't part of any analysis module; the recipes
live here so you can run and re-run them directly. Both inform how
to read the speed and resolution-quality plots that follow: the
issue table shows what fraction of issues ever get reopened (an
upper bound on the population-wide reopen-channel signal), and the
PR table shows how much work happens outside the issue lifecycle
(merged PRs that close zero issues).

A recorded snapshot of both query outputs lives in
[CALIBRATION.md](CALIBRATION.md). The recipes below regenerate it.

The issue-lifecycle query reports five weekly signals side by side.
Column names are kept terse so the output fits an 80-column
terminal; the legend below names what each column measures.

| Column | Meaning |
|---|---|
| `week_start` | Monday of the week (W-SUN anchor, matches `to_week`). |
| `reopens` | Reopen events that occurred that week. Population-wide upper bound on the high-precision reopen plot in `resolution_quality`. Event-keyed. |
| `c_reopened` | Issues created that week that have ever been reopened. Cohort-keyed. |
| `c_quiet` | Issues created that week that have never been reopened. Cohort-keyed. |
| `c_human` | Issues created that week by a human-credentialed actor (v3 classifier label; upper bound on actual human work since humans may run automation under their own credentials). Cohort-keyed. |
| `n_humans` | Distinct human authors among the `c_human` issues that week. Distinguishes "few humans, many issues each" from "many humans, few each." |

The two kinds of weekly signal share one x-axis but mean different
things: `reopens` is keyed on the week the reopen *happened*; every
`c_*` and `n_*` column is keyed on the week the issue was *created*.
They will not align row-by-row.

    sqlite3 data/db.sqlite <<'EOF'
    .headers on
    .mode column
    WITH reopen_events AS (
      SELECT date(ie.created_at, 'weekday 0', '-6 days') AS week_start,
             COUNT(*) AS reopens
      FROM issue_event ie
      JOIN issue i ON i.issue_id = ie.issue_id
      WHERE i.repo_id = (
              SELECT repo_id FROM repo
              WHERE owner='kubestellar' AND name='console'
            )
        AND i.is_pr = 0
        AND ie.event_type = 'reopened'
      GROUP BY week_start
    ),
    issue_cohorts AS (
      SELECT
          date(i.created_at, 'weekday 0', '-6 days') AS week_start,
          SUM(CASE WHEN EXISTS (
                  SELECT 1 FROM issue_event ie
                  WHERE ie.issue_id = i.issue_id
                    AND ie.event_type = 'reopened'
              ) THEN 1 ELSE 0 END) AS c_reopened,
          SUM(CASE WHEN NOT EXISTS (
                  SELECT 1 FROM issue_event ie
                  WHERE ie.issue_id = i.issue_id
                    AND ie.event_type = 'reopened'
              ) THEN 1 ELSE 0 END) AS c_quiet,
          SUM(CASE WHEN EXISTS (
                  SELECT 1 FROM producer_classification pc
                  WHERE pc.target_kind = 'issue'
                    AND pc.target_id = i.issue_id
                    AND pc.classifier_version = 'v3'
                    AND pc.producer = 'human-credentialed'
              ) THEN 1 ELSE 0 END) AS c_human,
          COUNT(DISTINCT CASE WHEN EXISTS (
                  SELECT 1 FROM producer_classification pc
                  WHERE pc.target_kind = 'issue'
                    AND pc.target_id = i.issue_id
                    AND pc.classifier_version = 'v3'
                    AND pc.producer = 'human-credentialed'
              ) THEN i.author_id END) AS n_humans
      FROM issue i
      WHERE i.repo_id = (
              SELECT repo_id FROM repo
              WHERE owner='kubestellar' AND name='console'
            )
        AND i.is_pr = 0
      GROUP BY week_start
    )
    SELECT
        COALESCE(re.week_start, ic.week_start) AS week_start,
        COALESCE(re.reopens, 0)                AS reopens,
        COALESCE(ic.c_reopened, 0)             AS c_reopened,
        COALESCE(ic.c_quiet, 0)                AS c_quiet,
        COALESCE(ic.c_human, 0)                AS c_human,
        COALESCE(ic.n_humans, 0)               AS n_humans
    FROM reopen_events re
    FULL OUTER JOIN issue_cohorts ic ON re.week_start = ic.week_start
    ORDER BY week_start;
    EOF

The `weekday 0` / `-6 days` modifiers floor to Monday, matching the
W-SUN week anchor used by `to_week` in the analysis pipeline, so
this output overlays cleanly on the weekly speed and
resolution-quality charts.

A few caveats to keep in mind when reading the table:

- The query uses `FULL OUTER JOIN`, supported by SQLite ≥ 3.39
  (Feb 2022). Whichever environment you run the query in needs at
  least that version; macOS Sonoma+ and recent Linux distributions
  do, but older `sqlite3` binaries do not. On an older install the
  join would need to be expressed as two `LEFT JOIN`s unioned
  together.
- `c_reopened` is *retroactive*: it depends on whether a future
  event flips the issue's state. The most recent weeks will
  undercount because there hasn't been enough time for reopens
  that haven't happened yet. The column is most reliable for older
  weeks where the reopen window has had time to play out.
- `c_reopened + c_quiet` equals the total issues created that week
  (the denominator ``first_look``'s issues-opened plot already
  shows). If you want a per-week reopen rate, that's
  `c_reopened / (c_reopened + c_quiet)` computed inline.
- `c_human` and `n_humans` depend on the classifier having been
  run at the v3 version. If `producer_classification` is empty for
  that version, both columns will be zero — bump the classifier
  first.

The merged-PR-lifecycle query characterizes how much work happens
outside the issue lifecycle by looking at merged PRs and how many
issues each one closes. The columns are per-week distributions of
issues-closed-by-PR. The legend below names what each column
measures.

| Column | Meaning |
|---|---|
| `week_start` | Monday of the PR's `created_at` week (W-SUN anchor). |
| `merged` | Merged PRs created that week. |
| `n0`..`n4` | Merged PRs created that week that closed exactly 0, 1, 2, 3, 4 issues respectively. |
| `n5+` | Merged PRs that closed 5 or more issues. |
| `mean_isc` | Mean issues closed per merged PR, denominator restricted to PRs that closed at least one (i.e., `n0` excluded). |

`n0` is the "PR that landed work without closing an issue" bucket —
the natural counterpart to "issues created that week" in the issue
table. A high `n0` count says a lot of work this week happened
outside the issue lifecycle.

    sqlite3 data/db.sqlite <<'EOF'
    .headers on
    .mode column
    WITH pr_close_counts AS (
      SELECT
          pr.issue_id AS pr_id,
          date(pr_iss.created_at, 'weekday 0', '-6 days') AS week_start,
          (SELECT COUNT(*) FROM linked_pr lp
            WHERE lp.pr_id = pr.issue_id) AS n_closed
      FROM pull_request pr
      JOIN issue pr_iss ON pr_iss.issue_id = pr.issue_id
      WHERE pr_iss.repo_id = (
              SELECT repo_id FROM repo
              WHERE owner='kubestellar' AND name='console'
            )
        AND pr_iss.is_pr = 1
        AND pr.merged = 1
    )
    SELECT
        week_start,
        COUNT(*)                                          AS merged,
        SUM(CASE WHEN n_closed = 0 THEN 1 ELSE 0 END)     AS n0,
        SUM(CASE WHEN n_closed = 1 THEN 1 ELSE 0 END)     AS n1,
        SUM(CASE WHEN n_closed = 2 THEN 1 ELSE 0 END)     AS n2,
        SUM(CASE WHEN n_closed = 3 THEN 1 ELSE 0 END)     AS n3,
        SUM(CASE WHEN n_closed = 4 THEN 1 ELSE 0 END)     AS n4,
        SUM(CASE WHEN n_closed >= 5 THEN 1 ELSE 0 END)    AS [n5+],
        ROUND(
            CAST(SUM(CASE WHEN n_closed >= 1 THEN n_closed ELSE 0 END)
                 AS REAL)
            / NULLIF(SUM(CASE WHEN n_closed >= 1 THEN 1 ELSE 0 END), 0),
            2
        )                                                 AS mean_isc
    FROM pr_close_counts
    GROUP BY week_start
    ORDER BY week_start;
    EOF

Caveats:

- `merged = n0 + n1 + n2 + n3 + n4 + n5+` by construction.
- `mean_isc` is `NULL` for weeks where every merged PR closed zero
  issues (denominator zero); read those rows as "no closing PRs to
  average over," not as "0 issues closed on average."
- "Closes an issue" here is the count of `linked_pr` rows referencing
  this PR — populated from `closes/fixes/resolves` keywords in PR
  bodies plus GitHub's own linked-issue events. A PR that closed an
  issue without either signal is not counted; that's a known
  data-completeness limit of the extraction layer rather than this
  query.
- The `[n5+]` column-name uses square-bracket quoting because `+`
  isn't a legal bare identifier in SQL.

`resolution_quality` produces weekly time-series resolution-quality
signals: high-precision/low-recall (reopen by original reporter;
same-reporter follow-up citing the closing PR) and
low-precision/higher-recall (post-close phrase matches in `explicit`
and `general` tiers; cross-reference events). Each metric is binned
weekly by its signal trigger (reopen `created_at`, follow-up issue
`created_at`, comment `commented_at`, cross-reference `xref_at`)
and stacked by the relevant producer. Era boundaries are annotated
on every plot. Every output CSV is preceded by a single-line caveat
header naming the four shared limitations from DESIGN.md (silent
drops, bidirectional adoption lag, attention non-uniformity,
multi-case bundling); convergence across signals is informative,
divergence is ambiguous, and a low reading in every signal is *not*
evidence the underlying phenomenon is absent:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(pwd)/output:/output" \
      console-analysis \
      -m src.analysis.resolution_quality --config /config/config.yaml --verbose

The `_reopen_by_original_reporter` plot only counts reopens whose
actor is the original issue author — a deliberately narrow
high-precision signal. The volume-calibration table above (issue
lifecycle) shows the population-wide upper bound: a reopen-event
total per week (`reopens`) and a per-cohort count of issues created
that week that ever got reopened (`c_reopened`). Both bound how
much signal a per-producer reopen plot can carry.

Each plot is produced in three forms:
- `output/plots/<repo>/*.png` -- static, portable, paste-into-doc
- `output/html/<repo>/*.html` -- interactive (Plotly), hover for
  exact values; self-contained file, opens in any browser
- `output/csv/<repo>/*.csv` -- the raw daily counts behind each plot

The `data` mount is writable because SQLite may need to access
WAL/shm sidecar files even when the database itself is opened
read-only; the analysis code uses a read-only connection internally
and will not modify the database.

## Recovering from a halted run

The extractors run a full SQLite integrity check at startup, after each
phase, and at most once per hour during long phases. Before each phase
they also write a `VACUUM INTO` snapshot under `data/snapshots/`. If an
integrity check fails, the extractor halts with exit code 3 and a
message indicating which phase failed.

To recover from a halted run:

1. Identify the failing phase from the log (e.g. "INTEGRITY CHECK
   FAILED for kubestellar/console: ... during reactions").
2. Replace `data/db.sqlite` with the snapshot taken before that phase:

       cp data/snapshots/kubestellar_console__before_reactions.sqlite data/db.sqlite

3. Optionally inspect what the phase had attempted to do, and decide
   whether to re-run the same phase or skip ahead.
4. Re-run the extractor. The startup integrity check should pass on
   the restored snapshot. Watermarks for completed phases are intact
   (they live in the database) so prior phases will not re-fetch.

If the snapshot itself is also corrupt, `sqlite3 <db> ".recover"` can
extract intact pages into a fresh database; see SQLite's documentation
for details.

Why a halt might happen: the database lives on a host-bind-mounted
filesystem inside the container (e.g. Rancher Desktop's virtiofs
layer), and SQLite warns that filesystems with imperfect locking or
fsync semantics can corrupt a database, particularly during WAL
checkpoints. See DESIGN.md for the full story and the structural fix
(moving the database to a Docker named volume) that's available if
the partial mitigations stop sufficing.

## Running tests

The smoke tests verify imports succeed, the schema applies cleanly to an
in-memory sqlite database, and the registry helpers behave correctly.
They need no PAT, no network, and no bind mounts:

    docker run --rm console-analysis tests/test_smoke.py

## Running directly (without Docker)

The container is the supported path; running directly is for development
convenience.

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python -m src.extractor_github --config config.yaml

When running on the host, use host-style paths in `config.yaml` (e.g.
`./data`, `../console`).

## Repository layout

    SCHEMA.md            -- data model
    DESIGN.md            -- methodology and architecture
    Dockerfile           -- container build
    requirements.txt     -- Python dependencies
    config.yaml.example  -- copy to config.yaml and edit
    src/                 -- source code
    tests/               -- tests

The directories `data/`, `output/`, `repos/`, and `config.yaml` itself
are gitignored.
