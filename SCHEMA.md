<!--
Copyright 2026 Mike Spreitzer
SPDX-License-Identifier: Apache-2.0
Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).
-->

# Schema design

This document describes the data model for the analysis. Code reads and writes
it; this document is the human-readable specification of *why* each table is
shaped the way it is.

## Top-level intent

We are extracting Git and GitHub artifacts about a small set of repositories
(initially `kubestellar/console`, later `kubestellar/docs`) and the reusable
workflows in `kubestellar/infra` that those repositories invoke. The goal is
to support time-resolved analysis: for any moment T in the repo's life, what
was happening, what artifacts were active, and (for events that occurred at T)
what state the surrounding scaffolding was in.

The schema is therefore organized as **immutable event streams** keyed by
time, with **mutable state** reconstructable by replaying events up to T.
Nothing is stored in a way that requires us to know "the current value" — every
historical state is queryable.

## Sources of truth

We distinguish three kinds of repositories in the schema:

- **subject** repositories are the analysis targets. Full corpus of issues,
  PRs, timelines, reactions, reviews, comments, workflow runs (metadata always,
  logs and artifacts when still in retention). Currently `kubestellar/console`;
  `kubestellar/docs` to be added.

- **support** repositories are not analysis targets but are extracted because
  they bear directly on what happens in the subjects. Currently:
  `kubestellar/infra` (holds reusable workflows imported by the subjects'
  callers); `kubestellar/hive` (the supervisory agent system that operates
  on the subjects -- its commit history and configuration are evidence of
  what the agentic system does and when, but hive's own development is not
  itself analyzed).
  For support repos, we extract per-commit metadata and the contents of
  workflow files, but not issues or PRs.

  As of this writing, the support role is narrowly exercised: the only
  thing the analysis layer actually reads from support repos is workflow
  file contents, and only `kubestellar/infra` contributes to that purpose
  (it holds the reusable workflows imported by the subjects). Hive is
  included as a support repo in anticipation of broadening the role --
  for example, to capture hive's policy files (`agents/`, `bin/`,
  `config/`) as a content time series, so future analyses can ask
  questions like "which scanner CLAUDE.md was active when this subject
  PR was merged?" That broadening is tracked as a future task; until
  then, hive's commit and per-file-change rows sit in the database
  unused but consistent.

- A repository can be both subject and support; the `role` column records this.

## Data sources for classification

For each issue or PR, we may have classifications from multiple sources, with
different levels of authority. The schema records all of them without picking
a winner:

- `journal` — explicit attribution from a hive activity log or equivalent
  external source. Highest authority. Not currently available; reserved.
- `workflow_run` — derivation from GitHub Actions run metadata that
  produced the artifact.
- `marker` — derivation from labels, title prefix conventions, and author
  identity on the artifact itself.
- `unknown` — no signal could classify the artifact.

Analysis code that wants a single classification per artifact can pick the
highest-authority source available.

## Tables

### `repo`

One row per repository we have any data on.

```
repo_id          INTEGER PRIMARY KEY
owner            TEXT NOT NULL
name             TEXT NOT NULL
role             TEXT NOT NULL  -- 'subject' | 'support' | 'both'
default_branch   TEXT
first_seen_at    TIMESTAMP NOT NULL
UNIQUE(owner, name)
```

### `actor`

One row per GitHub identity we encounter — humans and bots alike. The
`is_bot_login` flag detects accounts whose login follows GitHub's app
convention (`name[bot]`). Note that this is NOT a human/agent classifier;
humans run bots under their credentials, and that fact lives in
analysis-layer interpretation.

```
actor_id         INTEGER PRIMARY KEY
login            TEXT NOT NULL UNIQUE
gh_user_id       INTEGER
gh_type          TEXT             -- 'User' | 'Bot' | 'Organization'
is_bot_login     BOOLEAN          -- TRUE iff login ends in '[bot]'
first_seen_at    TIMESTAMP NOT NULL
```

### `commit`

One row per commit in any repository we extract Git history from.

```
commit_id        INTEGER PRIMARY KEY
repo_id          INTEGER NOT NULL REFERENCES repo(repo_id)
sha              TEXT NOT NULL
parent_shas      TEXT             -- newline-separated list, to record merges
author_name      TEXT
author_email     TEXT
author_login     TEXT             -- resolved via API; may be NULL
authored_at      TIMESTAMP NOT NULL
committer_name   TEXT
committer_email  TEXT
committed_at     TIMESTAMP NOT NULL
message          TEXT NOT NULL
UNIQUE(repo_id, sha)
```

We deliberately store both author and committer fields. They differ for
rebased work, GitHub-merged PRs, cherry-picks, and various bot operations.
The difference is sometimes informative.

