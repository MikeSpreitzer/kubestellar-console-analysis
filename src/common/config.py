# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Configuration loading for the console-analysis pipeline.

Reads ``config.yaml`` at the analysis-repo root. The file is gitignored;
``config.yaml.example`` is checked in as a template. The GitHub PAT is
read from the environment variable named in ``github_token_env`` so the
token never lives in a config file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


@dataclass
class RepoConfig:
    owner: str
    name: str
    role: str  # 'subject' | 'support' | 'both'
    local_clone: Optional[Path] = None

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass
class ExtractionConfig:
    log_fetch_concurrency: int = 5
    fetch_logs: bool = False
    fetch_artifacts: bool = False


@dataclass
class AnalysisConfig:
    daily_bin_timezone: str = "UTC"
    flurry_gap_minutes: Optional[int] = None  # None means compute from data


@dataclass
class Config:
    # github_token is None when the environment variable is unset. The
    # extractors must call require_github_token() to fail loudly; the
    # analysis layer does not need it and leaves it None.
    github_token: Optional[str]
    data_dir: Path
    output_dir: Path
    repos: list[RepoConfig] = field(default_factory=list)
    extraction: ExtractionConfig = field(default_factory=ExtractionConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db.sqlite"

    @property
    def gh_runs_dir(self) -> Path:
        return self.data_dir / "gh_runs"

    def repo_by_slug(self, slug: str) -> Optional[RepoConfig]:
        for r in self.repos:
            if r.slug == slug:
                return r
        return None

    def require_github_token(self) -> str:
        """Return the token, raising if unset.

        Called by entry points that actually need to make GitHub API
        calls (the extractors). The analysis layer does not call this.
        """
        if not self.github_token:
            raise RuntimeError(
                "GitHub PAT is required for this operation but the "
                "configured environment variable is unset; export your "
                "PAT before running"
            )
        return self.github_token


def load_config(path: Path | str = "config.yaml") -> Config:
    """Load the config from a YAML file.

    The path is interpreted relative to the current working directory.
    Pipeline entry points should change to the analysis-repo root before
    calling this.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"config file not found at {path}; "
            f"copy config.yaml.example to config.yaml and edit"
        )
    if path.is_dir():
        raise IsADirectoryError(
            f"{path} is a directory, not a file; "
            f"this often means Docker auto-created it as a bind-mount source. "
            f"Remove it (rmdir {path}), copy config.yaml.example to {path}, edit, "
            f"and try again"
        )
    raw = yaml.safe_load(path.read_text())

    token_env = raw.get("github_token_env", "GITHUB_TOKEN")
    token = os.environ.get(token_env) or None

    data_dir = Path(raw.get("data_dir", "./data")).resolve()
    output_dir = Path(raw.get("output_dir", "./output")).resolve()

    repos: list[RepoConfig] = []
    for r in raw.get("repos", []):
        local_clone = r.get("local_clone")
        repos.append(
            RepoConfig(
                owner=r["owner"],
                name=r["name"],
                role=r["role"],
                local_clone=Path(local_clone).resolve() if local_clone else None,
            )
        )

    ex = raw.get("extraction", {})
    extraction = ExtractionConfig(
        log_fetch_concurrency=ex.get("log_fetch_concurrency", 5),
        fetch_logs=ex.get("fetch_logs", False),
        fetch_artifacts=ex.get("fetch_artifacts", False),
    )

    an = raw.get("analysis", {})
    analysis = AnalysisConfig(
        daily_bin_timezone=an.get("daily_bin_timezone", "UTC"),
        flurry_gap_minutes=an.get("flurry_gap_minutes"),
    )

    return Config(
        github_token=token,
        data_dir=data_dir,
        output_dir=output_dir,
        repos=repos,
        extraction=extraction,
        analysis=analysis,
    )
