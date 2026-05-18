# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Smoke tests: imports succeed and the schema applies cleanly to an
in-memory sqlite database."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def test_imports():
    from src.common import config, db, github_client, registries  # noqa: F401
    from src.extractor_github import (  # noqa: F401
        comments, issues, labels, pr_files, reactions, reviews, runs, timelines,
    )
    from src.extractor_git import git_cli, walker  # noqa: F401
    from src.analysis import first_look, drilldown, commit_authorship  # noqa: F401
    from src.analysis import _plotly_html  # noqa: F401
    from src.classifier import adapters, main, record, rules  # noqa: F401


def test_schema_applies():
    from src.common.db import init_schema
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    expected = {
        "repo", "actor", "commit_", "commit_file", "workflow_file_state",
        "workflow_run", "issue", "pull_request", "pr_file", "label",
        "issue_label", "issue_event", "comment", "review", "reaction",
        "linked_pr", "producer_classification", "extraction_state",
        "extraction_run",
    }
    missing = expected - tables
    assert not missing, f"missing tables: {missing}"


def test_registries_basic_actor():
    from src.common.db import init_schema
    from src.common.registries import upsert_actor, upsert_actor_from_api

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)

    a = upsert_actor(conn, login="dependabot[bot]", gh_user_id=49699333, gh_type="Bot")
    b = upsert_actor(conn, login="dependabot[bot]")
    assert a == b, "second upsert should return the same actor_id"

    row = conn.execute("SELECT is_bot_login FROM actor WHERE actor_id = ?", (a,)).fetchone()
    assert row["is_bot_login"] == 1

    none = upsert_actor_from_api(conn, None)
    assert none is None


def test_registries_repo_role_merge():
    from src.common.db import init_schema
    from src.common.registries import upsert_repo

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    init_schema(conn)

    r = upsert_repo(conn, owner="kubestellar", name="infra", role="support")
    upsert_repo(conn, owner="kubestellar", name="infra", role="subject")
    role = conn.execute("SELECT role FROM repo WHERE repo_id = ?", (r,)).fetchone()["role"]
    assert role == "both"


def test_git_cli_parses_email_to_login():
    from src.extractor_git.git_cli import email_to_login

    assert email_to_login("123456+alice@users.noreply.github.com") == "alice"
    assert email_to_login("alice@users.noreply.github.com") == "alice"
    assert (
        email_to_login("49699333+dependabot[bot]@users.noreply.github.com")
        == "dependabot[bot]"
    )
    assert email_to_login("alice@example.com") is None
    assert email_to_login("") is None


def test_git_cli_parses_name_status_z():
    from src.extractor_git.git_cli import _parse_name_status_z

    # Mix of statuses including a rename.
    blob = "\nM\x00path/a.txt\x00A\x00path/b.txt\x00D\x00path/c.txt\x00R100\x00old.txt\x00new.txt\x00"
    out = _parse_name_status_z(blob)
    assert out == [
        {"path": "path/a.txt", "old_path": None, "change_type": "M"},
        {"path": "path/b.txt", "old_path": None, "change_type": "A"},
        {"path": "path/c.txt", "old_path": None, "change_type": "D"},
        {"path": "new.txt", "old_path": "old.txt", "change_type": "R"},
    ]


def test_git_cli_parses_numstat_z():
    from src.extractor_git.git_cli import _parse_numstat_z

    # Non-rename, then a rename, then a binary file.
    blob = "5\t3\tpath/a.txt\x000\t0\t\x00old.txt\x00new.txt\x00-\t-\timage.png\x00"
    out = _parse_numstat_z(blob)
    assert out == {
        "path/a.txt": {"added": 5, "removed": 3},
        "new.txt": {"added": 0, "removed": 0},
        "image.png": {"added": None, "removed": None},
    }


def test_integrity_check_full_passes_on_clean_db():
    import tempfile
    from src.common.db import (
        IntegrityError, connect, init_schema, integrity_check_full,
    )

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "db.sqlite"
        conn = connect(db_path)
        init_schema(conn)
        # No exception should be raised.
        integrity_check_full(conn)
        conn.close()