### `commit_file`

One row per (commit, file) pair. Records what happened to each file in each
commit.

```
commit_id        INTEGER NOT NULL REFERENCES commit(commit_id)
path             TEXT NOT NULL
old_path         TEXT             -- for renames; NULL otherwise
change_type      TEXT NOT NULL    -- 'A' | 'M' | 'D' | 'R' | 'C' | 'T'
lines_added      INTEGER
lines_removed    INTEGER
PRIMARY KEY (commit_id, path)
```

We do not store diff content in sqlite. If we need a diff, git is right there.

### `workflow_file_state`

One row per (repository, workflow file path, commit that touched it). Records
the file's contents at that commit, so we can reconstruct "what did this
workflow look like at time T" without re-running git for every query.

```
state_id         INTEGER PRIMARY KEY
repo_id          INTEGER NOT NULL REFERENCES repo(repo_id)
path             TEXT NOT NULL                 -- e.g. '.github/workflows/auto-qa.yml'
commit_id        INTEGER NOT NULL REFERENCES commit(commit_id)
content          TEXT                          -- full file content; NULL if file deleted
content_sha      TEXT                          -- sha256 of content; NULL if deleted
exists_after     BOOLEAN NOT NULL              -- false means this commit deleted the file
UNIQUE(repo_id, path, commit_id)
```

Workflow files are small (1–10 KB typically) and there aren't many commits
touching them, so the storage cost is modest. We prefer storing content over
re-extracting it because the alternative is checking out each commit on
demand, which is slow.

### `workflow_run`

One row per GitHub Actions run we observed. Metadata is assumed permanent
(revisit if discovery contradicts). Logs and artifacts are stored as files
outside sqlite (see `logs_status`, `artifacts_status`).

```
run_id              INTEGER PRIMARY KEY        -- GitHub's numeric run id
repo_id             INTEGER NOT NULL REFERENCES repo(repo_id)
workflow_path       TEXT NOT NULL
workflow_name       TEXT
run_number          INTEGER
run_attempt         INTEGER
event               TEXT                       -- 'push', 'schedule', 'pull_request', etc.
status              TEXT                       -- 'completed', 'in_progress', etc.
conclusion          TEXT                       -- 'success', 'failure', 'cancelled', etc.
head_sha            TEXT
head_branch         TEXT
actor_id            INTEGER REFERENCES actor(actor_id)
triggering_actor_id INTEGER REFERENCES actor(actor_id)
created_at          TIMESTAMP NOT NULL
run_started_at      TIMESTAMP
updated_at          TIMESTAMP
logs_status         TEXT NOT NULL              -- see status vocabulary below
logs_path           TEXT                       -- relative path to gzipped log file
artifacts_status    TEXT NOT NULL
artifacts_path      TEXT
```

Status vocabulary for `logs_status` and `artifacts_status`:
- `pending` — not yet attempted
- `fetched` — successfully retrieved and stored
- `expired` — past the 90-day retention window
- `unavailable` — present-but-not-retrievable (404 with active run, etc.)
- `deleted` — run itself was deleted via API
- `error` — fetch failed for another reason; should be retried later

Storage paths follow the convention
`gh_runs/<repo_id>/<run_id>/logs.tar.gz` (and similar for artifacts), under
the gitignored data directory.

### `issue`

One row per issue OR pull request. GitHub's data model treats PRs as a
specialization of issues; we follow that. The `is_pr` column distinguishes
them. PR-specific fields live in `pull_request`.

```
issue_id            INTEGER PRIMARY KEY
repo_id             INTEGER NOT NULL REFERENCES repo(repo_id)
number              INTEGER NOT NULL
gh_node_id          TEXT
title               TEXT NOT NULL
body                TEXT
author_id           INTEGER REFERENCES actor(actor_id)
state               TEXT NOT NULL              -- 'open' | 'closed' (last observed)
state_reason        TEXT                       -- 'completed' | 'not_planned' | 'reopened' | NULL
created_at          TIMESTAMP NOT NULL
updated_at          TIMESTAMP NOT NULL
closed_at           TIMESTAMP
closed_by_id        INTEGER REFERENCES actor(actor_id)
is_pr               BOOLEAN NOT NULL
last_observed_at    TIMESTAMP NOT NULL
UNIQUE(repo_id, number)
```

The mutable fields here (`state`, `state_reason`, `closed_at`, `closed_by_id`,
applied labels) are last-observed values. The historical evolution lives in
`issue_event`. The `last_observed_at` column tells us when the snapshot was
taken so we know whether to refresh.

### `pull_request`

One row per PR, joined to its `issue` row.

