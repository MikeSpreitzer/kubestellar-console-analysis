# Design

This document describes the design of the analysis pipeline beyond what is
already covered in `SCHEMA.md`. The schema document specifies the data model;
this document specifies the layered work that produces and consumes it,
covers methodology choices, and records context that helps future maintainers
re-run or extend the analysis.

## Purpose of the analysis

The current purpose is to characterize the degree and shape of
autonomous development of `kubestellar/console` (and later
`kubestellar/docs`) over their short, rapidly-evolving history, using
artifacts available from Git and GitHub. Concretely: how much of the
work is human, how much is agent-produced, what kinds of agent
producers operate and when each came online, and how those proportions
have moved over time.

A possible later goal — to the degree the artifacts support it — is to
assess the quality of the work being developed. That goal is more
ambitious and more constrained by what Git/GitHub can show; whether
this analysis pipeline grows to address it depends on what the initial
characterization reveals. Some quality categories the pipeline will not
be able to address from these sources are listed below under "What this
analysis cannot measure," and apply equally to the current and possible
later goals.

A central methodological constraint: the repositories under study have
evolved rapidly over a short history (roughly five months at the start
of this work, with at least six distinct ACMM-level transitions inside
that window). Aggregate measurements that average across this period
mix materially different systems. Every variable should be plotted
against time at fine resolution, with system-state transitions marked
as annotations on the time axis.

## Architectural layers

The pipeline is organized as four layers, each producing artifacts the next
consumes. This separation supports re-running individual layers, comparing
outputs across re-runs, and substituting better implementations without
disturbing upstream or downstream work.

### Layer 1 — extraction

Pulls data from external sources (GitHub REST API, git log on local
clones) into the sqlite store and the on-disk run-artifacts tree.

The extraction layer is **idempotent and incremental**. Running it twice
should not duplicate rows; running it after a hiatus should fetch only
what's new (and back-fill anything previously skipped due to errors). State
about what has been fetched lives in `extraction_state` rows.

Two extractors at this layer:

- **GitHub extractor** — issues, PRs, timeline events, reviews, comments,
  reactions, labels, workflow run metadata, workflow run logs and
  artifacts (where still in retention). Reads the GitHub REST API.

- **Git extractor** — commits, commit-file changes, workflow file states.
  Reads from local git clones of subject and support repositories.

Layer 1 produces only facts that GitHub or git can directly attest to. No
interpretation, no classification, no derived metrics.

### Database integrity defenses

The first long extraction run produced SQLite corruption mid-run, with no
clear root cause (possibly the bind-mounted filesystem layer of Rancher
Desktop under high concurrent I/O during the artifacts-fetch phase, or
something we have not pinned down). The pipeline now defends against
recurrence with several measures, applied to both extractors:

- **`PRAGMA synchronous = FULL`** so every commit waits for fsync,
  reducing risk from partial writes during an unclean exit.
- **`PRAGMA journal_mode = WAL`** with **periodic
  `wal_checkpoint(TRUNCATE)`** at phase boundaries and inside long
  per-item loops, keeping the WAL small so less unflushed state is at
  risk at any moment.
- **`VACUUM INTO`** snapshots before each phase, kept under
  `data/snapshots/`, so if a phase corrupts the database we have a
  clean pre-phase copy to fall back to.
- **Full `PRAGMA integrity_check`** at extractor startup, after each
  phase, and at most once per hour during long phases (via an
  `HourlyChecker` helper). Any failure halts the extractor with a clear
  error message pointing at the snapshot for recovery.

These defenses are extractor-layer concerns; the schema is unchanged
by them.

### Layer 2 — classification

Reads layer 1 output and produces `producer_classification` rows. The
classifier is **time-aware**: a marker convention introduced in week N
should classify only artifacts created at or after week N, with earlier
artifacts left to other rules.

The classifier preserves multiple verdicts per artifact (one per source).
Analysis code picks among them by source authority when a single label is
needed.

Layer 2 is **stateless across runs** in the sense that it can be re-run
from layer 1 output without requiring incremental bookkeeping. Each run
is tagged with a `classifier_version` string. Re-running with the same
version replaces existing rows for that version (so re-runs do not
accumulate duplicates); re-running with a new version adds new rows
alongside the old, so the output of two classifier versions can be
compared side by side.

### Layer 3 — analysis

Reads layers 1 and 2. Produces aggregate metrics, time series, and plots.

Layer 3 is the layer most likely to change frequently as we explore which
metrics tell which stories. The lower layers should be stable; the
analysis layer should not be.

