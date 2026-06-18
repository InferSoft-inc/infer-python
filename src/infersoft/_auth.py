"""OAuth2 client-credentials authentication for the Infersoft API.

Fetches a bearer token from the Auth0 token endpoint, caches it until shortly
before expiry, and transparently refreshes on demand (including on a 401).
Implements both the sync and async httpx auth flows so it backs ``Client`` and
``AsyncClient`` alike.

Refreshes are single-flight in both flows (a thread lock and an asyncio lock):
concurrent callers at expiry produce exactly one token request — the token
endpoint is rate-limited and M2M token issuance is billable, so a thundering
herd has real cost. Transient token-endpoint failures are retried with jittered
backoff; terminal ones (bad credentials) fail immediately.
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import AsyncGenerator, Generator

import httpx

from ._version import __version__
from .errors import AuthenticationError

_USER_AGENT = f"infersoft-python/{__version__}"

#: Transient token-endpoint statuses worth retrying. Kept in sync with
#: ``_http._RETRY_STATUSES`` (importing it would be circular: _http imports
#: _auth). Terminal statuses — notably 401/403, i.e. bad credentials — fail
#: immediately: retrying them is a pointless storm against the token endpoint.
_TRANSIENT_TOKEN_STATUSES = {408, 429, 500, 502, 503, 504}

#: Token endpoint requests keep their own timeout, independent of API timeouts.
_TOKEN_TIMEOUT = 30.0


def _token_backoff(attempt: int) -> float:
    """Full-jitter backoff for token retries (mirrors the HTTP layer's)."""
    return random.uniform(0, min(8.0, 0.5 * (2.0**attempt)))


class ClientCredentialsAuth(httpx.Auth):
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_url: str,
        audience: str,
        scope: str | None = None,
        leeway: int = 60,
        max_retries: int = 2,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_url = token_url
        self._audience = audience
        self._scope = scope
        self._leeway = leeway
        self._max_retries = max_retries

        self._lock = threading.Lock()
        # Safe to construct without a running loop on Python >= 3.10 (no loop
        # binding at creation); each client builds its own auth, so the lock
        # never crosses event loops.
        self._alock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def _is_valid(self) -> bool:
        return self._token is not None and time.monotonic() < (self._expires_at - self._leeway)

    def _payload(self) -> dict[str, str]:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "audience": self._audience,
        }
        if self._scope:
            payload["scope"] = self._scope
        return payload

    def _store(self, resp: httpx.Response) -> None:
        """Cache the token from a 200 token response."""
        try:
            data = resp.json()
        except ValueError as exc:
            raise AuthenticationError("token endpoint returned invalid JSON") from exc
        token = data.get("access_token")
        if not token:
            raise AuthenticationError("token endpoint response did not include access_token")
        self._token = token
        self._expires_at = time.monotonic() + float(data.get("expires_in", 3600))

    def _classify(self, resp: httpx.Response, attempt: int) -> bool:
        """Return True when the response should be retried, raise on terminal
        non-200s, and return False (after storing) on success."""
        if resp.status_code in _TRANSIENT_TOKEN_STATUSES and attempt < self._max_retries:
            return True
        if resp.status_code != 200:
            raise AuthenticationError(
                f"token endpoint returned {resp.status_code}: {resp.text[:500]}"
            )
        self._store(resp)
        return False

    def _fetch_token(self) -> None:
        attempt = 0
        while True:
            try:
                resp = httpx.post(
                    self._token_url,
                    json=self._payload(),
                    headers={"User-Agent": _USER_AGENT},
                    timeout=_TOKEN_TIMEOUT,
                )
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    time.sleep(_token_backoff(attempt))
                    attempt += 1
                    continue
                raise AuthenticationError(f"token request failed: {exc}") from exc
            except httpx.HTTPError as exc:
                raise AuthenticationError(f"token request failed: {exc}") from exc

            if self._classify(resp, attempt):
                time.sleep(_token_backoff(attempt))
                attempt += 1
                continue
            return

    async def _afetch_token(self) -> None:
        attempt = 0
        while True:
            try:
                # One-shot client is fine here: refreshes happen ~once an hour
                # (unlike the per-file presigned PUTs, which pool).
                async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT) as client:
                    resp = await client.post(
                        self._token_url,
                        json=self._payload(),
                        headers={"User-Agent": _USER_AGENT},
                    )
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(_token_backoff(attempt))
                    attempt += 1
                    continue
                raise AuthenticationError(f"token request failed: {exc}") from exc
            except httpx.HTTPError as exc:
                raise AuthenticationError(f"token request failed: {exc}") from exc

            if self._classify(resp, attempt):
                await asyncio.sleep(_token_backoff(attempt))
                attempt += 1
                continue
            return

    def _should_fetch(self, force: bool, stale: str | None) -> bool:
        """The under-the-lock re-check: fetch only if still needed.

        A forced refresh (401 recovery) re-fetches only when the cached token
        is still the stale one the caller saw — if another caller already
        refreshed while we waited on the lock, reuse their token instead of
        forcing a redundant fetch.
        """
        if force and self._token == stale:
            return True
        return not self._is_valid()

    def _ensure_token(self, force: bool = False) -> str:
        stale = self._token if force else None
        if force or not self._is_valid():
            with self._lock:
                if self._should_fetch(force, stale):
                    self._fetch_token()
        assert self._token is not None
        return self._token

    async def _aensure_token(self, force: bool = False) -> str:
        stale = self._token if force else None
        if force or not self._is_valid():
            async with self._alock:
                if self._should_fetch(force, stale):
                    await self._afetch_token()
        assert self._token is not None
        return self._token

    def sync_auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        request.headers["Authorization"] = f"Bearer {self._ensure_token()}"
        response = yield request

        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {self._ensure_token(force=True)}"
            yield request

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        request.headers["Authorization"] = f"Bearer {await self._aensure_token()}"
        response = yield request

        if response.status_code == 401:
            request.headers["Authorization"] = f"Bearer {await self._aensure_token(force=True)}"
            yield request