```
issue_id            INTEGER PRIMARY KEY REFERENCES issue(issue_id)
merged              BOOLEAN NOT NULL
merged_at           TIMESTAMP
merged_by_id        INTEGER REFERENCES actor(actor_id)
merge_commit_sha    TEXT
base_ref            TEXT
head_ref            TEXT
head_repo_id        INTEGER REFERENCES repo(repo_id)
draft               BOOLEAN
additions           INTEGER
deletions           INTEGER
changed_files       INTEGER
mergeable_state     TEXT
```

### `pr_file`

Per-PR, per-file. Mirrors `commit_file` but at PR granularity.

```
issue_id            INTEGER NOT NULL REFERENCES issue(issue_id)
path                TEXT NOT NULL
status              TEXT                       -- 'added' | 'modified' | 'removed' | 'renamed'
additions           INTEGER
deletions           INTEGER
changes             INTEGER
PRIMARY KEY (issue_id, path)
```

### `label`

Distinct labels per repo.

```
label_id            INTEGER PRIMARY KEY
repo_id             INTEGER NOT NULL REFERENCES repo(repo_id)
name                TEXT NOT NULL
description         TEXT
color               TEXT
UNIQUE(repo_id, name)
```

### `issue_label`

Currently-applied labels per issue. Historical label changes are events in
`issue_event`.

```
issue_id            INTEGER NOT NULL REFERENCES issue(issue_id)
label_id            INTEGER NOT NULL REFERENCES label(label_id)
PRIMARY KEY (issue_id, label_id)
```

### `issue_event`

The timeline. One row per event in an issue or PR's history. This is the
single richest table for the analysis.

```
event_id            INTEGER PRIMARY KEY
issue_id            INTEGER NOT NULL REFERENCES issue(issue_id)
gh_event_id         INTEGER                    -- GitHub's id; nullable for derived events
event_type          TEXT NOT NULL              -- 'labeled', 'unlabeled', 'closed',
                                               -- 'reopened', 'merged', 'commented',
                                               -- 'reviewed', 'cross-referenced',
                                               -- 'renamed', 'assigned',
                                               -- 'review_requested',
                                               -- 'head_ref_force_pushed', etc.
actor_id            INTEGER REFERENCES actor(actor_id)
created_at          TIMESTAMP NOT NULL
-- Type-specific payload:
label_id            INTEGER REFERENCES label(label_id)        -- for labeled/unlabeled
comment_id          INTEGER                                   -- for commented
review_id           INTEGER                                   -- for reviewed
review_state        TEXT                                      -- review state at the event
referenced_issue_id INTEGER REFERENCES issue(issue_id)        -- for cross-referenced
old_value           TEXT
new_value           TEXT
extra_json          TEXT                                      -- catchall for less-common fields
```

This table will be large. Indexes anticipated on (issue_id, created_at) and
(event_type, created_at).

### `comment`

Comment bodies, separate from the events table because the bodies are large.

```
comment_id          INTEGER PRIMARY KEY        -- GitHub's numeric id
issue_id            INTEGER NOT NULL REFERENCES issue(issue_id)
author_id           INTEGER REFERENCES actor(actor_id)
body                TEXT NOT NULL
created_at          TIMESTAMP NOT NULL
updated_at          TIMESTAMP
```

### `review`

PR reviews (the formal Approved / ChangesRequested / Commented kind).

```
review_id           INTEGER PRIMARY KEY        -- GitHub's numeric id
issue_id            INTEGER NOT NULL REFERENCES issue(issue_id)
author_id           INTEGER REFERENCES actor(actor_id)
state               TEXT NOT NULL
body                TEXT
submitted_at        TIMESTAMP
commit_sha          TEXT
```

### `reaction`

Reactions on issues, PRs, comments, and reviews.

```
reaction_id         INTEGER PRIMARY KEY        -- GitHub's numeric id
target_kind         TEXT NOT NULL              -- 'issue' | 'comment' | 'review'
target_id           INTEGER NOT NULL           -- references the corresponding table's PK
content             TEXT NOT NULL              -- '+1' | '-1' | 'laugh' | 'hooray' |
                                               -- 'confused' | 'heart' | 'rocket' | 'eyes'
actor_id            INTEGER REFERENCES actor(actor_id)
created_at          TIMESTAMP NOT NULL
```

### `linked_pr`

Many-to-many between issues and the PRs that close them. Populated from
`closes`/`fixes`/`resolves` keywords in PR bodies and from GitHub's own
linked-issue metadata. The `link_source` column records how the link was
discovered, so multiple sources reinforcing the same link are visible.

```
issue_id            INTEGER NOT NULL REFERENCES issue(issue_id)  -- the issue
pr_id               INTEGER NOT NULL REFERENCES issue(issue_id)  -- the PR
link_source         TEXT NOT NULL              -- 'github_api' | 'pr_body_keyword' | 'event'
PRIMARY KEY (issue_id, pr_id, link_source)
```

