<!--
Copyright 2026 Mike Spreitzer
SPDX-License-Identifier: Apache-2.0
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
