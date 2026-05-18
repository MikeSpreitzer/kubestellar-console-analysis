# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Producer-classification rules.

A ``Rule`` is a predicate-plus-verdict. The classifier evaluates rules
in declaration order, first match wins, and writes the matched rule's
verdict to the database. Rules are pure functions over a Record; they
should not have side effects, depend on global state, or do I/O.

The producer taxonomy values used here are:

- ``human-credentialed`` -- the artifact's actor identity is not
  detectably an automation. Upper bound on actual human work; humans
  may run automation under their own credentials, in which case the
  artifact is misclassified as human.
- ``copilot`` -- GitHub Copilot dispatched as automation. Includes
  related logins like ``copilot-pull-request-reviewer[bot]`` and
  ``copilot-swe-agent[bot]``; the specific login is preserved in
  ``sub_producer``.
- ``claude-app`` -- the ``claude[bot]`` GitHub App identity.
- ``prow`` -- the ``kubestellar-prow[bot]`` actor (labeler,
  APPROVALNOTIFIER, label events).
- ``netlify`` -- the ``netlify[bot]`` actor (deploy-preview comments).
- ``dependabot`` -- the ``dependabot[bot]`` actor.
- ``hive-scanner`` -- hive's scanner sub-agent.
- ``hive-reviewer`` -- the Claude-driven reviewer that committed under
  ``reviewer@claude-dev.local``. (We do not have a dedicated GitHub
  app login for this; the email is the only signal.)
- ``hive-merger`` -- the kubestellar-hive[bot] App when seen merging.
- ``other-bot-app`` -- any GitHub App login ending in ``[bot]`` that
  isn't more specifically classified above. Currently dominated by
  ``github-actions[bot]``, which is the runner identity for any
  workflow that posts/comments/files things; splitting that further
  requires content-based signals (workflow name, comment body
  parsing) that the classifier does not yet consume.
- ``project-bot`` -- one of the kubestellar-org bot identities seen in
  Co-Authored-By trailers or in non-noreply emails (e.g.
  ``ks-ci-bot``, ``kubestellar-bot``, ``auto-qa``,
  ``kubestellar-console-bot``).
- ``unknown`` -- the artifact has no actor identity at all.

Sub-producers carry finer detail when the rule resolves it (e.g. the
specific bot login or email).

When the rule list changes, bump ``CLASSIFIER_VERSION`` in
``main.py``. The classifier's startup logic refuses to overwrite rows
of one version with verdicts derived from a different rule list, as a
guard against developers forgetting the bump.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

from .record import Record


# ----------------------------------------------------------------------
# Producer label constants
# ----------------------------------------------------------------------

PRODUCER_HUMAN = "human-credentialed"
PRODUCER_UNKNOWN = "unknown"
PRODUCER_COPILOT = "copilot"
PRODUCER_CLAUDE_APP = "claude-app"
PRODUCER_PROW = "prow"
PRODUCER_NETLIFY = "netlify"
PRODUCER_DEPENDABOT = "dependabot"
PRODUCER_HIVE_SCANNER = "hive-scanner"
PRODUCER_HIVE_REVIEWER = "hive-reviewer"
PRODUCER_HIVE_MERGER = "hive-merger"
PRODUCER_OTHER_BOT_APP = "other-bot-app"
PRODUCER_PROJECT_BOT = "project-bot"


# ----------------------------------------------------------------------
# Bot-author email allowlist
#
# Maintained additively. When the classifier output reveals a producer
# we haven't named (or the trailer-enumeration CSV in
# commit_authorship reveals an automation identity not yet matched
# here), add an entry.
# ----------------------------------------------------------------------

# Specific known automation emails -> producer label
EMAIL_TO_PRODUCER: dict[str, str] = {
    "copilot@github.com":            PRODUCER_COPILOT,
    "scanner@kubestellar.io":        PRODUCER_HIVE_SCANNER,
    "reviewer@claude-dev.local":     PRODUCER_HIVE_REVIEWER,
    "kubestellar-bot@kubestellar.io":           PRODUCER_PROJECT_BOT,
    "kubestellar-bot@users.noreply.github.com": PRODUCER_PROJECT_BOT,
    "hive-bot@kubestellar.io":                  PRODUCER_HIVE_SCANNER,
    "kubestellar-hive@users.noreply.github.com": PRODUCER_HIVE_SCANNER,
    "ks-ci-bot@users.noreply.github.com":       PRODUCER_PROJECT_BOT,
    "auto-qa@example.com":                      PRODUCER_PROJECT_BOT,
    "tester-agent@kubestellar.io":              PRODUCER_PROJECT_BOT,
    "ai@example.com":                           PRODUCER_PROJECT_BOT,
    "bob@example.com":                          PRODUCER_PROJECT_BOT,
}


# Specific known bot logins -> producer label. Logins ending in
# ``[bot]`` that don't appear here fall through to PRODUCER_OTHER_BOT_APP.
LOGIN_TO_PRODUCER: dict[str, str] = {
    "copilot[bot]":                       PRODUCER_COPILOT,
    "copilot-pull-request-reviewer[bot]": PRODUCER_COPILOT,
    "copilot-swe-agent[bot]":             PRODUCER_COPILOT,
    "claude[bot]":                        PRODUCER_CLAUDE_APP,
    "kubestellar-hive[bot]":              PRODUCER_HIVE_MERGER,
    "kubestellar-prow[bot]":              PRODUCER_PROW,
    "netlify[bot]":                       PRODUCER_NETLIFY,
    "dependabot[bot]":                    PRODUCER_DEPENDABOT,
    "kubestellar-console-bot[bot]":       PRODUCER_PROJECT_BOT,
}