### `producer_classification`

Output of the classifier layer. One row per (target, source,
classifier_version) tuple. The (`target_kind`, `target_id`) pair points
into the appropriate source table:

- `issue` and `pr` both reference `issue.issue_id` (PRs are a kind
  of issue in our schema; the kind distinguishes them).
- `commit` references `commit_.commit_id`.
- `comment` references `comment.comment_id`.
- `review` references `review.review_id`.

There is no SQL foreign key on (`target_kind`, `target_id`) because
the target's table varies per row; integrity is maintained at the
application level. Multiple rows per target are possible (one from
`marker`, later one from `workflow_run`, etc.); analysis code that
wants a single classification per artifact picks among them by source
authority.

```
classification_id   INTEGER PRIMARY KEY
target_kind         TEXT NOT NULL              -- 'issue' | 'pr' | 'commit'
                                               --   | 'comment' | 'review'
target_id           INTEGER NOT NULL           -- references the appropriate source table
source              TEXT NOT NULL              -- 'journal' | 'workflow_run' | 'marker' | 'unknown'
producer            TEXT NOT NULL              -- e.g. 'human-credentialed',
                                               -- 'copilot', 'hive-scanner',
                                               -- 'hive-merger', 'project-bot',
                                               -- 'other-bot-app', 'unknown'
sub_producer        TEXT                       -- finer-grained: the matched login or email
basis               TEXT                       -- short explanation:
                                               --   'login=copilot[bot] (known)',
                                               --   'email=scanner@kubestellar.io (known)', etc.
classified_at       TIMESTAMP NOT NULL
classifier_version  TEXT NOT NULL              -- so re-runs with new logic produce new rows
UNIQUE (target_kind, target_id, source, classifier_version)
```

Re-running the classifier with the same `classifier_version` deletes
existing rows for that version (scoped to the repo being classified)
and re-inserts; the UNIQUE constraint also enforces this at the
schema level. Running with a new `classifier_version` adds new rows
alongside the old, so verdicts from different rule sets can be
compared.

### `extraction_state`

Bookkeeping. Records what we've fetched and when, so re-runs are
incremental.

```
key                 TEXT PRIMARY KEY           -- e.g. 'console:issues:since',
                                               -- 'infra:workflows:last_commit'
value               TEXT NOT NULL
updated_at          TIMESTAMP NOT NULL
```

### `extraction_run`

One row per invocation of the extractor, for audit and debugging.

```
run_id              INTEGER PRIMARY KEY
started_at          TIMESTAMP NOT NULL
ended_at            TIMESTAMP
exit_status         TEXT                       -- 'success' | 'partial' | 'error'
notes               TEXT
api_calls_made      INTEGER
rate_limit_waits    INTEGER
```

## Ordering of operations

The extractor populates tables in this order to satisfy foreign keys without
deferred constraints:

1. `repo`
2. `actor` (created lazily as we encounter logins)
3. `commit`, `commit_file` (from git, for subject and support repos)
4. `workflow_file_state` (derived from commits touching `.github/workflows/`)
5. `label` (from API)
6. `issue` and `pull_request` (from API)
7. `pr_file`, `issue_label`, `comment`, `review`, `reaction`, `issue_event`, `linked_pr`
8. `workflow_run` (with logs/artifacts to filesystem)
9. `producer_classification` (separate classifier pass, after all data is present)

## What's deliberately not in the schema

- **No "current" computed fields beyond what GitHub itself returns.**
  Aggregate metrics (per-day counts, rolling windows, etc.) live in the
  analysis layer, not the data layer. The data layer stores facts.

- **No diff content.** Available via git when needed.

- **No log content.** Stored as files outside sqlite, referenced by path.

- **No producer_classification with built-in time slicing.** A classification
  is a verdict on the artifact as it exists; if we want to know "what would
  the classifier have said at time T," that's a re-run with `classifier_version`
  reflecting the time-T marker conventions, producing new rows.

## Open questions and deferred decisions

- **Workflow run logs from before the 90-day window** are unrecoverable. We
  fetch eagerly for what's still in retention. Later runs incrementally fetch
  new runs and back-fill metadata for older ones, but cannot recover expired
  logs.

- **The journal source** is reserved in `producer_classification.source` but
  not yet populated. The schema can accept rows from this source whenever it
  becomes available; no migration needed.

- **Adding `kubestellar/docs` as a subject** is anticipated. No schema changes
  needed; just additional rows in `repo` with `role='subject'`.

- **Compaction / VACUUM strategy** not addressed; sqlite handles steady-state
  growth fine for this volume.
