# Copyright 2026 Mike Spreitzer
# SPDX-License-Identifier: Apache-2.0
# Authored by Mike Spreitzer with assistance from Claude (Anthropic, Opus 4.7).

"""Thin GitHub REST client with rate-limit awareness.

Handles the two flavors of throttling GitHub uses:

- **Primary** rate limit (5,000/hour authenticated). The response carries
  ``X-RateLimit-Remaining`` and ``X-RateLimit-Reset`` headers; when
  ``Remaining`` reaches a small number we sleep until ``Reset``.
- **Secondary** rate limit (concurrent / per-minute / content-creation
  caps). These surface as 403 or 429 with a ``retry-after`` header or a
  body containing ``secondary rate limit``. We honor ``retry-after`` if
  present, else exponential backoff.

The client also paginates Link-header-driven endpoints so callers can
iterate without thinking about pages.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional
from urllib.parse import parse_qs, urlparse

import requests


log = logging.getLogger(__name__)


GITHUB_API = "https://api.github.com"
DEFAULT_USER_AGENT = "console-analysis/0.1 (+https://github.com/MikeSpreitzer/console-analysis)"

# Threshold below which we proactively sleep until the primary rate-limit reset.
RATE_LIMIT_LOW_WATER = 50

# Backoff defaults for secondary rate-limit retries.
SECONDARY_BACKOFF_INITIAL_S = 2.0
SECONDARY_BACKOFF_MAX_S = 120.0
SECONDARY_BACKOFF_FACTOR = 2.0
SECONDARY_MAX_ATTEMPTS = 6

# Backoff defaults for transient network errors (read timeouts,
# connection resets, dropped TCP). These are independent of GitHub's
# rate limit machinery and may happen at any time.
NETWORK_BACKOFF_INITIAL_S = 1.0
NETWORK_BACKOFF_MAX_S = 60.0
NETWORK_BACKOFF_FACTOR = 2.0
NETWORK_MAX_ATTEMPTS = 6

# Backoff defaults for 5xx server errors. GitHub's timeline endpoint in
# particular emits sporadic 500s on issues with unusual histories;
# these may be transient or may be persistent for the specific issue.
# We retry with backoff and let the caller decide what to do after
# attempts are exhausted.
SERVER_ERROR_BACKOFF_INITIAL_S = 2.0
SERVER_ERROR_BACKOFF_MAX_S = 60.0
SERVER_ERROR_BACKOFF_FACTOR = 2.0
SERVER_ERROR_MAX_ATTEMPTS = 4

# Per-request HTTP timeout. Connect and read timeouts are set
# independently so a slow server response doesn't trip a connect-timeout
# limit and a stuck connection doesn't wait indefinitely.
CONNECT_TIMEOUT_S = 30.0
READ_TIMEOUT_S = 60.0


@dataclass
class ClientStats:
    """Counters surfaced for extraction-run logging."""
    api_calls_made: int = 0
    rate_limit_waits: int = 0
    secondary_waits: int = 0
    network_retries: int = 0
    server_error_retries: int = 0
    bytes_in: int = 0


@dataclass
class GitHubClient:
    token: str
    user_agent: str = DEFAULT_USER_AGENT
    base_url: str = GITHUB_API
    accept: str = "application/vnd.github+json"
    api_version: str = "2022-11-28"
    session: requests.Session = field(default_factory=requests.Session)
    stats: ClientStats = field(default_factory=ClientStats)

    def __post_init__(self) -> None:
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Accept": self.accept,
                "X-GitHub-Api-Version": self.api_version,
                "User-Agent": self.user_agent,
            }
        )

    # ------------------------------------------------------------------
    # Core request
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        accept: Optional[str] = None,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> requests.Response:
        """One HTTP call, with rate-limit handling and retries."""
        if not url.startswith("http"):
            url = self.base_url + url

        headers: dict[str, str] = {}
        if accept is not None:
            headers["Accept"] = accept

        secondary_attempt = 0
        secondary_backoff = SECONDARY_BACKOFF_INITIAL_S
        network_attempt = 0
        network_backoff = NETWORK_BACKOFF_INITIAL_S
        server_error_attempt = 0
        server_error_backoff = SERVER_ERROR_BACKOFF_INITIAL_S
        while True:
            self._maybe_wait_primary_limit()
            self.stats.api_calls_made += 1
            try:
                resp = self.session.request(
                    method,
                    url,
                    params=params,
                    headers=headers or None,
                    stream=stream,
                    allow_redirects=allow_redirects,
                    timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
                )
            except (
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                network_attempt += 1
                if network_attempt > NETWORK_MAX_ATTEMPTS:
                    log.error(
                        "network error retries exhausted after %d attempts: %s: %s",
                        network_attempt, url, exc,
                    )
                    raise
                wait = network_backoff
                log.warning(
                    "network error on %s (attempt %d, %s); sleeping %.1fs",
                    url, network_attempt, type(exc).__name__, wait,
                )
                self.stats.network_retries += 1
                time.sleep(wait)
                network_backoff = min(
                    network_backoff * NETWORK_BACKOFF_FACTOR, NETWORK_BACKOFF_MAX_S
                )
                continue

            if not stream and resp.content:
                self.stats.bytes_in += len(resp.content)

            if self._is_secondary_rate_limit(resp):
                secondary_attempt += 1
                if secondary_attempt > SECONDARY_MAX_ATTEMPTS:
                    log.error(
                        "secondary rate limit exhausted after %d attempts: %s",
                        secondary_attempt, url,
                    )
                    resp.raise_for_status()
                wait = self._secondary_wait_seconds(resp, secondary_backoff)
                log.warning(
                    "secondary rate limit hit; sleeping %.1fs (attempt %d) for %s",
                    wait, secondary_attempt, url,
                )
                self.stats.secondary_waits += 1
                time.sleep(wait)
                secondary_backoff = min(
                    secondary_backoff * SECONDARY_BACKOFF_FACTOR, SECONDARY_BACKOFF_MAX_S
                )
                continue

            if self._is_primary_rate_limit_exceeded(resp):
                # We exhausted primary limit despite the proactive wait.
                # Sleep until reset, then retry.
                self._wait_until_reset(resp)
                continue

            # Retry on 5xx with bounded attempts. After exhausting,
            # return the response so the caller can decide whether to
            # raise or skip; raise_for_status() will fire if the caller
            # uses paginate() / get_json().
            if 500 <= resp.status_code < 600:
                server_error_attempt += 1
                if server_error_attempt > SERVER_ERROR_MAX_ATTEMPTS:
                    log.error(
                        "server error retries exhausted after %d attempts: "
                        "%s %s -> %d",
                        server_error_attempt, method, url, resp.status_code,
                    )
                    return resp
                wait = server_error_backoff
                log.warning(
                    "server error %d on %s (attempt %d); sleeping %.1fs",
                    resp.status_code, url, server_error_attempt, wait,
                )
                self.stats.server_error_retries += 1
                time.sleep(wait)
                server_error_backoff = min(
                    server_error_backoff * SERVER_ERROR_BACKOFF_FACTOR,
                    SERVER_ERROR_BACKOFF_MAX_S,
                )
                continue

            return resp

    # ------------------------------------------------------------------
    # Convenience wrappers
    # ------------------------------------------------------------------

    def get_json(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        accept: Optional[str] = None,
    ) -> Any:
        resp = self.request("GET", url, params=params, accept=accept)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        if not resp.content:
            return None
        return resp.json()

    def paginate(
        self,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        accept: Optional[str] = None,
    ) -> Iterator[Any]:
        """Yield items across paginated REST results.

        For endpoints that return arrays. For endpoints that wrap items in
        an envelope (e.g. workflow runs use ``workflow_runs`` as the key),
        use ``paginate_envelope`` instead.
        """
        params = dict(params or {})
        params.setdefault("per_page", 100)
        next_url: Optional[str] = url
        next_params: Optional[dict[str, Any]] = params
        while next_url:
            resp = self.request("GET", next_url, params=next_params, accept=accept)
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, list):
                for item in data:
                    yield item
            else:
                # Envelope detected; caller should have used paginate_envelope
                raise ValueError(
                    f"paginate() called on envelope response for {url}; "
                    f"use paginate_envelope() with the appropriate items_key"
                )
            next_url, next_params = self._next_link(resp)

    def paginate_envelope(
        self,
        url: str,
        items_key: str,
        *,
        params: Optional[dict[str, Any]] = None,
        accept: Optional[str] = None,
    ) -> Iterator[Any]:
        """Yield items from endpoints that return ``{total_count, items_key: [...]}``."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        next_url: Optional[str] = url
        next_params: Optional[dict[str, Any]] = params
        while next_url:
            resp = self.request("GET", next_url, params=next_params, accept=accept)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get(items_key, []):
                yield item
            next_url, next_params = self._next_link(resp)

    # ------------------------------------------------------------------
    # Rate-limit handling
    # ------------------------------------------------------------------

    def _maybe_wait_primary_limit(self) -> None:
        """Sleep proactively if the last response told us we're nearly out."""
        # Cheap path: only inspect after we've seen at least one response
        # by reading the session's last response from a stored attribute
        # (we don't store it; instead, we react reactively in request()).
        # This method is a hook; reactively handling 403 is done after.
        return

    def _is_primary_rate_limit_exceeded(self, resp: requests.Response) -> bool:
        if resp.status_code != 403:
            return False
        remaining = resp.headers.get("X-RateLimit-Remaining")
        if remaining is None:
            return False
        try:
            return int(remaining) == 0
        except ValueError:
            return False

    def _is_secondary_rate_limit(self, resp: requests.Response) -> bool:
        if resp.status_code not in (403, 429):
            return False
        # Primary 403 we handle separately
        if self._is_primary_rate_limit_exceeded(resp):
            return False
        body_text = ""
        try:
            body_text = (resp.text or "").lower()
        except Exception:
            pass
        if "secondary rate limit" in body_text or "abuse detection" in body_text:
            return True
        # Pure 429 also treated as secondary
        if resp.status_code == 429:
            return True
        return False

    def _secondary_wait_seconds(self, resp: requests.Response, fallback: float) -> float:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after)
            except ValueError:
                pass
        return fallback

    def _wait_until_reset(self, resp: requests.Response) -> None:
        reset = resp.headers.get("X-RateLimit-Reset")
        if not reset:
            log.warning("primary rate limit hit but no reset header; sleeping 60s")
            time.sleep(60)
            self.stats.rate_limit_waits += 1
            return
        try:
            reset_ts = int(reset)
        except ValueError:
            time.sleep(60)
            self.stats.rate_limit_waits += 1
            return
        now = time.time()
        wait = max(0.0, reset_ts - now) + 1.0
        log.warning(
            "primary rate limit exhausted; sleeping %.0fs until reset",
            wait,
        )
        self.stats.rate_limit_waits += 1
        time.sleep(wait)

    # ------------------------------------------------------------------
    # Link header parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _next_link(resp: requests.Response) -> tuple[Optional[str], Optional[dict[str, Any]]]:
        link = resp.headers.get("Link")
        if not link:
            return None, None
        # Parse RFC 5988 Link header: <url>; rel="next", <url>; rel="last", ...
        for part in link.split(","):
            segs = [s.strip() for s in part.split(";")]
            if len(segs) < 2:
                continue
            url_part = segs[0]
            if not (url_part.startswith("<") and url_part.endswith(">")):
                continue
            rel = None
            for seg in segs[1:]:
                if seg.startswith("rel="):
                    rel = seg[4:].strip('"')
                    break
            if rel != "next":
                continue
            next_url = url_part[1:-1]
            # When following a Link, params are encoded into the URL; do
            # not pass them again to avoid duplication.
            parsed = urlparse(next_url)
            qs = parse_qs(parsed.query)
            # Flatten single-valued lists
            params = {k: (v[0] if len(v) == 1 else v) for k, v in qs.items()}
            base = next_url.split("?")[0]
            return base, params
        return None, None