def test_vacuum_into_creates_clean_copy():
    import tempfile
    from src.common.db import (
        connect, init_schema, integrity_check_full, vacuum_into,
    )

    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        src_path = td_path / "db.sqlite"
        snap_path = td_path / "snap.sqlite"

        conn = connect(src_path)
        init_schema(conn)
        conn.execute(
            "INSERT INTO repo (owner, name, role, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            ("kubestellar", "console", "subject", "2026-01-15T12:00:00Z"),
        )
        vacuum_into(conn, snap_path)
        conn.close()

        # The snapshot should be a valid sqlite database with the row we wrote.
        snap_conn = connect(snap_path)
        integrity_check_full(snap_conn)
        row = snap_conn.execute(
            "SELECT owner, name FROM repo"
        ).fetchone()
        assert row["owner"] == "kubestellar"
        assert row["name"] == "console"
        snap_conn.close()


def test_hourly_checker_first_call_does_not_check():
    """First call to maybe_check should be a no-op (no hour elapsed yet)."""
    import tempfile
    from src.common.db import HourlyChecker, connect, init_schema

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "db.sqlite"
        conn = connect(db_path)
        init_schema(conn)
        hc = HourlyChecker(interval_seconds=3600.0)
        assert hc.maybe_check(conn) is False
        conn.close()


def test_hourly_checker_zero_interval_always_checks():
    """With interval_seconds=0, every call should run the check."""
    import tempfile
    import time
    from src.common.db import HourlyChecker, connect, init_schema

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "db.sqlite"
        conn = connect(db_path)
        init_schema(conn)
        hc = HourlyChecker(interval_seconds=0.0)
        # Sleep a tiny amount so monotonic() advances.
        time.sleep(0.01)
        assert hc.maybe_check(conn) is True
        conn.close()


def test_connect_readonly_refuses_writes():
    """connect_readonly should open without error and reject writes."""
    import tempfile
    from src.common.db import connect, connect_readonly, init_schema

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "db.sqlite"
        # Create database with the writable connect, then close.
        wconn = connect(db_path)
        init_schema(wconn)
        wconn.execute(
            "INSERT INTO repo (owner, name, role, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            ("kubestellar", "console", "subject", "2026-01-15T12:00:00Z"),
        )
        wconn.close()

        # Open read-only and verify reads work.
        rconn = connect_readonly(db_path)
        rows = rconn.execute("SELECT owner, name FROM repo").fetchall()
        assert len(rows) == 1
        assert rows[0]["owner"] == "kubestellar"

        # Verify a write attempt raises.
        try:
            rconn.execute(
                "INSERT INTO repo (owner, name, role, first_seen_at) "
                "VALUES (?, ?, ?, ?)",
                ("foo", "bar", "subject", "2026-01-15T12:00:00Z"),
            )
            rconn.commit()
            rconn.close()
            raise AssertionError("expected read-only connection to reject INSERT")
        except sqlite3.OperationalError:
            pass
        rconn.close()


def test_first_look_classifies_login():
    from src.analysis.first_look import (
        _classify_login, CREDENTIAL_BOT, CREDENTIAL_HUMAN, CREDENTIAL_UNKNOWN,
    )
    assert _classify_login("dependabot[bot]") == CREDENTIAL_BOT
    assert _classify_login("kubestellar-hive[bot]") == CREDENTIAL_BOT
    assert _classify_login("clubanderson") == CREDENTIAL_HUMAN
    assert _classify_login("MikeSpreitzer") == CREDENTIAL_HUMAN
    assert _classify_login(None) == CREDENTIAL_UNKNOWN


def test_commit_authorship_trailer_regex():
    from src.analysis.commit_authorship import _COAUTHOR_RE

    msg = (
        "Fix the foo handler\n"
        "\n"
        "Body of the commit.\n"
        "\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        "Co-authored-by: Alice <alice@example.com>\n"
    )
    matches = list(_COAUTHOR_RE.finditer(msg))
    assert len(matches) == 2
    assert matches[0].group("name") == "Claude"
    assert matches[0].group("email") == "noreply@anthropic.com"
    assert matches[1].group("name") == "Alice"
    assert matches[1].group("email") == "alice@example.com"

    # No trailer at all
    matches = list(_COAUTHOR_RE.finditer("plain message\n"))
    assert len(matches) == 0


