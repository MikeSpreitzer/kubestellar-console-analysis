<!--
Copyright 2026 Mike Spreitzer
SPDX-License-Identifier: Apache-2.0
Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).
-->

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
have moved over time. `kubestellar/hive` is treated as a support
repository (not a subject of the analysis): hive is the supervisory
layer that drives console's agentic activity, so its commit history
and configuration files are useful evidence of what the agentic
system does and when, but hive's own development is not currently
analyzed. (Among other things, hive itself does not appear to be
managed by the same system of agents that operates on console and
docs.)

A note on the support role as currently exercised: the analysis layer
actually reads support-repo content for only one purpose today --
resolving `@main` reusable-workflow imports against `kubestellar/infra`.
Hive is included as a support repo on the speculation that the role
will broaden -- e.g., to capture hive's policy files
(`agents/`, `bin/`, `config/`) as a content time series, so future
subject-focused analyses can ask questions like "which scanner
CLAUDE.md was active when this subject PR was merged?" Until that
broadening happens, hive's commits sit in the database unused but
consistent. See SCHEMA.md and the open task on broadening the
content-snapshot path filter for details.

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
  reactions, labels, workflow run metadata. Optionally workflow run logs
  and artifacts when still in retention; both default off (see "Log
  fetching deferred" below for why). Reads the GitHub REST API. Requires
  a GitHub PAT.

- **Git extractor** — commits, commit-file changes, workflow file states.
  Reads from local git clones of subject and support repositories. Does
  not require a GitHub PAT.

Layer 1 produces only facts that GitHub or git can directly attest to. No
interpretation, no classification, no derived metrics.

### Database integrity defenses

Two long extraction runs against the bind-mounted SQLite database
produced localized b-tree corruption mid-run. In each case the damage
was confined to a single tree (the table being most aggressively
written at the time), which is consistent with the failure modes
SQLite documents on filesystems whose locking or fsync semantics
cannot be fully trusted -- see SQLite's
[How To Corrupt An SQLite Database File](https://www.sqlite.org/howtocorrupt.html),
which calls out network and otherwise-virtualized filesystems
explicitly. Rancher Desktop's host-bind-mount layer (virtiofs or
reverse-sshfs depending on configuration) is conceptually similar: a
layered filesystem whose semantics are implemented by the
virtualization stack rather than directly by the host's native
filesystem. We did not pin the root cause to a specific bug; the
behavior fits the pattern.

The pipeline defends against recurrence with several measures, applied
to both extractors:

- **`PRAGMA synchronous = FULL`** so every commit waits for fsync,
  reducing risk from partial writes during an unclean exit.
- **`PRAGMA journal_mode = WAL`** with **periodic
  `wal_checkpoint(TRUNCATE)`** at phase boundaries and inside long
  per-item loops, keeping the WAL small so less unflushed state is at
  risk at any moment. (Note: SQLite identifies WAL checkpoints as the
  one operation in WAL mode where unreliable fsync can still cause
  corruption, so periodic checkpointing is a partial mitigation, not
  a full one, on a filesystem whose fsync we don't fully trust.)
- **`VACUUM INTO`** snapshots before each phase, kept under
  `data/snapshots/`, so if a phase corrupts the database we have a
  clean pre-phase copy to fall back to.
- **Full `PRAGMA integrity_check`** at extractor startup, after each
  phase, and at most once per hour during long phases (via an
  `HourlyChecker` helper). Any failure halts the extractor with a clear
  error message pointing at the snapshot for recovery.

The defenses above are partial mitigations. The structural fix --
moving the SQLite database off the bind-mounted filesystem entirely --
is described under "Open design questions" below; it is not yet
implemented because the current workload (with logs and artifacts
disabled) appears to fit within whatever threshold these mitigations
cover. If corruption recurs, that's the trigger to do the migration.

These defenses are extractor-layer concerns; the schema is unchanged
by them.

### Layer 2 — classification

Reads layer 1 output and produces rows in
[`producer_classification`](SCHEMA.md#producer_classification). The
classifier is uniform across artifact kinds: issues, PRs, commits,
comments, and reviews are all presented to the rule list as a common
``Record`` shape. ``Record`` carries fields broader than any single
kind needs (``message`` for commits, ``labels`` for issues/PRs, etc.);
predicates that branch on artifact-specific fields can guard with
``r.target_kind``.

Source code for the layer:

- `src/classifier/record.py` -- the ``Record`` dataclass, with
  ``target_kind`` ∈ ``{issue, pr, commit, comment, review}``.
- `src/classifier/rules.py` -- the rule list, the producer constants,
  the bot-email and bot-login lookup tables, the
  ``credential_class_of(producer)`` mapping that the analysis layer
  uses for coarse bot-vs-human-vs-unknown plotting, and a
  ``rules_signature()`` over the lookup tables.
- `src/classifier/adapters.py` -- per-kind generators that fetch rows
  from sqlite and yield ``Record`` instances.
- `src/classifier/main.py` -- the orchestrator: walks each subject
  repo, runs every adapter, applies rules, writes verdicts.

The classifier is **time-aware in spirit**: a marker convention
introduced in week N should classify only artifacts created at or
after week N, with earlier artifacts left to other rules. The current
rule list does not yet exercise this -- predicates are
time-invariant -- because the rules we have so far (login matches,
email matches) are themselves time-invariant. When a time-conditioned
rule is added, the predicate inspects ``r.created_at`` directly.

The classifier preserves multiple verdicts per artifact (one per
source). Source values: ``marker`` (the only one currently
implemented; uses login, email, and other artifact-level signals);
``workflow_run`` and ``journal`` are reserved in the schema for
future enrichment from those data sources.

Layer 2 is **stateless across runs** in the sense that it can be
re-run from layer 1 output without requiring incremental bookkeeping.
Each run is tagged with a `classifier_version` string defined at the
top of `src/classifier/main.py`. Re-running with the same version
deletes existing rows for that version (scoped to the repo being
classified) before inserting fresh ones; re-running with a new
version adds new rows alongside the old, so verdicts from two
versions can be compared.

To guard against forgetting to bump ``CLASSIFIER_VERSION`` after a
rule change, the classifier maintains
``classifier:<version>:rules_signature`` in ``extraction_state`` and
checks it at startup. If the signature differs from what was stored
under the same version previously, the classifier refuses to run
until either the rules are reverted or the version is bumped.

### Layer 3 — analysis

Reads layers 1 and 2. Produces aggregate metrics, time series, and plots.

Layer 3 is the layer most likely to change frequently as we explore which
metrics tell which stories. The lower layers should be stable; the
analysis layer should not be.

Layer 3 opens the database via ``connect_readonly()`` rather than the
write-time ``connect()`` used by the extractors. This is enforced
architecturally: an analysis module that tried to ``INSERT`` would
raise an OperationalError. The SQLite file itself is opened with
``mode=ro`` in the URI; the surrounding directory must remain
filesystem-writable so SQLite can access WAL/shm sidecars.

Output of layer 3, per plot, in the typical case:
- A PNG (matplotlib) for paste-into-doc portability.
- An HTML file (Plotly) with embedded JS, opening in any browser, for
  interactive exploration with precise hover values.
- A CSV with the raw daily counts behind the plot.

The three forms have parallel names under
``output/plots/<repo>/``, ``output/html/<repo>/``, and
``output/csv/<repo>/`` respectively, so any single chart's numbers are
inspectable without re-running the analysis.

A few plots produce **HTML and CSV only**, no PNG. Specifically the
per-producer breakdowns -- ``bot_issue_producers`` in drilldown and
``bot_commit_producers`` in commit_authorship -- are stacked over many
bands (one per producer identity), and a static PNG with a busy stack
is hard to read: you can't tell which band is which without a legend
that doesn't fit, and you can't read exact values at all. The HTML
remains useful because hover surfaces the band identity and value,
and the CSV remains useful for direct inspection. Producing PNGs of
these would create visually plausible but actually-not-useful
artifacts. A future refinement would be to switch the per-producer
plots to a small-multiples layout (one panel per producer) that
renders well in both PNG and HTML.

The analysis layer currently exposes six entry points:
- ``src.analysis.first_look`` -- six daily-binned bot- vs.
  human-credential plots over GitHub-side data (issues, PRs,
  comments) plus two volume-calibration plots: a weekly
  stacked-bar of merged PRs by issues-closed bucket, and a
  growing column of per-week histograms of issues filed per human
  author from L5 onward.
- ``src.analysis.drilldown`` -- follow-ups that surface specific
  rows or per-actor breakdowns informed by what the first-look plots
  reveal. Outputs are mostly CSV tables; the time-series follow-ups
  also produce HTML.
- ``src.analysis.commit_authorship`` -- credential analysis at commit
  granularity using the git-extractor's
  [``commit_``](SCHEMA.md#commit_) table, plus a
  cross-tab joining commit authorship to PR identity.
- ``src.analysis.authorship`` -- Issue->PR producer cross-tabs using
  the full producer taxonomy from ``classifier/rules.py`` (not the
  coarse credential split). Two cross-tabs are produced: a
  full-history view, and a windowed view restricted to edges whose
  closing PR's ``merged_at`` is on or after a configurable cutoff
  (default ``2026-05-03``). Edges from ``linked_pr.pr_body_keyword``
  plus a 5-minute close-time heuristic.
- ``src.analysis.speed`` -- speed and cadence metrics as weekly
  time series (issue-to-first-linked-PR latency,
  PR-open-to-merge, fast-close count, MTTR with three
  methodologies × median/mean × two closure paths --
  issue-closed-by-PR vs. issue-closed-without-PR -- for 12 MTTR
  charts). Era boundaries annotated on every plot. Uses the full
  producer taxonomy.
- ``src.analysis.resolution_quality`` -- the two-regime
  resolution-quality signals as weekly time series
  (high-precision/low-recall: reopen-by-original-reporter,
  follow-up-citing-close-PR; low-precision/higher-recall:
  post-close phrase matches in ``explicit`` and ``general`` tiers,
  cross-reference events). Era boundaries annotated. Every output
  CSV is preceded by a caveat header naming the four shared
  limitations from this document. Uses the full producer taxonomy.

For the specific plots and tables each entry point produces today, see
[Current analysis outputs](#current-analysis-outputs) below.

In addition to the modules above, two volume-calibration SQL
queries live in README.md as ad-hoc recipes (one keyed on the issue
lifecycle, one on merged-PR creation). They are deliberately not
pipeline modules: they're one-shot characterizations whose output
shape is meant to be read directly by a human, not consumed
downstream, and changing them shouldn't require a rebuild. Keeping
them as recipes lowers the bar for adjusting the calibration as
questions evolve.

### Current analysis outputs

The list below enumerates exactly what each entry point produces today.
It will grow as we add plots; the layer's architecture above does not.

**Classification used in the current outputs.** Outputs split into
two groups by classification granularity.

The earlier modules -- ``first_look``, ``drilldown``, and
``commit_authorship`` -- split activity into ``bot-credentialed``
(actor login ends in ``[bot]``), ``human-credentialed`` (anything
else), and ``unknown`` (the field was NULL). This is a credential
classification, not a producer classification: human-credentialed
work is an upper bound on actual human work, since humans may run
automation under their own credentials.

The later modules -- ``authorship``, ``speed``, and
``resolution_quality`` -- use the full producer taxonomy from
``src/classifier/rules.py`` (``human-credentialed``, ``copilot``,
``claude-app``, ``hive-scanner``, ``hive-reviewer``, ``hive-bot``,
``prow``, ``project-bot``, ``netlify``, ``dependabot``,
``other-bot-app``, ``unknown``). They invoke the classifier rules
inline rather than joining to the
[``producer_classification``](SCHEMA.md#producer_classification)
table; verdicts are equivalent because the rule list is the same,
but a query that wanted verdicts at classifier-version granularity
would need to read the table.

**``first_look``: six daily-binned stacked-area plots plus two
weekly volume-calibration plots, per subject repo.** Each is
rendered to PNG, HTML, and CSV.

1. **Issues opened per day, by author credential** -- splits
   ``issue`` rows (``is_pr=0``) by the credential class of the opener.
2. **Issues closed per day, by closer credential** -- splits
   ``issue`` rows (``state='closed'``, non-NULL ``closed_by_id``) by
   the credential class of the closer.
3. **PRs opened per day, by author credential** -- splits ``issue``
   rows (``is_pr=1``) by opener credential.
4. **PRs merged per day, by merger credential** -- splits
   ``pull_request`` rows (``merged=1``) by the credential class of
   ``merged_by_id``.
5. **Comments on issues per day, by commenter credential** -- comments
   on issue rows where ``is_pr=0``.
6. **Comments on PRs per day, by commenter credential** -- comments on
   issue rows where ``is_pr=1``.
7. **Merged-PR close-count distribution per week**
   (``merged_pr_close_count_distribution``) -- weekly stacked-bar of
   merged PRs created that week, stratified by how many issues each
   PR closes (buckets 0, 1, 2, 3, 4, 5+). The ``0`` bucket measures
   "work that landed without closing an issue" -- a calibration of
   how much PR work happens entirely outside the issue lifecycle.
   Era boundaries annotated.
8. **Issues filed per human author per week, L5 onward**
   (``human_issue_distribution``) -- a growing single-column stack
   of per-week histograms. For each week from 2026-04-06 onward, one
   panel: x-axis is log-spaced bins of issues-filed-per-author for
   that week (1, 2, 3-5, 6-10, 11-20, 21-50, 51-100, 100+), y-axis
   is count of distinct human-credentialed authors in that bin.
   Distinguishes "few humans, many issues each" from "many humans,
   few each" -- the per-author distribution that the
   ``c_human``/``n_humans`` columns of the volume-calibration table
   in README.md cannot show. Both PNG and HTML render the same
   layout (vertical small-multiples, one row per week, independent
   y-axes); the figure grows linearly with the number of weeks.

**``drilldown``: follow-up artifacts informed by what first-look
revealed.** Mostly CSV tables; one stacked-area HTML plot.

1. **Bot-opened-issue producers** -- per-bot-account issue creation,
   broken down by author login. CSV summary (login, total, first_seen,
   last_seen) plus a stacked-area HTML plot of daily counts per login
   (HTML-only; see the note above on per-producer plots not producing
   PNGs). Surfaces which bot identities (auto-QA, link-checker, etc.)
   drive the bot-credentialed share of issue creation.
2. **Post-cutoff human PR authors** -- one row per
   human-credentialed PR opened on or after a configurable cutoff
   (default ``2026-05-03``). Plus a per-author summary. Identifies who,
   if anyone, still authors PRs by hand after the apparent transition
   to bot-driven authorship.
3. **PRs around the transition** -- a window of ``window_days`` days
   on each side of the cutoff, listing each PR's author and merger
   logins. Lets a reader see the actual identity flip across the
   inflection.

The cutoff and window are CLI flags (``--cutoff YYYY-MM-DD``,
``--window-days N``); the default cutoff is informally derived from a
pixellated reading of the first-look plots and should be refined as
data accumulates.

**``commit_authorship``: credential analysis at commit granularity,
plus disclosed-AI-collaboration signals from commit messages.** Reads
the git-extractor's ``commit_`` table. Credential classification
combines two signals: ``author_login`` ending in ``[bot]`` (derived
from GitHub noreply emails by the git extractor) and ``author_email``
matching a known bot-email allowlist (currently
``copilot@github.com``, ``scanner@kubestellar.io``,
``reviewer@claude-dev.local`` -- maintained additively as new
automation is observed).

1. **Commits authored per day, by author credential** -- daily
   stacked-area plot per repo (PNG + HTML + CSV), in the same shape
   as the first-look plots but at commit granularity.
2. **PR vs. commit-author cross-tab** -- one row per merged PR with
   the PR's author credential, the merger credential, and the
   credential of the author of the merge commit. Note: the merge
   commit's author is a coarse proxy for "what produced the PR's
   changes" -- for squash merges (the dominant pattern in console)
   it is the merger, not the feature-branch authors. Treated as
   exploratory. Output: a full per-PR CSV, a credential-cross-tab
   summary CSV, and a CSV restricted to the cell originally framed
   as analytically interesting (PR-author=bot,
   commit-author=human); on console this cell turned out to capture
   only the squash-merge artifact, not the broader pattern, so the
   CSV is small and primarily diagnostic.
3. **Bot-author commit producers** -- daily counts per bot login or
   bot-email producer (HTML-only plot plus CSV summary; see the note
   above on per-producer plots not producing PNGs), parallel to
   drilldown's ``bot_issue_producers``.
4. **Co-Authored-By trailer enumeration** -- one row per
   (commit, trailer) pair, plus a per-trailer-identity summary.
   Lets us discover which AI tools and identities have left
   disclosed traces in commit messages. The classification of
   trailer names/emails as "AI tool" uses a regex list with
   word-boundary semantics: specific tool/vendor names (``claude``,
   ``anthropic``, ``copilot``, ``cursor``, ``cody``, ``codeium``,
   ``aider``); generic automation markers (bare and hyphen-suffix
   forms of ``bot``, ``agent``; the GitHub-app ``[bot]`` form;
   ``assistant``); and a small set of project-specific identities
   (``kubestellar-hive``, ``kubestellar-bot``,
   ``scanner@kubestellar.io``, ``auto-qa``, ``GitHub Actions``,
   ``bob@example.com``). Word boundaries prevent false-positives on
   ``robotics``, ``agenda``, etc. The list is additive: when the
   enumeration CSV reveals an automation identity that isn't yet
   matched, add a regex for it.
5. **Disclosed-AI-collaboration time series** -- daily counts of
   commits split by whether the message discloses an AI tool via
   Co-Authored-By, vs. those that do not. The disclosed share is a
   *lower bound* on AI-assisted commits within the human-credentialed
   bucket: commits whose tool involvement was undisclosed (trailer
   omitted or stripped) are indistinguishable from hand-typed
   commits at the metadata level. Useful for tracking era
   transitions in a project's adoption of AI-assisted development.

**``authorship``: Issue->PR producer cross-tabs.** Edges from
``linked_pr`` rows with ``link_source = 'pr_body_keyword'``
(populated by the GitHub extractor from ``closes``/``fixes``/
``resolves`` keywords in PR bodies) plus an in-memory close-time
heuristic where the issue's ``closed_at`` is within 5 minutes of a
PR's ``merged_at`` and the closer matches the PR merger or author.
Edge sources are kept distinct in the per-edge CSV so the reader
can see how much of either cross-tab comes from each.

1. **Full-history cross-tab** -- counts of (issue producer, PR
   producer) pairs across all edges. CSV plus heatmap PNG/HTML.
2. **Windowed cross-tab** -- restricted to edges whose closing PR's
   ``merged_at`` is on or after the configured cutoff
   (``--start-date YYYY-MM-DD``, default ``2026-05-03``). Same shape
   as the full-history cross-tab; the cutoff is intended to bracket
   the L5->L6 hive handoff so the post-handoff shape can be read
   independently of the pre-handoff history. CSV plus heatmap
   PNG/HTML; output filenames carry the ``_since_<DATE>`` suffix.
3. **Per-edge dump** -- one row per (issue, PR, edge_source) tuple
   with both endpoints' producer classifications, for follow-up
   inspection. Carries both the analysis database's primary keys
   (``issue_id``, ``pr_id``) and the GitHub-visible numbers
   (``issue_number``, ``pr_number``) so a row can be looked up
   directly on the GitHub UI; also carries ``pr_merged_at`` so the
   same file can be re-windowed without re-running the module.

**``speed``: speed and cadence metrics, weekly time series.** Per
DESIGN.md these are speed metrics, not quality metrics; a
high-throughput system can be shipping bad work quickly. Per the
era-awareness mandate the corpus is non-stationary on a timescale
of weeks, so all four metrics here are weekly time series with
era-boundary annotations rather than aggregates over the full
window. Per-issue / per-PR values are placed in a weekly bin by
the closing PR's ``merged_at``, with a fallback to the issue's
``closed_at`` for issues closed without a linked merged PR (those
issues are included rather than dropped).

1. **Issue open -> first linked PR merge** -- weekly median by
   issue-author producer of the per-issue interval. Edges from
   ``linked_pr`` only. One line per producer; era boundaries
   annotated.
2. **PR open -> merge** -- weekly median by PR-author producer of
   the per-PR interval. One line per producer.
3. **Fast-close** -- weekly count of issues closed within
   ``--fast-close-threshold-minutes`` minutes (default 5) of being
   opened, stacked by closer producer. Replaces the previous
   post-cutoff histogram.
4. **MTTR** -- twelve separate charts: three methodologies
   (``first_close``, ``cumulative_open``, ``final_close``) × two
   statistics (median, mean) × two closure paths
   (``_pr`` for issues closed by a linked merged PR; ``_no_pr`` for
   issues closed without one). Each is per-week per-closer-producer
   as a line chart on a log y-axis. ``first_close`` is
   ``created_at -> first observed close``; ``cumulative_open`` is the
   sum of ``(open -> close)`` intervals across the issue's life,
   excluding closed-then-reopened gaps; ``final_close`` is
   ``created_at -> last observed close``. Reopen counts and the
   ``closed_by_pr`` flag are recorded per issue in the per-issue CSV
   so a reader can compute methodology gaps (DESIGN.md flags the
   cumulative-vs-final gap as informative) or filter populations.

**``resolution_quality``: triangulation signals across two precision
regimes, weekly time series.** Per the "Resolution-quality signals"
section below, no single signal here is a measurement; convergence
across signals is informative, divergence is ambiguous, low readings
everywhere are *not* evidence the underlying phenomenon is absent.
All four metrics are weekly counts stacked by the relevant producer,
binned by the timestamp of the **signal trigger** itself (the event
or comment that constitutes the signal). Era boundaries are
annotated on every plot. Every output CSV is preceded by a
single-line caveat header naming the four shared limitations: silent
drops, bidirectional adoption lag, attention non-uniformity,
multi-case bundling.

High-precision / low-recall:

1. **Reopen by original reporter** -- ``issue_event`` reopens where
   ``actor_id == author_id``. Per-event CSV plus a weekly count
   stacked by closer producer (CSV + PNG + HTML).
2. **Same-reporter follow-up citing closing PR** -- for each closed
   (issue, closing-PR) pair from ``linked_pr``, later same-reporter
   issues whose body or first comment contains either
   ``owner/repo#N`` or bare ``#N`` matching the closing PR's number.
   Per-edge CSV plus a weekly count stacked by reporter producer.

Low-precision / higher-recall:

3. **Post-close phrase matches** -- two tiers (``explicit``,
   ``general``) of dissatisfaction phrases substring-matched
   (case-insensitive) against comments posted after the issue's
   ``closed_at``. The tier names describe what each list contains
   rather than a precision ordering -- see the comment on
   ``PHRASE_TIERS`` in ``resolution_quality.py`` for why
   "stronger / weaker" turned out to mislead, and why
   ``regression`` was removed from the explicit list. A comment
   hitting both tiers gets two rows. Per-match CSV plus, per tier,
   a weekly count stacked by closer producer (CSV + PNG + HTML).
   The per-match CSV carries an ``is_reporter_followup`` flag for
   readers who want to subset to comments by the original reporter.
4. **Cross-reference patterns** -- ``cross-referenced`` events on
   closed issues. The per-event CSV preserves
   ``before_close`` / ``after_close`` / ``never_closed`` so a reader
   can subset by direction. The weekly time-series plot is a single
   stacked-bar series across all directions, by closer producer.

### Layer 4 — interpretation (out of scope for code)

Human reading of layer 3 outputs against the questions that motivated the
analysis. Decisions about what to investigate next.

## What "agentic development" means for this analysis

The repositories under study process work via several producers, only
some of which are humans. The classifier's current producer labels
(see ``src/classifier/rules.py`` for the canonical list and the
predicates that produce them):

- ``human-credentialed`` — author login does not end in ``[bot]`` and
  the email is not on the known-bot allowlist. Upper bound on actual
  human work; the credential field cannot distinguish human-typed work
  from human-tool-orchestrated work, and does not account for humans
  running bots under their own credentials.
- ``copilot`` — GitHub Copilot dispatched as automation (covers
  ``copilot[bot]``, ``copilot-pull-request-reviewer[bot]``,
  ``copilot-swe-agent[bot]``, and the ``copilot@github.com`` email).
  ``sub_producer`` carries the specific identity.
- ``claude-app`` — the ``claude[bot]`` GitHub App identity (distinct
  from ``hive-reviewer``, which is identified by the
  ``reviewer@claude-dev.local`` email rather than a GitHub App).
- ``prow`` — the ``kubestellar-prow[bot]`` actor (labeler,
  APPROVALNOTIFIER, label events). Prow is installed but does not
  actually gate console merges; its presence in the data is mostly
  bookkeeping artifacts.
- ``netlify`` — the ``netlify[bot]`` actor (deploy-preview comments).
- ``dependabot`` — the ``dependabot[bot]`` actor.
- ``hive-bot`` — the ``kubestellar-hive[bot]`` GitHub App identity,
  regardless of role. The same identity authors issues, opens PRs,
  and merges PRs; the classifier sees only the actor identity, so
  this label is identity-only. Analyses that care about the merging
  role specifically should join via PR-merger fields, not via this
  artifact-author classification.
- ``hive-scanner`` — hive's scanner sub-agent, identified by the
  ``scanner@kubestellar.io`` email and related kubestellar-org email
  identities.
- ``hive-reviewer`` — the Claude-driven reviewer that committed under
  ``reviewer@claude-dev.local``. (No dedicated GitHub-app login; the
  email is the only signal.)
- ``project-bot`` — kubestellar-org bot identities seen in
  Co-Authored-By trailers or in non-noreply emails (``ks-ci-bot``,
  ``kubestellar-bot``, ``auto-qa``, ``kubestellar-console-bot``,
  others).
- ``other-bot-app`` — any GitHub App login ending in ``[bot]`` that
  isn't more specifically classified above. Currently dominated by
  ``github-actions[bot]``, which is the runner identity for *any*
  GitHub Actions workflow that posts/comments/files things; further
  splitting requires content-based signals (workflow name, comment
  body parsing) the classifier does not yet consume.
- ``unknown`` — the artifact has no actor identity at all.

Co-Authored-By trailer detection (the disclosed-AI-collaboration
signal) is currently a separate code path inside the
``commit_authorship`` analysis, not part of the classifier rule list.
It produces a complementary lower-bound signal on AI-assisted commits
that escaped the credential classification (developer commits under
their own identity but discloses AI involvement via a trailer).

The taxonomy is an evolving artifact. As workflows are added,
removed, or modified in the subject and support repositories, the
classifier rules need to track these changes. The
``classifier_version`` column on ``producer_classification`` and the
``rules_signature()`` integrity check let us re-run when the taxonomy
changes; old verdicts are preserved alongside new ones for
comparison.

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
  [`workflow_file_state`](SCHEMA.md#workflow_file_state) returning
  the latest commit-content prior to T for each path. Any query about
  behavior at T should resolve via this pattern, not by sampling at
  coarser intervals.

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

## Metric inventory

These are the metrics anticipated for the analysis layer. The
categorization below is deliberate: speed metrics, behavior metrics,
and quality metrics measure different things and shouldn't be conflated.
Not all will be implemented in the first cut; the list is meant to be
aspirational and evolving.

### Era awareness (applies to every metric)

The ``kubestellar/console`` repo has gone through six ACMM layers in
its lifetime, each with distinct characteristics. The v2 ACMM paper
identifies differences across at least:

- The agent-instruction layer: none in L1, evolving through L2's
  introduction of CLAUDE.md and copilot-instructions, to L6's
  per-agent CLAUDE.md files (e.g. hive's 902-line scanner instructions).
- The tool stack: prompt-and-review in L1, testing infrastructure
  in L3, triage loops and auto-QA in L4, full agent fleets in L6.
- Feedback-loop sophistication: open in L1-L3, closed-loop with
  the system measuring and reacting to itself starting in L4.
- The human role: producer (L1-L3), reviewer (L4-L5), strategist
  setting direction with humans gating queue config rather than
  individual artifacts (L6).
- Throughput: each layer higher than the last; L6 dramatically so.
- Attribution discipline: less structured agent attribution in
  earlier layers, with conventions for marking AI-assisted work
  evolving over time.

The whole lifetime of `kubestellar/console` has been an experiment
in coding-agent use; no era was non-agentic. What changes across
eras is the structure, discipline, and observability of the
agentic activity.

The attribution-discipline difference is the one our measurement
framework most directly contends with: earlier layers had less
structured attribution, so author-credential classification is less
reliable as a measure of human-vs-agent split during those eras.
The visible "human-credentialed" share in the early period may
overstate human activity because agent activity was less detectable.
Other differences (the agent-instruction layer evolving, the
feedback loops appearing, the role shifts) are not directly
captured by our author-credential metrics; they require different
measurement strategies (looking at the workflow and policy file
trees over time, looking at workflow run patterns, or going outside
Git/GitHub data entirely).

Whether the analyses here correctly characterize any specific era
is something only a reader with ground truth about that era can
evaluate. The numbers and curves are the analysis's view of what's
visible; whether that view matches the era's reality is a separate
question, and the analysis itself cannot answer it.

When plotting metrics across the repo's lifetime, mark era boundaries
as point annotations on the time axis where feasible -- the
continuous appearance of the curve obscures categorical
differences across eras.

### Authorship and orchestration mix (descriptive)

These are the foundational descriptive plots -- they show *who* does
*what*, without claiming any of it is good or bad.

- PR authorship by producer, daily, time series
  (already in first_look, currently as bot-vs-human credential class
  rather than full producer taxonomy)
- PR merger by producer, daily, time series
  (already in first_look, same caveat)
- Issue authorship by producer, daily, time series
  (already in first_look, same caveat)
- Issue → PR producer mapping (who-wrote-issue × who-wrote-PR)
  (in `authorship.py`; two cross-tabs as heatmap PNG/HTML --
  full-history and windowed since a configurable cutoff -- plus a
  per-edge CSV that carries `pr_merged_at` for re-windowing; edges
  from `linked_pr.pr_body_keyword` plus a 5-minute close-time
  heuristic with edge sources kept distinct; not currently broken
  out by week)

### Speed and cadence (not quality)

These measure how fast the system clears work, not whether the work
is good. A high-throughput system can be shipping bad work quickly.

- PRs merged per day, segmented by producer (already in first_look)
- Issues opened per day, segmented by producer (already in first_look)
- Time from issue open to first PR linked to it
- Time from PR open to merge, distribution
- Issues closed within N minutes of opening (right-tail of fast closes)
- **Mean Time To Resolution** of issues, broken out by closer
  producer. Methodologies diverge in the presence of reopens, so
  all three are reported:

  * *first-close interval*: open → first close. Smallest of the three.
    Treats the issue as resolved at the first close even if it was
    later reopened. Most "MTTR" tools use this by default.
  * *cumulative-open time*: sum of all (open → close) intervals
    across the issue's life. Excludes the closed-and-then-reopened
    gaps where nobody was working on it. Best matches "how long was
    this an active concern."
  * *final-close interval*: open → final close. Wall-clock from
    initial report until things stabilized, including any
    abandoned-then-resumed periods. Largest of the three.

  Each methodology is plotted as a weekly time series at both median
  and mean, and each (methodology, statistic) pair is split by
  closure path (issue closed by a linked merged PR vs. closed
  without one). That yields 3 × 2 × 2 = 12 charts. Filenames look
  like ``speed_mttr_first_close_median_pr.{png,html,csv}`` and
  ``..._no_pr.{...}``. Reopen counts and the per-issue values for
  all three methodologies are written to ``speed_mttr_per_issue.csv``
  so a reader can compute methodology gaps (e.g., cumulative-open
  vs. final-close) or filter populations.

### Behavior and churn (descriptive, not evaluative)

These measure what the codebase looks like over time. They are
*descriptive* -- they tell us about the shape of churn, not whether
it indicates good or bad work. A line removed by a later PR could
be a quality fix, a refactor, completion of work that was a sketch
in the earlier PR, or a response to a changed requirement; the
metric does not distinguish among these. Treat producer cross-tabs
as descriptive break-outs, not quality verdicts.

- Same-file touch-return rate, family of curves over multiple windows
- Line survival curves per cohort, segmented by producer of the
  addition AND producer of the removal (four cells:
  bot/bot, bot/human, human/bot, human/human). The four-cell view
  is descriptive; specifically, it cannot be read as "agentic system
  fixing its own work" without an external signal of intent (see
  below on commit-message keyword scans).
- Per-file commit frequency over time
- Comparison of survival distributions across the agentic-handoff
  boundary -- whether the shape changed when hive came online

### Engagement signals (procedural, quality-adjacent)

Useful for the "is anyone reading what's being shipped" question.
Procedural rather than evaluative on their own.

- Comments per PR before merge, distribution
- Fraction of PRs with any human-credentialed comment before merge
- Reviews per PR (formal)

### Resolution-quality signals (triangulation, not measurement)

The underlying question -- "did the resolution actually resolve the
problem the reporter faced" -- is **fundamentally not measurable**
from GitHub data alone. Reporter satisfaction lives in someone's
head, not in the data; we can only enumerate observable proxies and
let them triangulate. Convergence across multiple proxies is
informative; divergence is ambiguous. Reporting any single number
from any single proxy is misleading.

#### Shared limitations affecting all signals

Before the per-signal entries, four limitations apply to every signal
in this section:

1. **Silent drops.** A reporter who hits a problem, sees the issue
   closed in a way that didn't help them, and just moves on (uses a
   different tool, files no follow-up) produces no signal at all.
   Each metric in this section is a count of *signal triggers*.
   Each trigger is evidence that dissatisfaction exists somewhere.
   Many cases of dissatisfaction trigger no signal -- silent drops,
   adoption lag, attention gaps -- so the count is a lower bound on
   the number of dissatisfaction cases, with unbounded slack between
   the count and the truth. The framing is asymmetric: a high count
   in any signal is real evidence that the underlying phenomenon
   exists; a low count in every signal is *not* evidence that the
   phenomenon is absent.

2. **Adoption lag is bidirectional.** It degrades both precision and
   recall of time-windowed signals.
   - *Recall-degrading direction*: a reporter on an old version files
     a follow-up after the fix was merged but before they upgraded.
     If the metric requires the follow-up to be near the closing PR
     in time, it misses these.
   - *Precision-degrading direction*: a reporter files a fresh
     complaint about behavior that was just fixed in the latest
     release, but they're running an older release. The metric flags
     it as resolution-quality dissatisfaction; it's actually adoption
     lag noise.

   A partial mitigation specific to this project is described under
   "Version-aware filtering" below.

3. **Attention non-uniformity.** Reporters and observers are not
   full-time on the project; they have other work, take vacations,
   may not visit GitHub for days or even weeks at a time. The
   relevant gap can be as short as a long weekend. Time-windowed
   metrics that pick a single N-days window miss real follow-ups
   that happened later and admit unrelated activity that fell
   within the window. Reporting results across multiple window
   widths is the realistic defense.

4. **Multi-case bundling.** A "follow-up" can be many different
   things: restating the same complaint, flagging that the
   resolution introduced a new problem, flagging that the resolution
   addressed only part of the problem, citing the closing PR with
   "this only handles A, not B," etc. Several proxies blur these
   together; entries below say which one each catches.

Each per-signal entry below names that signal's individual
precision/recall character on top of these shared limitations.

#### Per-signal entries

These are split into two precision/recall regimes. **High-precision
signals** rarely fire when nothing's wrong, but most cases of
dissatisfaction don't trigger them. **Low-precision, higher-recall
signals** flag many things, only some of which are real
dissatisfaction; they're useful as triggers for human review or as
volume measurements, not as definitive counts.

**High-precision, low-recall (when they fire, they almost always
indicate dissatisfaction):**

- **Reopen by original reporter.** When the original reporter
  reopens, that's nearly definitive evidence the close was
  premature. Sub-segment: reopened by original reporter vs.
  reopened by someone else vs. reopened by maintainer (each carries
  different signal).
- **👎 reaction on a closing comment** (or 👍 on a "still broken"
  follow-up). Explicit dissatisfaction. Sparse where present.
- **Same-reporter follow-up that explicitly cites the closing PR.**
  Phrases like "after #N", "fix from #N didn't work", "this regressed
  in #N", or GitHub cross-reference timeline events linking back to
  the closing PR. Catches both "resolution introduced a new problem"
  and "resolution addressed only part of the problem" cases.
- **Maintainer-marked duplicate** (`state_reason='duplicate'`). High
  precision because a triager actively decided two issues were the
  same problem. Recall depends on triager diligence.

**Low-precision, higher-recall (these flag many things that aren't
necessarily dissatisfaction):**

- **Same-reporter re-filing rate** (any new issue by reporter R
  within window W of one of R's earlier closes, in the same repo).
  Some of those re-filings are about the resolution; many are
  unrelated. Useful as a volume signal across producer classes
  rather than as a count of dissatisfaction.
- **Cross-reporter same-area volume.** New issues in the same label
  or feature area as a recently-closed one, by people other than the
  original reporter. Catches the "person B hits the same broken
  behavior person A reported" case but with substantial false
  positives from genuinely-different bugs in the same area.
- **Title/body text similarity** between a new issue and a recently
  closed one, above a threshold. Higher recall than explicit citation
  but much noisier; threshold is a judgment call.
- **"Still broken" / "doesn't fix" / "didn't work" / "what about Y"
  phrase matching** on post-close comments. Recall depends on whether
  the commenter uses the matched phrasing; precision depends on
  whether the comment is actually about *this* close. Higher precision
  when narrowed to comments by the original reporter (graduating
  toward the high-precision tier).
- **Cross-reference patterns**: how many issues cite a given closing
  PR in the days/weeks after merge, by people other than the merger.
  The presence of post-merge citations is a signal that the close is
  being talked about; whether the talk is "this fix is good" or "this
  fix broke things" is ambiguous from the count alone.

#### Version-aware filtering (partial adoption-lag mitigation, console-specific)

The `kubestellar/console` issue-reporting template asks reporters
for the version they're using, often as specifically as a git
commit sha. That signal -- when present -- is enough to convert the
adoption-lag confound from an unknown bias into a filter.

This is a feature of `kubestellar/console`'s template
specifically, not a uniform feature of the kubestellar
organization. Other repos in the org may not request a version,
or may request it in a different form. Extending this mitigation
to docs (or any other future subject) requires a per-repo check of
the issue template; we don't get it for free across the
organization.

The mitigation, when implemented for console, would:

1. Parse the reporter's version from the issue body.
2. Map the version (release tag, branch name, or sha) to the set
   of commit shas it includes.
3. For each follow-up signal, check whether the reporter's reported
   version actually contained the closing PR's commit.
   - If it did: the complaint is **strong evidence** of resolution
     dissatisfaction; promote it in the precision tier.
   - If it didn't: the complaint is **adoption-lag noise**, not
     evidence about the fix; exclude from quality signals.
   - If the reporter didn't supply a version: the signal stays in
     its baseline tier.

We don't currently extract this signal; implementing it is a real
piece of work (parsing the body, mapping versions to shas via
tag/release walks) but tractable for the repos whose templates
support it.

#### Reading the signals together

The recommendation: report multiple signals from both regimes
together, rather than picking one. **Convergence** -- multiple
high-precision signals firing on the same artifact, plus a
low-precision signal -- is strong evidence. **Divergence** --
high-precision signals quiet but low-precision signals elevated --
is ambiguous: it could be a noisy area, or it could be widespread
dissatisfaction that's not surfacing in the high-precision channels.
The asymmetry is the honest framing: a high reading in any signal
is evidence the underlying phenomenon exists; a low reading in
every signal is *not* evidence the phenomenon is absent (silent
drops and adoption lag together can keep all signals quiet).

**The reopen channel is structurally narrow.** The reopen-based
high-precision signals require the reporter to notice the close
didn't help, return to GitHub, find the closed issue, and explicitly
reopen it -- the very burden the precision/recall framing implicitly
assumed reporters would bear. Most reporters drop silently after a
close, so the population that *can* trigger a reopen-based signal is
a small fraction of the population whose dissatisfaction is at
issue. This is not a limitation of the metric; it is a property of
the channel. In ``kubestellar/console`` specifically, the
volume-calibration query in [CALIBRATION.md](CALIBRATION.md) gives
the population-wide upper bound: at the L5 peak only ~5.4% of
issues created that week were ever reopened by anyone, and post-L5
the rate sits below 1%. Those numbers are corpus-and-time-specific
(see CALIBRATION.md's recency note), but the structural narrowness
they expose generalizes: a "high count" reading on
``_reopen_by_original_reporter`` is small in absolute terms by
construction, and a "low count" reading is essentially uninformative
about the underlying phenomenon. The asymmetric framing above
applies with extra force.

#### A separate signal not in the precision/recall framing

- **Commit-message keyword scan.** Counts of commits whose messages
  contain "revert", "fix", "rollback", "broken", etc., aggregated
  by author/producer. A cheap proxy for "this commit was undoing or
  correcting prior work." Doesn't fit the issue-resolution-quality
  framing above; it's a separate signal about the commit stream
  itself. Useful as a level to compare across producer classes and
  across the agentic-handoff boundary.

### Self-quality of the analysis (not subject-quality)

Not a quality metric of the analyzed code, but worth tracking
alongside the others:

- **Cross-version classifier verdict comparison.** When the rule list
  improves, comparing producer classifications across versions shows
  where the analysis was previously fooled. The diff between v1 and
  v2 is itself a measurement of how much the older view was wrong.

### Agentic infrastructure (descriptive)

Tracks the rate at which the agentic system's own infrastructure
changes. Descriptive, not quality.

The "agentic system's own infrastructure" is broader than
``.github/workflows/``. The corpus splits into two eras:

- *Pre-hive era* (repo start through the May 1-3 handoff). Spans
  ACMM layers 1 through 5. Infrastructure includes GitHub Actions
  workflows under ``.github/workflows/`` in the subjects, reusable
  workflows imported from ``kubestellar/infra``, and the
  per-CLAUDE.md instruction files the subjects ship for use by AI
  tools. The v1 ACMM paper additionally listed agents that operated
  in this era (we believe most of them appear in ``kubestellar/console``'s
  commit history; surfacing them is an analysis we have not yet
  run -- a SQL query against ``commit_file`` for paths matching
  agent conventions would reveal them, with cohort analysis showing
  when each came online). One phase late in this era is Andy's use
  of Claude Code with looping; the infrastructure for that phase
  included Andy's prompts, loop scripts, and Claude Code
  configuration. Some of that may have been checked into a repo;
  some may have lived on Andy's workstation only. The portion that
  wasn't checked in is genuinely unobservable from our data.
- *Hive era* (since the May 1-3 handoff). Everything in the pre-hive
  list, plus hive's own configuration -- its ``agents/``, ``bin/``,
  ``config/`` files, including the per-agent CLAUDE.md files (e.g.
  the 902-line scanner CLAUDE.md), and the scheduled-runner setup
  hive uses on the host where it runs.

What we currently observe and don't:

- We capture per-commit content of workflow files in subjects
  (``console`` and, when added, ``docs``) and in the currently-narrow
  ``support`` role for ``kubestellar/infra`` -- the reusable workflows.
  We do not currently capture the broader file content of hive
  (its ``agents/``, ``bin/``, ``config/``); see open task #16 on
  broadening the path filter for support repos.
- We capture commit-level metadata for hive (commit dates, authors,
  per-file change counts and types) but not the file contents
  themselves. So we can answer "when did hive's policy churn" but
  not "what did the policy look like at time T."
- We capture the full commit history of ``console``, including
  whatever agent definitions or prompts were checked in there during
  the pre-hive era. Whether the agents the v1 paper described are
  fully captured depends on whether they were committed; if they
  were, we have them. We have not yet run the analysis to enumerate
  them.
- For the Andy-Claude-Code-loop phase, configuration that wasn't
  checked into a repo we extract is unobservable. Any analysis claim
  about that phase needs that limitation stated.

Metrics in this category, with the observability of each:

- Number of distinct workflow files active per day
  (workflows in subjects + infra; computable today)
- Workflow file change rate, uncollapsed and flurry-collapsed
  (computable today for workflows in subjects + infra)
- Disabled/re-enabled status of each workflow over time
  (inferred from gaps in workflow run streams; not yet implemented;
  see the open question on capturing web-UI disables)
- Pre-hive-era agent enumeration (a SQL query against
  ``commit_file`` for paths matching agent conventions, with first/
  last seen per path; computable today, not yet run)
- Hive policy churn rate (commit count per day in hive's
  ``agents/``, ``bin/``, ``config/``; computable today since we
  have hive's commit metadata, just not the file contents)
- Hive policy content snapshots over time (not computable today;
  blocked by open task #16)

### Subject-area splits

For each metric above where it's meaningful: split by

- Issue/PR kind (bug, feature, doc, etc.)
- Tier classification (tier/0..3, where the label is present)
- Whether the change touches `.github/workflows/` (i.e., is the
  agentic scaffolding being modified)

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

The analysis-repo working tree lives alongside the cloned analysis-target
repos as **siblings** under a common parent directory. The analysis repo
does not contain the cloned target repos; the docker invocation
bind-mounts the sibling clones into the container at `/repos`.

Host layout:

```
<parent>/
+-- console-analysis/                  # this repo
|   +-- SCHEMA.md
|   +-- DESIGN.md
|   +-- README.md
|   +-- TASKS.md
|   +-- LICENSE
|   +-- .gitignore                     # excludes data/, output/, config.yaml
|   +-- Dockerfile
|   +-- requirements.txt
|   +-- config.yaml.example
|   +-- config.yaml                    # gitignored
|   +-- data/                          # gitignored
|   |   +-- db.sqlite                  # the sqlite database
|   |   +-- snapshots/                 # pre-phase VACUUM INTO snapshots
|   |   \-- gh_runs/                   # gzipped logs and artifacts (per-run)
|   |       \-- <repo_id>/<run_id>/
|   |           +-- logs.tar.gz
|   |           \-- artifacts/...
|   +-- output/                        # gitignored; analysis layer's outputs
|   |   +-- plots/<repo>/              # PNGs (matplotlib)
|   |   +-- html/<repo>/               # HTMLs (Plotly, embedded JS)
|   |   \-- csv/<repo>/                # raw daily counts behind each plot
|   +-- src/
|   |   +-- schema.sql                 # DDL matching SCHEMA.md
|   |   +-- extractor_github/          # layer 1, GitHub side
|   |   +-- extractor_git/             # layer 1, git side
|   |   +-- classifier/                # layer 2
|   |   +-- analysis/                  # layer 3
|   |   \-- common/                    # shared utilities
|   \-- tests/
+-- console/                           # subject repo (cloned by user)
+-- docs/                              # subject (when added)
+-- hive/                              # support (supervisory layer)
\-- infra/                             # support (reusable workflows)
```

The `data/` and `output/` directories are gitignored. The cloned
sibling repos are not part of this repo; the user maintains them
separately. None of the analysis code writes to those clones.

Container view: when the docker invocation mounts `$(cd .. && pwd)`
as `/repos:ro`, the container sees the sibling clones at
`/repos/console`, `/repos/hive`, `/repos/infra`, etc. The
`config.yaml` in the container references those container-side paths
in its `local_clone:` fields. (See `config.yaml.example` for the
fully-resolved paths the example uses.)

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
    name: hive
    role: support                       # supervisory layer; not a subject
    local_clone: ../hive
  - owner: kubestellar
    name: infra
    role: support
    local_clone: ../infra
extraction:
  log_fetch_concurrency: 5             # respect secondary rate limits
  fetch_logs: false                    # see "Log fetching deferred" below
  fetch_artifacts: false               # see "Log fetching deferred" below;
                                       # also produced 159 GB on first try
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
- **Log fetching deferred.** Two attempts at fetching workflow run logs
  produced SQLite database corruption mid-run (different b-trees damaged
  on different runs, but always during the logs-fetch phase). The
  failure mode -- localized b-tree damage to whichever table was being
  most aggressively written at the time -- is consistent with SQLite's
  documented warnings about layered/virtualized filesystems whose
  locking or fsync semantics may be unreliable; see "Database integrity
  defenses" above. The pipeline currently defaults to
  `fetch_logs: false`. Run metadata (workflow_path, workflow_name,
  actor, event, conclusion, head_sha, timestamps) is fully captured and
  is sufficient for the human/agent characterization the analysis is
  initially targeting. If the analysis later requires log content, the
  candidate fixes -- in increasing order of disruption -- are: lower
  fetch concurrency to 1; fetch logs as a separate process that does
  not touch SQLite at all and reconciles after; or do the
  named-volume migration described under "Open design questions".
- **Datetime arithmetic stays unit-aware.** SQLite-stored ISO
  timestamps come back through ``pd.to_datetime(..., utc=True)`` as
  ``datetime64[us, UTC]`` on the pandas/numpy versions the container
  currently pins (pandas 3.x, numpy 2.x). ``Timestamp.value`` and
  ``Timedelta.value`` are unconditionally nanoseconds, so a bare
  ``astype('int64')`` on a datetime Series silently yields
  microseconds and any comparison against ``.value`` is off by 1000x
  -- a class of bug that produces zero matches rather than a loud
  failure. The convention in this codebase is therefore to do
  datetime arithmetic via ``Timestamp - Timestamp``,
  ``Series.dt.total_seconds()``, and ``Series.dt.floor("D")``, which
  are all unit-aware. The one place that needs an int64 fast path
  (``authorship._heuristic_edges``'s binary search) goes through an
  explicit ``.astype("datetime64[ns, UTC]").astype("int64")`` to
  force the unit before integer arithmetic. New code that wants to
  mix datetime-derived integers with ``Timestamp.value`` /
  ``Timedelta.value`` should follow that pattern.

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

- **Move the SQLite database off the bind-mounted filesystem.** SQLite
  documents that broken locking or unreliable fsync on the underlying
  filesystem can corrupt the database, and explicitly identifies WAL
  checkpoints as the one operation in WAL mode where unreliable fsync
  can still cause corruption (see
  [How To Corrupt An SQLite Database File](https://www.sqlite.org/howtocorrupt.html)).
  Docker's own guidance recommends named volumes -- which live on the
  VM's native ext4 rather than going through the host-bind-mount layer
  -- as the preferred mechanism for performance-sensitive persistent
  data (see [Docker volumes documentation](https://docs.docker.com/engine/storage/volumes/)).
  Migration shape: the database lives in a Docker named volume mounted
  at a fixed in-container path; the bind-mounted `data/` keeps the
  on-disk file artifacts (`gh_runs/`, snapshots) and the analysis
  outputs. Host-side inspection of the database happens by either
  `docker cp` of a snapshot, or a separate read-only inspection
  invocation that mounts the volume into a short-lived container.
  This is the structural fix to the corruption issues we hit; not yet
  implemented because the current workload appears to fit within what
  the partial mitigations cover. Triggers for doing the migration:
  another corruption recurrence, or a decision to enable log fetching
  again.
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
- **Walk full PR commit ancestry for the PR-vs-commit-author
  cross-tab.** ``commit_authorship.pr_vs_commit_authors`` currently
  joins each PR to a single commit -- the PR's merge commit -- as a
  coarse proxy for "what produced the PR's changes." For
  squash-merged PRs (the dominant pattern in console) the merge
  commit's author is the merger, not the feature-branch authors, so
  the proxy collapses to a trivial restatement of "who clicked
  merge." A more thorough version would walk back from
  ``merge_commit_sha`` through the PR's ancestry, enumerate all
  feature-branch commits, and report the set of distinct commit
  authors per PR. Implementation: either subprocess
  ``git log <base>..<head>`` per PR, or walk ``commit_.parent_shas``
  in the database. Not currently driven by an analysis question --
  the simple version showed only 8 cells of interest, all from a
  brief overlap window during the May 1-3 handoff -- but the proper
  version is the right thing if we ever want to ask "which PRs
  contained any human-authored commits."
- **Comment-body parsing to split ``github-actions[bot]``.** The
  classifier's largest single producer is ``other-bot-app`` with
  ``sub_producer=github-actions[bot]`` -- 33,000+ records, all
  bundled together. ``github-actions[bot]`` is the runner identity
  for any GitHub Actions workflow that posts/comments/files, so the
  bundle covers everything from typo-checker comments to Netlify
  status posts to auto-QA reporters. Splitting it usefully requires
  a different signal than the actor login alone. Candidates: parsing
  comment bodies for telltale strings (e.g., ``[Auto-QA]``,
  ``Netlify``, ``tide``, ``prow``); cross-referencing comments to
  workflow runs by ``head_sha`` and timing; or matching title
  prefixes for issues/PRs created by these workflows. The right
  version is probably workflow-run cross-reference for accuracy
  combined with body parsing as a cheap fallback. Currently deferred
  because the unsplit ``github-actions[bot]`` bucket is recognizable
  enough to set aside in analyses that don't need it, and splitting
  is a meaningful piece of work.
- **Version-aware filtering of resolution-quality signals.** The
  shared limitations on dissatisfaction signals include adoption
  lag, which biases time-windowed signals in both directions. A
  partial mitigation works for `kubestellar/console` because its
  issue-reporting template asks for the version the reporter is
  using, often as a release tag or git commit sha. (This is a
  feature of console's template specifically, not of the
  kubestellar organization; other repos like docs may have
  different templates and would need separate verification.)
  Extracting that signal converts adoption lag from an unknown
  bias into a filter -- complaints from reporters who were on a
  version *not* containing the closing PR's commit are
  adoption-lag noise and should be excluded from quality signals;
  complaints from reporters who *were* on a version including the
  fix become much stronger evidence of resolution dissatisfaction.
  Implementation requires (a) parsing the version field from the
  issue body, (b) building a version-to-commit-shas map by walking
  the relevant repos' tags
  and release history. Tractable in this project because of the
  template discipline. Not yet implemented; described in detail in
  the "Resolution-quality signals" section under "Version-aware
  filtering."
