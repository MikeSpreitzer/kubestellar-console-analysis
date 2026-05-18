# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Thin wrapper around the ``git`` command-line tool.

The git extractor avoids depending on a Python git library; calling the
CLI directly is robust, dependency-free, and produces stable formats
we can parse.

All functions take a ``repo_path`` (the working tree of a local clone)
and shell out via ``subprocess.run``. They raise on non-zero exit.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Iterable, Iterator, Optional


log = logging.getLogger(__name__)


def run_git(
    repo_path: Path,
    args: list[str],
    *,
    binary: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess:
    """Execute git in the given repo.

    ``binary=True`` returns ``stdout`` as bytes (used for ``git show`` of
    workflow file content, which may not be valid UTF-8 in pathological
    cases). Otherwise ``stdout`` is a UTF-8 string with errors replaced.
    """
    cmd = ["git", "-C", str(repo_path), *args]
    log.debug("run: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=not binary,
        encoding=None if binary else "utf-8",
        errors=None if binary else "replace",
    )


def default_branch(repo_path: Path) -> str:
    """Return the local symbolic ref of the remote's default branch.

    Falls back to ``main`` if the remote head is not configured.
    """
    try:
        result = run_git(
            repo_path, ["symbolic-ref", "refs/remotes/origin/HEAD", "--short"]
        )
        ref = result.stdout.strip()
        # ref looks like "origin/main"
        if "/" in ref:
            return ref.split("/", 1)[1]
        return ref
    except subprocess.CalledProcessError:
        return "main"


def rev_parse(repo_path: Path, ref: str) -> Optional[str]:
    """Return the resolved sha of ``ref``, or None if it doesn't exist."""
    try:
        result = run_git(repo_path, ["rev-parse", "--verify", ref])
        return result.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


# Field separators used in ``git log --format=`` so we can parse robustly.
# Picked to be unlikely to occur in commit metadata.
FIELD_SEP = "\x1eFLD\x1e"
RECORD_SEP = "\x1eREC\x1e"


# ``%H`` sha, ``%P`` parents, ``%an``/``%ae`` author, ``%aI`` ISO 8601 author date,
# ``%cn``/``%ce``/``%cI`` committer, ``%B`` raw message body.
LOG_FORMAT = (
    "%H" + FIELD_SEP +
    "%P" + FIELD_SEP +
    "%an" + FIELD_SEP +
    "%ae" + FIELD_SEP +
    "%aI" + FIELD_SEP +
    "%cn" + FIELD_SEP +
    "%ce" + FIELD_SEP +
    "%cI" + FIELD_SEP +
    "%B" +
    RECORD_SEP
)


def iter_commits(
    repo_path: Path,
    rev_range: str,
    *,
    first_parent_only: bool = False,
) -> Iterator[dict]:
    """Yield commit dicts for ``rev_range`` (oldest first).

    Each yielded dict has keys: ``sha``, ``parent_shas`` (list[str]),
    ``author_name``, ``author_email``, ``authored_at``,
    ``committer_name``, ``committer_email``, ``committed_at``,
    ``message``.

    ``rev_range`` is anything ``git log`` accepts: ``main``,
    ``<old>..HEAD``, etc. Empty range yields nothing.
    """
    args = [
        "log",
        f"--format={LOG_FORMAT}",
        "--reverse",  # oldest first; matches our incremental processing order
        rev_range,
    ]
    if first_parent_only:
        args.insert(1, "--first-parent")
    result = run_git(repo_path, args)
    raw = result.stdout
    if not raw:
        return
    records = raw.split(RECORD_SEP)
    for rec in records:
        rec = rec.strip("\n")
        if not rec:
            continue
        fields = rec.split(FIELD_SEP)
        if len(fields) < 9:
            continue
        (sha, parents, an, ae, ad, cn, ce, cd, msg) = fields[:9]
        yield {
            "sha": sha.strip(),
            "parent_shas": parents.split() if parents.strip() else [],
            "author_name": an,
            "author_email": ae,
            "authored_at": ad,
            "committer_name": cn,
            "committer_email": ce,
            "committed_at": cd,
            "message": msg,
        }


def commit_file_changes(repo_path: Path, sha: str) -> list[dict]:
    """Return per-file changes for one commit.

    Each entry has: ``path``, ``old_path`` (renames only), ``change_type``
    ('A'|'M'|'D'|'R'|'C'|'T'), ``lines_added``, ``lines_removed``.
    Binary files report None for line counts.

    Implementation: combine ``git show --name-status -z`` (for status
    and rename pairing) with ``git show --numstat`` (for line counts).
    Both are run with ``--no-renames=false`` so renames are detected.
    """
    name_status = _name_status(repo_path, sha)
    numstat = _numstat(repo_path, sha)
    out: list[dict] = []
    seen_paths = set()
    for entry in name_status:
        path = entry["path"]
        seen_paths.add(path)
        ns = numstat.get(path, {})
        out.append({
            "path": path,
            "old_path": entry.get("old_path"),
            "change_type": entry["change_type"],
            "lines_added": ns.get("added"),
            "lines_removed": ns.get("removed"),
        })
    # numstat may include paths not in name_status if formats disagree;
    # we ignore those — name_status is authoritative.
    return out


def _name_status(repo_path: Path, sha: str) -> list[dict]:
    """Parse ``git show --name-status -z``."""
    args = [
        "show", "--no-color", "--name-status", "-z", "--no-abbrev",
        "--format=", sha,
    ]
    try:
        result = run_git(repo_path, args)
    except subprocess.CalledProcessError as exc:
        # Empty commits produce no output and exit 0; merges may produce
        # combined output. Defensive fallback: empty list.
        log.warning("name-status failed for %s: %s", sha, exc)
        return []
    return _parse_name_status_z(result.stdout)


def _parse_name_status_z(blob: str) -> list[dict]:
    """Parse the NUL-separated output of ``--name-status -z``.

    Format: each record is "STATUS\\0PATH\\0" or
    "STATUS\\0OLD_PATH\\0NEW_PATH\\0" (renames/copies). The leading
    output starts with possibly a stray newline from ``--format=``;
    we strip it.
    """
    blob = blob.lstrip("\n")
    if not blob:
        return []
    tokens = blob.split("\x00")
    # Drop a trailing empty token from the final separator.
    if tokens and tokens[-1] == "":
        tokens.pop()
    out: list[dict] = []
    i = 0
    while i < len(tokens):
        status = tokens[i]
        if not status:
            i += 1
            continue
        # Status may be e.g. "M", "A", "D", "T", "R100", "C75", or
        # for combined diffs of merges, two-letter codes.
        first = status[0]
        if first in ("R", "C") and len(tokens) > i + 2:
            old_path = tokens[i + 1]
            new_path = tokens[i + 2]
            out.append({
                "path": new_path,
                "old_path": old_path,
                "change_type": first,
            })
            i += 3
        elif first in ("A", "M", "D", "T") and len(tokens) > i + 1:
            path = tokens[i + 1]
            out.append({
                "path": path,
                "old_path": None,
                "change_type": first,
            })
            i += 2
        else:
            # Unknown status (combined merges produce "MM", "AM", etc.);
            # fall back to recording with the first letter.
            if len(tokens) > i + 1:
                path = tokens[i + 1]
                out.append({
                    "path": path,
                    "old_path": None,
                    "change_type": first or "M",
                })
                i += 2
            else:
                i += 1
    return out


def _numstat(repo_path: Path, sha: str) -> dict[str, dict]:
    """Parse ``git show --numstat`` into ``{path: {added, removed}}``.

    Binary files appear as ``- - path``; we record None for those.
    Renames are formatted as ``a\\tb\\t{old => new}`` or
    ``a\\tb\\told\\0new`` depending on flags. We use ``-z`` to make
    rename paths unambiguous.
    """
    args = [
        "show", "--no-color", "--numstat", "-z", "--no-abbrev",
        "--format=", sha,
    ]
    try:
        result = run_git(repo_path, args)
    except subprocess.CalledProcessError as exc:
        log.warning("numstat failed for %s: %s", sha, exc)
        return {}
    return _parse_numstat_z(result.stdout)


def _parse_numstat_z(blob: str) -> dict[str, dict]:
    """Parse ``--numstat -z`` output.

    Per-file format with -z:
        added\\tremoved\\tpath\\0                        (non-rename)
        added\\tremoved\\t\\0old_path\\0new_path\\0       (rename)
    """
    blob = blob.lstrip("\n")
    out: dict[str, dict] = {}
    if not blob:
        return out
    tokens = blob.split("\x00")
    if tokens and tokens[-1] == "":
        tokens.pop()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok:
            i += 1
            continue
        # tok is "added\tremoved\tpath" for non-renames, or
        # "added\tremoved\t" with empty trailing path for renames whose
        # old/new paths follow as the next two tokens.
        parts = tok.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        added_s, removed_s = parts[0], parts[1]
        added = None if added_s == "-" else int(added_s)
        removed = None if removed_s == "-" else int(removed_s)
        path_part = parts[2]
        if path_part == "":
            # Rename: next two tokens are old, new
            if i + 2 < len(tokens):
                new_path = tokens[i + 2]
                out[new_path] = {"added": added, "removed": removed}
                i += 3
                continue
            i += 1
            continue
        else:
            out[path_part] = {"added": added, "removed": removed}
            i += 1
    return out


def show_file_at(
    repo_path: Path,
    sha: str,
    path: str,
) -> Optional[bytes]:
    """Return the file content at ``sha:path``, or None if missing.

    Returns bytes so we can compute a content-sha without lossy decoding.
    The caller decides whether to decode for storage.
    """
    args = ["show", f"{sha}:{path}"]
    try:
        result = run_git(repo_path, args, binary=True, check=False)
    except subprocess.CalledProcessError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def email_to_login(email: str) -> Optional[str]:
    """Heuristic: extract a GitHub login from a noreply email.

    GitHub assigns ``<id>+<login>@users.noreply.github.com`` (or
    ``<login>@users.noreply.github.com`` for older accounts).
    Bot identities follow the same pattern.
    """
    if not email or "@" not in email:
        return None
    local, _, domain = email.partition("@")
    if not domain.endswith("users.noreply.github.com"):
        return None
    if "+" in local:
        _, _, login = local.partition("+")
        return login or None
    return local or None