# ----------------------------------------------------------------------
# Rule infrastructure
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    producer: str
    sub_producer: Optional[str]
    basis: str


# A Rule's predicate inspects a Record. A True return means the rule
# matches and its verdict_fn is invoked to produce the verdict. The
# verdict_fn receives the same Record so it can extract sub_producer
# detail from the matched data.
@dataclass(frozen=True)
class Rule:
    name: str
    when: Callable[[Record], bool]
    verdict_fn: Callable[[Record], Verdict]


# ----------------------------------------------------------------------
# Helper predicates
# ----------------------------------------------------------------------

def _login_str(r: Record) -> str:
    return r.author_login if isinstance(r.author_login, str) else ""


def _email_str(r: Record) -> str:
    return r.author_email if isinstance(r.author_email, str) else ""


# ----------------------------------------------------------------------
# Rule definitions
#
# Order matters. The classifier evaluates in this order; first match
# wins. Specific rules come before catch-alls.
# ----------------------------------------------------------------------

RULES: list[Rule] = [
    # 1. Specific known logins (more precise than the generic "[bot]"
    #    suffix rule below).
    Rule(
        name="known-login",
        when=lambda r: _login_str(r) in LOGIN_TO_PRODUCER,
        verdict_fn=lambda r: Verdict(
            producer=LOGIN_TO_PRODUCER[_login_str(r)],
            sub_producer=_login_str(r),
            basis=f"login={_login_str(r)} (known)",
        ),
    ),

    # 2. Any GitHub-app bot login ending in [bot] -- catch-all for
    #    bots whose specific identity we haven't classified yet.
    Rule(
        name="other-bot-app",
        when=lambda r: _login_str(r).endswith("[bot]"),
        verdict_fn=lambda r: Verdict(
            producer=PRODUCER_OTHER_BOT_APP,
            sub_producer=_login_str(r),
            basis=f"login={_login_str(r)} ([bot] suffix)",
        ),
    ),

    # 3. Specific known automation emails.
    Rule(
        name="known-email",
        when=lambda r: _email_str(r) in EMAIL_TO_PRODUCER,
        verdict_fn=lambda r: Verdict(
            producer=EMAIL_TO_PRODUCER[_email_str(r)],
            sub_producer=_email_str(r),
            basis=f"email={_email_str(r)} (known)",
        ),
    ),

    # 4. Default: human-credentialed if we have any actor identity at
    #    all. This is the upper-bound caveat -- humans may be running
    #    automation under their own credentials, but we cannot detect
    #    that from the email or login alone.
    Rule(
        name="default-human",
        when=lambda r: bool(_login_str(r) or _email_str(r)),
        verdict_fn=lambda r: Verdict(
            producer=PRODUCER_HUMAN,
            sub_producer=_login_str(r) or _email_str(r),
            basis="no automation marker matched",
        ),
    ),

    # 5. Final fallback: no actor identity at all.
    Rule(
        name="unknown",
        when=lambda r: True,
        verdict_fn=lambda r: Verdict(
            producer=PRODUCER_UNKNOWN,
            sub_producer=None,
            basis="no author_login or author_email available",
        ),
    ),
]


# ----------------------------------------------------------------------
# Coarse credential class
#
# Some analyses care only about a binary "bot vs. human" split, not
# the full producer taxonomy. ``credential_class_of(producer)`` maps
# a producer label to one of three coarse classes for those analyses.
# ----------------------------------------------------------------------

CREDENTIAL_HUMAN = "human-credentialed"
CREDENTIAL_BOT = "bot-credentialed"
CREDENTIAL_UNKNOWN = "unknown"


def credential_class_of(producer: str) -> str:
    """Map a producer label to its coarse credential class."""
    if producer == PRODUCER_HUMAN:
        return CREDENTIAL_HUMAN
    if producer == PRODUCER_UNKNOWN:
        return CREDENTIAL_UNKNOWN
    # Every other producer is some flavor of automation.
    return CREDENTIAL_BOT


def classify(record: Record) -> tuple[Verdict, str]:
    """Apply rules in order, return the first match's verdict and the
    matching rule's name.

    Always returns a verdict; the final ``unknown`` rule is the
    guaranteed match.
    """
    for rule in RULES:
        if rule.when(record):
            return rule.verdict_fn(record), rule.name
    # The final rule's predicate is ``True``, so this is unreachable.
    raise AssertionError("no rule matched (final unknown rule should always match)")


# ----------------------------------------------------------------------
# Rule integrity check
#
# A hash over the rule definitions so the classifier orchestrator can
# detect "rules changed but classifier_version did not." See
# main.CLASSIFIER_VERSION.
# ----------------------------------------------------------------------

def rules_signature() -> str:
    """A stable hash of the rule list and the lookup tables it
    references. Changes if rules change.

    Note: hashes the structure and known-identity sets, not the
    predicate callables (which are unhashable). Predicate code changes
    that don't touch these structures won't move the hash; bump
    CLASSIFIER_VERSION manually for those.
    """
    import hashlib
    h = hashlib.sha256()
    for rule in RULES:
        h.update(rule.name.encode("utf-8"))
        h.update(b"\x00")
    for k in sorted(EMAIL_TO_PRODUCER):
        h.update(f"{k}={EMAIL_TO_PRODUCER[k]}".encode("utf-8"))
        h.update(b"\x00")
    for k in sorted(LOGIN_TO_PRODUCER):
        h.update(f"{k}={LOGIN_TO_PRODUCER[k]}".encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]
