-- Copyright 2026 Mike Spreitzer
-- SPDX-License-Identifier: Apache-2.0
-- Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

-- Schema for console-analysis sqlite store.
-- See SCHEMA.md for the human-readable specification of why each table is
-- shaped the way it is. This file is the canonical source of truth for the
-- DDL; if SCHEMA.md and this file disagree, this file wins (and SCHEMA.md
-- should be updated).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ----------------------------------------------------------------------
-- Repositories
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS repo (
    repo_id         INTEGER PRIMARY KEY,
    owner           TEXT NOT NULL,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('subject', 'support', 'both')),
    default_branch  TEXT,
    first_seen_at   TIMESTAMP NOT NULL,
    UNIQUE (owner, name)
);

-- ----------------------------------------------------------------------
-- Actors (humans and bots)
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS actor (
    actor_id        INTEGER PRIMARY KEY,
    login           TEXT NOT NULL UNIQUE,
    gh_user_id      INTEGER,
    gh_type         TEXT CHECK (gh_type IN ('User', 'Bot', 'Organization', 'Mannequin')),
    is_bot_login    BOOLEAN NOT NULL,
    first_seen_at   TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_actor_is_bot_login ON actor(is_bot_login);

-- ----------------------------------------------------------------------
-- Commits and per-file changes
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS commit_ (
    commit_id       INTEGER PRIMARY KEY,
    repo_id         INTEGER NOT NULL REFERENCES repo(repo_id),
    sha             TEXT NOT NULL,
    parent_shas     TEXT,
    author_name     TEXT,
    author_email    TEXT,
    author_login    TEXT,
    authored_at     TIMESTAMP NOT NULL,
    committer_name  TEXT,
    committer_email TEXT,
    committed_at    TIMESTAMP NOT NULL,
    message         TEXT NOT NULL,
    UNIQUE (repo_id, sha)
);

CREATE INDEX IF NOT EXISTS idx_commit_authored_at ON commit_(authored_at);
CREATE INDEX IF NOT EXISTS idx_commit_committed_at ON commit_(committed_at);
CREATE INDEX IF NOT EXISTS idx_commit_author_login ON commit_(author_login);

CREATE TABLE IF NOT EXISTS commit_file (
    commit_id       INTEGER NOT NULL REFERENCES commit_(commit_id),
    path            TEXT NOT NULL,
    old_path        TEXT,
    change_type     TEXT NOT NULL CHECK (change_type IN ('A', 'M', 'D', 'R', 'C', 'T')),
    lines_added     INTEGER,
    lines_removed   INTEGER,
    PRIMARY KEY (commit_id, path)
);

CREATE INDEX IF NOT EXISTS idx_commit_file_path ON commit_file(path);

-- ----------------------------------------------------------------------
-- Workflow file states (time series of file content per workflow path)
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workflow_file_state (
    state_id        INTEGER PRIMARY KEY,
    repo_id         INTEGER NOT NULL REFERENCES repo(repo_id),
    path            TEXT NOT NULL,
    commit_id       INTEGER NOT NULL REFERENCES commit_(commit_id),
    content         TEXT,
    content_sha     TEXT,
    exists_after    BOOLEAN NOT NULL,
    UNIQUE (repo_id, path, commit_id)
);

CREATE INDEX IF NOT EXISTS idx_workflow_file_state_repo_path ON workflow_file_state(repo_id, path);

-- ----------------------------------------------------------------------
-- GitHub Actions workflow runs
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS workflow_run (
    run_id              INTEGER PRIMARY KEY,
    repo_id             INTEGER NOT NULL REFERENCES repo(repo_id),
    workflow_path       TEXT NOT NULL,
    workflow_name       TEXT,
    run_number          INTEGER,
    run_attempt         INTEGER,
    event               TEXT,
    status              TEXT,
    conclusion          TEXT,
    head_sha            TEXT,
    head_branch         TEXT,
    actor_id            INTEGER REFERENCES actor(actor_id),
    triggering_actor_id INTEGER REFERENCES actor(actor_id),
    created_at          TIMESTAMP NOT NULL,
    run_started_at      TIMESTAMP,
    updated_at          TIMESTAMP,
    logs_status         TEXT NOT NULL CHECK (logs_status IN
                            ('pending', 'fetched', 'expired', 'unavailable', 'deleted', 'error')),
    logs_path           TEXT,
    artifacts_status    TEXT NOT NULL CHECK (artifacts_status IN
                            ('pending', 'fetched', 'expired', 'unavailable', 'deleted', 'error')),
    artifacts_path      TEXT
);

CREATE INDEX IF NOT EXISTS idx_workflow_run_created_at ON workflow_run(created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_run_repo_workflow ON workflow_run(repo_id, workflow_path);
CREATE INDEX IF NOT EXISTS idx_workflow_run_head_sha ON workflow_run(head_sha);

-- ----------------------------------------------------------------------
-- Issues and pull requests
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS issue (
    issue_id            INTEGER PRIMARY KEY,
    repo_id             INTEGER NOT NULL REFERENCES repo(repo_id),
    number              INTEGER NOT NULL,
    gh_node_id          TEXT,
    title               TEXT NOT NULL,
    body                TEXT,
    author_id           INTEGER REFERENCES actor(actor_id),
    state               TEXT NOT NULL CHECK (state IN ('open', 'closed')),
    state_reason        TEXT,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    closed_at           TIMESTAMP,
    closed_by_id        INTEGER REFERENCES actor(actor_id),
    is_pr               BOOLEAN NOT NULL,
    last_observed_at    TIMESTAMP NOT NULL,
    UNIQUE (repo_id, number)
);

CREATE INDEX IF NOT EXISTS idx_issue_created_at ON issue(created_at);
CREATE INDEX IF NOT EXISTS idx_issue_closed_at ON issue(closed_at);
CREATE INDEX IF NOT EXISTS idx_issue_author ON issue(author_id);
CREATE INDEX IF NOT EXISTS idx_issue_state ON issue(state);
CREATE INDEX IF NOT EXISTS idx_issue_is_pr ON issue(is_pr);

CREATE TABLE IF NOT EXISTS pull_request (
    issue_id            INTEGER PRIMARY KEY REFERENCES issue(issue_id),
    merged              BOOLEAN NOT NULL,
    merged_at           TIMESTAMP,
    merged_by_id        INTEGER REFERENCES actor(actor_id),
    merge_commit_sha    TEXT,
    base_ref            TEXT,
    head_ref            TEXT,
    head_repo_id        INTEGER REFERENCES repo(repo_id),
    draft               BOOLEAN,
    additions           INTEGER,
    deletions           INTEGER,
    changed_files       INTEGER,
    mergeable_state     TEXT
);

CREATE INDEX IF NOT EXISTS idx_pr_merged_at ON pull_request(merged_at);
CREATE INDEX IF NOT EXISTS idx_pr_merged_by ON pull_request(merged_by_id);

CREATE TABLE IF NOT EXISTS pr_file (
    issue_id            INTEGER NOT NULL REFERENCES issue(issue_id),
    path                TEXT NOT NULL,
    status              TEXT,
    additions           INTEGER,
    deletions           INTEGER,
    changes             INTEGER,
    PRIMARY KEY (issue_id, path)
);

CREATE INDEX IF NOT EXISTS idx_pr_file_path ON pr_file(path);

-- ----------------------------------------------------------------------
-- Labels
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS label (
    label_id            INTEGER PRIMARY KEY,
    repo_id             INTEGER NOT NULL REFERENCES repo(repo_id),
    name                TEXT NOT NULL,
    description         TEXT,
    color               TEXT,
    UNIQUE (repo_id, name)
);

CREATE TABLE IF NOT EXISTS issue_label (
    issue_id            INTEGER NOT NULL REFERENCES issue(issue_id),
    label_id            INTEGER NOT NULL REFERENCES label(label_id),
    PRIMARY KEY (issue_id, label_id)
);

-- ----------------------------------------------------------------------
-- Issue / PR timeline events
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS issue_event (
    event_id            INTEGER PRIMARY KEY,
    issue_id            INTEGER NOT NULL REFERENCES issue(issue_id),
    gh_event_id         INTEGER,
    event_type          TEXT NOT NULL,
    actor_id            INTEGER REFERENCES actor(actor_id),
    created_at          TIMESTAMP NOT NULL,
    label_id            INTEGER REFERENCES label(label_id),
    comment_id          INTEGER,
    review_id           INTEGER,
    review_state        TEXT,
    referenced_issue_id INTEGER REFERENCES issue(issue_id),
    old_value           TEXT,
    new_value           TEXT,
    extra_json          TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_issue_created ON issue_event(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_event_type_created ON issue_event(event_type, created_at);
CREATE INDEX IF NOT EXISTS idx_event_actor ON issue_event(actor_id);

-- ----------------------------------------------------------------------
-- Comments
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS comment (
    comment_id          INTEGER PRIMARY KEY,
    issue_id            INTEGER NOT NULL REFERENCES issue(issue_id),
    author_id           INTEGER REFERENCES actor(actor_id),
    body                TEXT NOT NULL,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_comment_issue_created ON comment(issue_id, created_at);
CREATE INDEX IF NOT EXISTS idx_comment_author ON comment(author_id);

-- ----------------------------------------------------------------------
-- Reviews (PR-level formal reviews)
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS review (
    review_id           INTEGER PRIMARY KEY,
    issue_id            INTEGER NOT NULL REFERENCES issue(issue_id),
    author_id           INTEGER REFERENCES actor(actor_id),
    state               TEXT NOT NULL,
    body                TEXT,
    submitted_at        TIMESTAMP,
    commit_sha          TEXT
);

CREATE INDEX IF NOT EXISTS idx_review_issue ON review(issue_id);
CREATE INDEX IF NOT EXISTS idx_review_state ON review(state);

-- ----------------------------------------------------------------------
-- Reactions
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS reaction (
    reaction_id         INTEGER PRIMARY KEY,
    target_kind         TEXT NOT NULL CHECK (target_kind IN ('issue', 'comment', 'review')),
    target_id           INTEGER NOT NULL,
    content             TEXT NOT NULL CHECK (content IN
                            ('+1', '-1', 'laugh', 'hooray', 'confused', 'heart', 'rocket', 'eyes')),
    actor_id            INTEGER REFERENCES actor(actor_id),
    created_at          TIMESTAMP NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_reaction_target ON reaction(target_kind, target_id);

-- ----------------------------------------------------------------------
-- Issue ↔ PR link table (many-to-many; multiple sources may witness a link)
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS linked_pr (
    issue_id            INTEGER NOT NULL REFERENCES issue(issue_id),
    pr_id               INTEGER NOT NULL REFERENCES issue(issue_id),
    link_source         TEXT NOT NULL CHECK (link_source IN
                            ('github_api', 'pr_body_keyword', 'event')),
    PRIMARY KEY (issue_id, pr_id, link_source)
);

CREATE INDEX IF NOT EXISTS idx_linked_pr_pr ON linked_pr(pr_id);

-- ----------------------------------------------------------------------
-- Producer classification (output of layer 2)
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS producer_classification (
    classification_id   INTEGER PRIMARY KEY,
    issue_id            INTEGER NOT NULL REFERENCES issue(issue_id),
    source              TEXT NOT NULL CHECK (source IN
                            ('journal', 'workflow_run', 'marker', 'unknown')),
    producer            TEXT NOT NULL,
    sub_producer        TEXT,
    confidence          REAL,
    basis               TEXT,
    classified_at       TIMESTAMP NOT NULL,
    classifier_version  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_classification_issue ON producer_classification(issue_id);
CREATE INDEX IF NOT EXISTS idx_classification_producer ON producer_classification(producer);
CREATE INDEX IF NOT EXISTS idx_classification_version ON producer_classification(classifier_version);

-- ----------------------------------------------------------------------
-- Extraction bookkeeping
-- ----------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS extraction_state (
    key                 TEXT PRIMARY KEY,
    value               TEXT NOT NULL,
    updated_at          TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS extraction_run (
    run_id              INTEGER PRIMARY KEY,
    started_at          TIMESTAMP NOT NULL,
    ended_at            TIMESTAMP,
    exit_status         TEXT CHECK (exit_status IN ('success', 'partial', 'error')),
    notes               TEXT,
    api_calls_made      INTEGER DEFAULT 0,
    rate_limit_waits    INTEGER DEFAULT 0
);