def test_commit_authorship_ai_tool_detection():
    from src.analysis.commit_authorship import _trailer_names_ai_tool

    # AI tools: should match
    assert _trailer_names_ai_tool("Claude", "noreply@anthropic.com") is True
    assert _trailer_names_ai_tool("GitHub Copilot", "copilot@github.com") is True
    assert _trailer_names_ai_tool("Cursor", "noreply@cursor.so") is True
    assert _trailer_names_ai_tool("dependabot[bot]", "x@y") is True
    # Bare-bot/agent/assistant variants observed in real data
    assert _trailer_names_ai_tool("Scanner Bot", "scanner@kubestellar.io") is True
    assert _trailer_names_ai_tool("ks-ci-bot", "ks-ci-bot@users.noreply.github.com") is True
    assert _trailer_names_ai_tool("Auto-QA Bot", "auto-qa@example.com") is True
    assert _trailer_names_ai_tool("kubestellar-bot", "kubestellar-bot@kubestellar.io") is True
    assert _trailer_names_ai_tool("kubestellar-hive", "hive-bot@kubestellar.io") is True
    assert _trailer_names_ai_tool("tester-agent", "tester-agent@kubestellar.io") is True
    assert _trailer_names_ai_tool("AI Assistant", "ai@example.com") is True
    assert _trailer_names_ai_tool("GitHub Actions", "noreply@github.com") is True
    # Bob is a known automation handle in this project (matched by
    # the bob@example.com email rule, not the bare name).
    assert _trailer_names_ai_tool("Bob", "bob@example.com") is True
    # Humans: should not
    assert _trailer_names_ai_tool("Mike Spreitzer", "mspreitz@us.ibm.com") is False
    assert _trailer_names_ai_tool("Andy Anderson", "andy@clubanderson.com") is False
    # Bob's name without the matching email should NOT match (we don't
    # want every other "Bob" to be flagged as automation).
    assert _trailer_names_ai_tool("Bob Smith", "bsmith@somewhere.com") is False
    # Should not false-positive on words that contain bot/agent as
    # substrings without word boundaries
    assert _trailer_names_ai_tool("Robotics Inc", "robot@example.com") is False
    assert _trailer_names_ai_tool("Agenda Maker", "agenda@example.com") is False
    # Empty
    assert _trailer_names_ai_tool("", "") is False


def test_classifier_rules_cover_known_cases():
    """The shared classifier rules should produce expected verdicts
    for the populations we've already observed in the data."""
    from src.classifier.record import Record
    from src.classifier.rules import (
        CREDENTIAL_BOT, CREDENTIAL_HUMAN, CREDENTIAL_UNKNOWN,
        PRODUCER_HIVE_MERGER, PRODUCER_HIVE_SCANNER, PRODUCER_COPILOT,
        PRODUCER_HUMAN, PRODUCER_OTHER_BOT_APP, PRODUCER_PROJECT_BOT,
        PRODUCER_UNKNOWN,
        classify, credential_class_of,
    )

    def _classify(login=None, email=None):
        v, _ = classify(Record(
            target_kind="commit", target_id=0,
            author_login=login, author_email=email, author_name=None,
            created_at="",
        ))
        return v.producer

    from src.classifier.rules import (
        PRODUCER_CLAUDE_APP, PRODUCER_DEPENDABOT, PRODUCER_NETLIFY,
        PRODUCER_PROW,
    )
    # Known logins
    assert _classify(login="kubestellar-hive[bot]") == PRODUCER_HIVE_MERGER
    assert _classify(login="copilot[bot]") == PRODUCER_COPILOT
    assert _classify(login="copilot-swe-agent[bot]") == PRODUCER_COPILOT
    assert _classify(login="copilot-pull-request-reviewer[bot]") == PRODUCER_COPILOT
    assert _classify(login="claude[bot]") == PRODUCER_CLAUDE_APP
    assert _classify(login="kubestellar-prow[bot]") == PRODUCER_PROW
    assert _classify(login="netlify[bot]") == PRODUCER_NETLIFY
    assert _classify(login="dependabot[bot]") == PRODUCER_DEPENDABOT
    assert _classify(login="kubestellar-console-bot[bot]") == PRODUCER_PROJECT_BOT
    # Generic [bot] login -- the catch-all
    assert _classify(login="github-actions[bot]") == PRODUCER_OTHER_BOT_APP
    assert _classify(login="some-unknown[bot]") == PRODUCER_OTHER_BOT_APP
    # Known emails
    assert _classify(email="scanner@kubestellar.io") == PRODUCER_HIVE_SCANNER
    assert _classify(email="copilot@github.com") == PRODUCER_COPILOT
    assert _classify(email="ks-ci-bot@users.noreply.github.com") == PRODUCER_PROJECT_BOT
    # Human
    assert _classify(login="clubanderson",
                     email="andy@clubanderson.com") == PRODUCER_HUMAN
    assert _classify(email="mspreitz@us.ibm.com") == PRODUCER_HUMAN
    # Unknown
    assert _classify() == PRODUCER_UNKNOWN

    # Coarse credential classes
    assert credential_class_of(PRODUCER_HUMAN) == CREDENTIAL_HUMAN
    assert credential_class_of(PRODUCER_UNKNOWN) == CREDENTIAL_UNKNOWN
    assert credential_class_of(PRODUCER_HIVE_SCANNER) == CREDENTIAL_BOT
    assert credential_class_of(PRODUCER_OTHER_BOT_APP) == CREDENTIAL_BOT


