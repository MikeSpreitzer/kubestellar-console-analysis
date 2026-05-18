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
`config.yaml`), the data directory, the output directory, and the
parent of your local git clones (read-only); pass the PAT. Use
`--user "$(id -u):$(id -g)"` so files written into the bind-mounted
directories are owned by your host user. The example assumes the
analysis repo and its sibling clones (`console`, `infra`, …) live
under a common parent directory:

    docker run --rm \
      --user "$(id -u):$(id -g)" \
      -e GITHUB_TOKEN \
      -v "$(pwd):/config:ro" \
      -v "$(pwd)/data:/data" \
      -v "$(pwd)/output:/output" \
      -v "$(cd .. && pwd):/repos:ro" \
      console-analysis \
      -m src.extractor_github --config /config/config.yaml

`$(cd .. && pwd)` resolves to the absolute path of the parent of the
analysis repo. The names of the sibling clones inside that parent
(`console`, `infra`, etc.) must match the trailing path component of
each `local_clone` value in `config.yaml` (the example uses
`/repos/console`, `/repos/infra`).

To run the git extractor or any other module, change the trailing
arguments:

    docker run --rm --user "$(id -u):$(id -g)" ... console-analysis \
      -m src.extractor_git --config /config/config.yaml

The git extractor doesn't talk to GitHub, so the `GITHUB_TOKEN`
environment variable can be omitted for it.

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

Each plot is produced in three forms:
- `output/plots/<repo>/*.png` -- static, portable, paste-into-doc
- `output/html/<repo>/*.html` -- interactive (Plotly), hover for
  exact values; self-contained file, opens in any browser
- `output/csv/<repo>/*.csv` -- the raw daily counts behind each plot

The `data` mount is writable because SQLite may need to access
WAL/shm sidecar files even when the database itself is opened
read-only; the analysis code uses a read-only connection internally
and will not modify the database.

## Running the classifier

The classifier walks every subject repo's issues, PRs, commits,
comments, and reviews, applies a shared rule list (see
`src/classifier/rules.py`), and writes verdicts to the
`producer_classification` table. Required before analyses that join
to that table; the existing analysis modules also import from
`src/classifier/rules` directly for inline credential classification
during plotting.

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
substitute `'v2'` with whatever version the classifier last ran with):

Producer breakdown across all artifact kinds:

    SELECT producer, COUNT(*) n
    FROM producer_classification
    WHERE classifier_version = 'v2'
    GROUP BY producer
    ORDER BY n DESC;

Producer breakdown for one kind:

    SELECT producer, COUNT(*) n
    FROM producer_classification
    WHERE classifier_version = 'v2' AND target_kind = 'pr'
    GROUP BY producer
    ORDER BY n DESC;

Sub-producer detail within `other-bot-app`:

    SELECT sub_producer, COUNT(*) n
    FROM producer_classification
    WHERE classifier_version = 'v2' AND producer = 'other-bot-app'
    GROUP BY sub_producer
    ORDER BY n DESC;

Compare two classifier versions on the same artifact:

    SELECT v1.producer AS v1_producer, v2.producer AS v2_producer, COUNT(*) n
    FROM producer_classification v1
    JOIN producer_classification v2
      ON v1.target_kind = v2.target_kind
     AND v1.target_id = v2.target_id
     AND v1.source = v2.source
    WHERE v1.classifier_version = 'v1' AND v2.classifier_version = 'v2'
    GROUP BY v1.producer, v2.producer
    ORDER BY n DESC;

Join classification back to the source artifact (PRs example):

    SELECT i.number, i.title, pc.producer, pc.sub_producer
    FROM issue i
    JOIN producer_classification pc
      ON pc.target_kind = 'pr' AND pc.target_id = i.issue_id
    WHERE i.is_pr = 1
      AND pc.classifier_version = 'v2'
      AND pc.producer = 'hive-merger'
    LIMIT 20;

The other artifact kinds use `target_kind = 'issue'`, `'commit'`,
`'comment'`, or `'review'` and join to the corresponding source
table (see [`SCHEMA.md`](SCHEMA.md) for which key field each
`target_id` references).

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