Output of layer 3:
- Plots as PNG files
- Underlying CSVs for each plot, named in parallel, so the numbers behind
  any chart are inspectable without re-running
- A run log in markdown summarizing what was produced and any caveats

### Layer 4 — interpretation (out of scope for code)

Human reading of layer 3 outputs against the questions that motivated the
analysis. Decisions about what to investigate next.

## What "agentic development" means for this analysis

The repositories under study process work via several producers, only some
of which are humans. The producer taxonomy used by the classifier:

- **Humans (credentialed as themselves)** — author login does not end in
  `[bot]`, no explicit AI co-author trailer present. This is an upper
  bound on actual human work; the credential field cannot distinguish
  human-typed work from human-tool-orchestrated work, and does not
  account for humans running bots under their own credentials.

- **Humans with AI collaboration disclosed** — credential is human, but a
  `Co-Authored-By:` trailer or similar marker discloses tool use.

- **Copilot** — author is `copilot[bot]` (or related), or the `copilot`
  label is applied. Dispatched by various orchestrators (the
  `reusable-ai-fix.yml` infra workflow, hive's scanner, manual triggers).
  The orchestrator and the producer are different concerns.

- **kubestellar-hive[bot]** — author or merger is the hive identity. Hive
  came online late in the repo's life; presence of this identifier means
  the artifact is from the hive era. Hive itself is a supervisory layer
  with multiple sub-agents (scanner, reviewer, architect, outreach,
  supervisor); the GitHub-visible markers do not always distinguish
  among them.

- **Auto-QA family** — issues with `[Auto-QA]` title prefix or analogous
  labels. Multiple sub-checks emit through this convention.

- **Agentic-workflows framework (`[aw]`)** — the `aw` framework supports
  many distinct workflows; the prefix marks the framework, not a specific
  producer. Sub-classification requires inspecting which workflow ran.

- **Per-purpose checkers** — link-checker, typo-checker, pr-verifier,
  etc. Each has its own label or distinctive title. (Distinct from
  hive's "scanner" sub-agent, which is the supervisory dispatcher inside
  hive.)

- **Other bots** — dependabot, github-actions, copilot-pull-request-reviewer,
  dashboard-snapshot bots. Identified by login.

- **Unknown** — bot-credentialed but not classifiable from available
  markers; or human-credentialed with the upper-bound caveat.

The taxonomy is an evolving artifact. As workflows are added, removed, or
modified in the subject and support repositories, the classifier rules
need to track these changes. The `classifier_version` column on
`producer_classification` lets us re-run when the taxonomy changes.

## Time resolution

The repository under study has a short history (months), with rapid
non-stationary evolution. Time resolution choices reflect this.

- **For workflow file state**: store at every commit that touches the
  file, not at fixed intervals. Sampling at intervals would smear over
  short-lived states; commit-resolution captures every state change at
  full fidelity.

- **For issue/PR aggregates plotted in graphs**: daily bins by default.
  Daily bins surface the L5→L6 transition cleanly; weekly bins do not.
  Plots can be visually smoothed when needed, but raw data is daily.

- **For inflection events** (workflow file additions, label taxonomy
  changes, ACMM-transition-bearing commits): plotted as point
  annotations on the time axis, not as bins.

- **For "active workflow set at time T"**: a query against
  `workflow_file_state` returning the latest commit-content prior to T
  for each path. Any query about behavior at T should resolve via this
  pattern, not by sampling at coarser intervals.

## Flurry handling

We hypothesize that workflow file changes (and possibly other
agent-driven activity) sometimes arrive in bursts — multiple commits
over hours, then quiet. If that pattern holds in the data, the *intent*
of a burst is usually a single coordinated change, and counting every
intermediate commit as an independent state transition would over-count
meaningful evolution.

Whether the pattern actually holds is itself an empirical question for
this analysis to answer. The plotted inter-commit interval distribution
will show whether it does, and at what timescale; the existence and
shape of flurries should not be assumed.

The schema stores everything at full fidelity; flurry detection (if
applicable) is an analysis-layer concern. The analysis layer offers
two views:

- **Uncollapsed**: every commit is a state transition.
- **Flurry-collapsed**: commits within a configurable inter-commit gap
  threshold are grouped and represented by their end-of-flurry state.

The collapse threshold is not picked a priori. The analysis layer
computes the inter-commit interval distribution for the relevant
commits and lets the threshold be informed by that distribution (or
plotted as a sensitivity curve over multiple thresholds). If the
distribution shows no clear bimodality, the flurry-collapsed view may
not be informative and the uncollapsed view is the relevant one.

## Reversal detection

Formal git reverts (`git revert`-style commits) are rare in this corpus.
Agentic authors typically produce forward-direction "fix" PRs that may
fully or partially undo earlier work, without the `Revert` marker. The
analysis must detect informal reversal, not formal reverts.

Approaches, in order of expected usefulness:

1. **Line-survival analysis**: for lines added by PR A, how long until
   they're removed by some later PR B. Plotted as a Kaplan-Meier-style
   survival curve per cohort of PRs.

2. **File-touch return rate**: how often a file modified by PR A is
   modified again by PR B within window W. Window W is itself a variable;
   plot the family of curves over multiple windows rather than picking
   one.

3. **Diff-against-earlier-diff**: inverse-overlap measurement between a
   PR's diff and recent prior PRs' diffs. Most direct, most expensive,
   noisiest. Reserved for second-pass analysis if the cheaper proxies
   warrant.

Reversal is a candidate metric; whether it ends up in the published
analysis depends on what the cheap proxies actually show.

## Quality metric inventory

These are the metrics anticipated for the analysis layer. Not all will be
implemented in the first cut; the list is meant to be aspirational and
evolving.

### Authorship and orchestration mix

- PR authorship by producer, daily, time series
- PR merger by producer, daily, time series
- Issue authorship by producer, daily, time series
- Issue → PR producer mapping (who-wrote-issue × who-wrote-PR), weekly

### Throughput and cadence

- PRs merged per day, segmented by producer
- Issues opened per day, segmented by producer
- Time from issue open to first PR linked to it
- Time from PR open to merge, distribution
- Issues closed within N minutes of opening (right-tail of fast closes)

### Engagement signals

- Comments per PR before merge, distribution
- Fraction of PRs with any human-credentialed comment before merge
- Reviews per PR (formal)
- Up-vote / thumbs-down reaction counts on issues and PRs

### Reversal and churn

- Same-file touch-return rate, family of curves over multiple windows
- Line survival curves per cohort
- Per-file commit frequency over time

### Workflow scaffolding

- Number of distinct workflow files active per day
- Workflow file change rate (uncollapsed and flurry-collapsed)
- Disabled/re-enabled status of each workflow over time

### Issue lifecycle

- Reopen rate per cohort
- Time to reopen distribution
- Same-reporter follow-up filing rate
- "Still broken" comment presence on closed issues

### Subject-area splits

For each metric above where it's meaningful: split by

- Issue/PR kind (bug, feature, doc, etc.)
- Tier classification (tier/0..3, where the label is present)
- Whether the change touches `.github/workflows/` (i.e., is the agentic
  scaffolding being modified)

## What this analysis cannot measure

These limitations should be kept visible whenever results are presented.

- **Reporter satisfaction with closed issues** — silent drops by original
  reporters are invisible by construction.
- **Quality-of-resolution in the middle of the distribution** —
  partial/sideways resolutions usually produce no Git/GitHub artifact.
- **Production correctness** — runtime behavior is not in the data.
- **Performance regressions below hard CI thresholds** — slow drift is
  invisible to per-PR metrics.
- **Concurrency correctness beyond what `-race` happens to surface** —
  the schedule space is not exhausted by CI.
- **Security properties beyond pattern-matching** — composition errors
  and system-context bugs do not pattern-match.
- **Architectural drift** — direction of cumulative changes is a
  judgment, not a metric.
- **Scope drift** — whether incoming work matches the project's
  ROADMAP non-goals is a judgment, not a metric, though the analysis can
  flag candidates for review.
- **Distinctions among hive sub-agents** — the visible markers do not
  reliably discriminate scanner from architect from reviewer.
- **Pre-`copilot`-label Copilot work** — if Copilot dispatch existed
  before the label convention, those PRs are misclassified.

## Storage layout

```
console-analysis/
├── SCHEMA.md
├── DESIGN.md
├── README.md
├── LICENSE
├── .gitignore                         # excludes data/
├── data/                              # gitignored
│   ├── db.sqlite                      # the sqlite database
│   ├── snapshots/                     # pre-phase VACUUM INTO snapshots
│   └── gh_runs/                       # gzipped logs and artifacts
│       └── <repo_id>/
│           └── <run_id>/
│               ├── logs.tar.gz
│               └── artifacts/...
├── repos/                             # gitignored, optional
│   ├── kubestellar-console/           # git clone of subject repo
│   ├── kubestellar-docs/
│   └── kubestellar-infra/
├── src/
│   ├── schema.sql                     # DDL matching SCHEMA.md
│   ├── extractor_github/              # layer 1, GitHub side
│   ├── extractor_git/                 # layer 1, git side
│   ├── classifier/                    # layer 2
│   ├── analysis/                      # layer 3
│   └── common/                        # shared utilities
├── output/                            # gitignored; analysis layer's outputs
│   ├── plots/
│   ├── csv/
│   └── reports/
└── tests/
```

The data and output directories are gitignored. The repos directory is
also gitignored — the analysis works from local git clones the user
maintains; the analysis repo does not vendor the subject repos.

## Configuration

Configuration is read from a `config.yaml` (gitignored, with
`config.yaml.example` checked in) at the analysis repo root. Anticipated
fields:

```yaml
github_token_env: GITHUB_TOKEN          # env var holding the PAT
data_dir: ./data
output_dir: ./output
repos:
  - owner: kubestellar
    name: console
    role: subject
    local_clone: ../console            # path to git clone (optional)
  - owner: kubestellar
    name: infra
    role: support
    local_clone: ../infra
extraction:
  log_fetch_concurrency: 5             # respect secondary rate limits
  fetch_logs: true                     # eagerly fetch within 90-day window
  fetch_artifacts: false               # very large at agentic CI scale; off by default
analysis:
  daily_bin_timezone: UTC
  flurry_gap_minutes: null             # null means compute from data
```

## Choices recorded for traceability

A handful of choices were made in conversation that warrant being
preserved here so future re-runs follow the same conventions:

- **Workflow run metadata is assumed permanent.** If a future test
  reveals it isn't, this assumption is revisited.
- **Workflow run logs are stored as gzipped files outside sqlite**,
  under `data/gh_runs/<repo_id>/<run_id>/`.
- **The data directory is gitignored.** Sharing of the database is not
  yet arranged; if and when it is, that affects neither the schema nor
  the layout, only the publication step.
- **Python is the implementation language.** Standard library plus
  `requests`, `matplotlib`, and `pandas` cover the needs.
- **The repo lives at `MikeSpreitzer/console-analysis`** in personal
  GitHub space, separate from any kubestellar org governance.
- **Reusable workflows are imported from `infra@main`** by the subject
  repos (per observation), so infra's main HEAD at the time of an event
  determines the reusable's behavior at that event. The schema treats
  infra as a parallel time series of state for this reason.

## Re-running the analysis

The pipeline is designed for repeated runs. Typical re-run flow:

1. Update local clones (`git pull` in each subject and support repo).
2. Run extractors. They fetch only what's new since `extraction_state`
   was last updated.
3. Re-run classifier (full pass; it produces new `producer_classification`
   rows tagged with the current `classifier_version`).
4. Re-run analysis. Produces fresh plots and CSVs, possibly side by side
   with the prior run for comparison.

A single command at the repo root should execute all four steps in order;
intermediate steps should also be invocable individually for development.

## Open design questions

- **How to publish the data.** The sqlite database may be useful to
  share with collaborators (Andy, the paper's author, anyone reproducing
  results). Sharing arrangement TBD.
- **Whether to mirror the analysis at `kubestellar/docs`** as a
  parallel-but-separate run, or extend this one to cover both subject
  repos. The schema already supports the latter; the choice affects
  practical workflow, not data modeling.
- **How to incorporate hive's journal/ledger** if and when access is
  arranged. The `journal` source is reserved in
  `producer_classification.source` but not populated.
- **Whether to add a comparator project** (a human-developed Kubernetes
  ecosystem project) for ordinal baselines. Earlier discussion concluded
  this was useful only as a sanity check; not in the first cut.
- **Capturing workflows disabled via the GitHub web UI.** A maintainer
  can disable a workflow through GitHub's Actions tab; this sets the
  workflow's `state` (visible via
  `GET /repos/{owner}/{repo}/actions/workflows`) to
  `disabled_manually` without altering the workflow file in git. The
  GitHub REST API exposes only current state, not its history; the
  audit log would have history but is org-admin-gated. Snapshotting
  current state at extraction time is of limited value on its own —
  extractions are expected to be far less frequent than the rate at
  which workflows are disabled or re-enabled, so periodic snapshots
  would miss most transitions. The realistic path is to *infer*
  disable/re-enable events from gaps in workflow run streams, in the
  analysis layer; current-state snapshots, if captured, are useful only
  as a sanity check on those inferences at extraction time. Whether to
  add even that snapshot is deferred.