def test_commit_authorship_classifies_commit():
    import math
    from src.analysis.commit_authorship import (
        _classify_commit, CREDENTIAL_BOT, CREDENTIAL_HUMAN, CREDENTIAL_UNKNOWN,
    )
    # Login-based bot
    assert _classify_commit("dependabot[bot]", None) == CREDENTIAL_BOT
    assert _classify_commit("kubestellar-hive[bot]", "any@example.com") == CREDENTIAL_BOT
    # Email-based bot (no login)
    assert _classify_commit(None, "scanner@kubestellar.io") == CREDENTIAL_BOT
    assert _classify_commit(None, "copilot@github.com") == CREDENTIAL_BOT
    assert _classify_commit(None, "reviewer@claude-dev.local") == CREDENTIAL_BOT
    # Human (real email)
    assert _classify_commit(None, "andy@clubanderson.com") == CREDENTIAL_HUMAN
    assert _classify_commit("clubanderson", "andy@clubanderson.com") == CREDENTIAL_HUMAN
    # Unknown (no signal)
    assert _classify_commit(None, None) == CREDENTIAL_UNKNOWN
    assert _classify_commit(None, "") == CREDENTIAL_UNKNOWN
    # Pandas NaN (a float) must be tolerated -- regression test for
    # the AttributeError seen on first real run.
    nan = float("nan")
    assert _classify_commit(nan, nan) == CREDENTIAL_UNKNOWN
    assert _classify_commit(nan, "andy@clubanderson.com") == CREDENTIAL_HUMAN
    assert _classify_commit("dependabot[bot]", nan) == CREDENTIAL_BOT


def test_plotly_html_writes_tab_title():
    """write_html_with_title should produce an HTML file containing a
    <title>...</title> with the given text."""
    import tempfile
    import plotly.graph_objects as go
    from src.analysis._plotly_html import write_html_with_title

    fig = go.Figure(data=[go.Scatter(x=[1, 2, 3], y=[4, 5, 6])])
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "x.html"
        write_html_with_title(fig, out, "My Tab Title")
        text = out.read_text()
        assert "<title>My Tab Title</title>" in text
        # Special characters should be HTML-escaped.
        out2 = Path(td) / "y.html"
        write_html_with_title(fig, out2, "Issues opened (foo & bar)")
        text2 = out2.read_text()
        assert "<title>Issues opened (foo &amp; bar)</title>" in text2


def test_commit_authorship_daily_counts_arbitrary_values():
    """Regression: _daily_counts must work for classification columns
    whose values are not the credential constants (e.g. the disclosure
    constants used by disclosed_ai_collaboration).
    """
    import pandas as pd
    from src.analysis.commit_authorship import _daily_counts

    df = pd.DataFrame([
        {"ts": "2026-01-01T00:00:00Z", "kind": "alpha"},
        {"ts": "2026-01-01T00:00:00Z", "kind": "beta"},
        {"ts": "2026-01-02T00:00:00Z", "kind": "alpha"},
    ])
    # No column_order: keep all groupby columns.
    daily = _daily_counts(df, "ts", "kind")
    assert "alpha" in daily.columns
    assert "beta" in daily.columns
    assert daily.loc[daily.index[0], "alpha"] == 1

    # With column_order: respect it, drop unspecified columns.
    daily = _daily_counts(df, "ts", "kind", column_order=["beta", "alpha"])
    assert list(daily.columns) == ["beta", "alpha"]


def test_first_look_daily_counts():
    """Verify daily binning groups by UTC day and pivots correctly."""
    import pandas as pd
    from src.analysis.first_look import _daily_counts

    df = pd.DataFrame([
        {"created_at": "2026-01-01T05:00:00Z", "credential": "human-credentialed"},
        {"created_at": "2026-01-01T18:00:00Z", "credential": "human-credentialed"},
        {"created_at": "2026-01-01T20:00:00Z", "credential": "bot-credentialed"},
        {"created_at": "2026-01-02T03:00:00Z", "credential": "human-credentialed"},
    ])
    daily = _daily_counts(df, "created_at", "credential")
    assert len(daily) == 2
    # Day 2026-01-01 should have 2 human + 1 bot.
    day1 = daily.iloc[0]
    assert day1["human-credentialed"] == 2
    assert day1["bot-credentialed"] == 1


def test_iso8601_timestamps_roundtrip(tmp_path=None):
    """Regression: ISO 8601 timestamps with 'T' and 'Z' must round-trip
    through the database without sqlite3 attempting to parse them as
    Python datetimes (which would crash on the 'T' separator).
    """
    import tempfile
    from src.common.db import connect, init_schema

    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "db.sqlite"
        conn = connect(db_path)
        init_schema(conn)

        # Insert a repo and an issue with a typical GitHub timestamp.
        conn.execute(
            "INSERT INTO repo (owner, name, role, first_seen_at) "
            "VALUES (?, ?, ?, ?)",
            ("kubestellar", "console", "subject", "2026-01-15T12:00:00Z"),
        )
        repo_id = conn.execute(
            "SELECT repo_id FROM repo WHERE owner='kubestellar' AND name='console'"
        ).fetchone()["repo_id"]

        conn.execute(
            """
            INSERT INTO issue (
                repo_id, number, title, state, created_at, updated_at,
                is_pr, last_observed_at
            )
            VALUES (?, 1, 't', 'open', ?, ?, 1, ?)
            """,
            (
                repo_id,
                "2026-05-15T08:50:57Z",
                "2026-05-15T08:50:57Z",
                "2026-05-15T08:50:58Z",
            ),
        )
        conn.execute(
            "INSERT INTO pull_request (issue_id, merged) "
            "SELECT issue_id, 0 FROM issue WHERE repo_id = ?",
            (repo_id,),
        )

        # The query that crashed in production: a join with a timestamp
        # comparison and fetchall.
        rows = conn.execute(
            """
            SELECT i.issue_id, i.updated_at
            FROM issue i
            LEFT JOIN pull_request pr ON pr.issue_id = i.issue_id
            WHERE i.repo_id = ?
              AND i.is_pr = 1
              AND (pr.issue_id IS NULL OR i.updated_at > i.last_observed_at)
            """,
            (repo_id,),
        ).fetchall()
        # The row's updated_at < last_observed_at, so the WHERE clause
        # should exclude it. What matters is that fetchall() did not
        # raise on the timestamp values.
        assert isinstance(rows, list)

        # Spot-check that timestamps come back as plain strings.
        ts = conn.execute(
            "SELECT created_at FROM issue WHERE repo_id = ?", (repo_id,)
        ).fetchone()["created_at"]
        assert isinstance(ts, str)
        assert ts == "2026-05-15T08:50:57Z"

        conn.close()


if __name__ == "__main__":
    # Run without pytest for ease of in-container smoke check.
    test_imports()
    test_schema_applies()
    test_registries_basic_actor()
    test_registries_repo_role_merge()
    test_git_cli_parses_email_to_login()
    test_git_cli_parses_name_status_z()
    test_git_cli_parses_numstat_z()
    test_integrity_check_full_passes_on_clean_db()
    test_vacuum_into_creates_clean_copy()
    test_hourly_checker_first_call_does_not_check()
    test_hourly_checker_zero_interval_always_checks()
    test_connect_readonly_refuses_writes()
    test_first_look_classifies_login()
    test_classifier_rules_cover_known_cases()
    test_commit_authorship_classifies_commit()
    test_commit_authorship_trailer_regex()
    test_commit_authorship_ai_tool_detection()
    test_commit_authorship_daily_counts_arbitrary_values()
    test_plotly_html_writes_tab_title()
    test_first_look_daily_counts()
    test_iso8601_timestamps_roundtrip()
    print("smoke tests passed")
