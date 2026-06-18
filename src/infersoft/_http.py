"""Thin HTTP layer: authenticated JSON requests plus raw presigned uploads."""

from __future__ import annotations

import asyncio
import copy as _copy
import logging
import random
import time
import uuid
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

import httpx

from ._auth import ClientCredentialsAuth
from ._version import __version__
from .errors import (
    APIConnectionError,
    APITimeoutError,
    DownloadTransferError,
    InfersoftError,
    UploadTransferError,
    raise_for_problem,
    request_id_of,
)

logger = logging.getLogger("infersoft")


def _wrap_transport_error(
    exc: httpx.TransportError, method: str, path: str
) -> InfersoftError:
    """Map a transport-level httpx failure onto the SDK exception tree."""
    if isinstance(exc, httpx.TimeoutException):
        return APITimeoutError(f"{method} {path} timed out")
    return APIConnectionError(f"{method} {path} could not reach the API: {exc}")

#: Transient statuses that are safe to retry. This is a curated subset, not a
#: category check: 409 (deterministic conflict) and 501/505 are deliberately
#: excluded.
_RETRY_STATUSES = {
    HTTPStatus.REQUEST_TIMEOUT,
    HTTPStatus.TOO_MANY_REQUESTS,
    HTTPStatus.INTERNAL_SERVER_ERROR,
    HTTPStatus.BAD_GATEWAY,
    HTTPStatus.SERVICE_UNAVAILABLE,
    HTTPStatus.GATEWAY_TIMEOUT,
}

#: HTTP methods that are inherently safe to retry. Mutating POSTs are retried
#: only when they carry an Idempotency-Key (so the server dedupes the replay) or
#: a caller explicitly marks the request idempotent (e.g. read-only searches).
_IDEMPOTENT_METHODS = {"GET", "HEAD", "OPTIONS", "DELETE", "PUT"}

#: Upper bound on a single backoff sleep, before jitter.
_MAX_BACKOFF = 8.0

#: Ceiling on a server-supplied Retry-After. A misbehaving server or
#: intermediary must not be able to stall clients arbitrarily; anything above
#: this is suspect (our own server emits 1-60s across all its Retry-After
#: sources). Negative values clamp to 0 (retry immediately) rather than
#: crashing time.sleep.
_MAX_RETRY_AFTER = 60.0

#: Request header carrying a client idempotency key (matches the API).
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: RFC 9457 problem `type` marking the server's transient in-flight 409:
#: another request with the same Idempotency-Key is still executing, and the
#: response carries a Retry-After. Must match the server's
#: idempotency.ProblemTypeRequestInProgress byte-for-byte — coordinate changes.
#: Untyped 409s are deterministic conflicts and stay terminal.
_PROBLEM_TYPE_REQUEST_IN_PROGRESS = "https://api.infersoft.com/problems/request-in-progress"


def _is_request_in_progress(resp: httpx.Response) -> bool:
    """Defensively detect the transient in-flight 409 by its problem type."""
    try:
        body = resp.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    return body.get("type") == _PROBLEM_TYPE_REQUEST_IN_PROGRESS


def _is_retryable_response(resp: httpx.Response, retry: bool) -> bool:
    """Shared (sync/async) response-retry decision for _send.

    Beyond the transient status set, a 409 carrying the request-in-progress
    problem type is retryable for retry-enabled requests: the same key is
    still executing server-side, and a later retry replays its recorded
    response. The body parse only ever runs on 409s.
    """
    if not retry:
        return False
    if resp.status_code in _RETRY_STATUSES:
        return True
    return resp.status_code == HTTPStatus.CONFLICT and _is_request_in_progress(resp)


def new_idempotency_key() -> str:
    """Return a fresh idempotency key (uuid4 hex, 32 chars)."""
    return uuid.uuid4().hex


class HttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth: ClientCredentialsAuth,
        timeout: float,
        max_retries: int,
        upload_timeout: float,
        upload_max_retries: int | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            auth=auth,
            timeout=timeout,
            headers={
                "User-Agent": f"infersoft-python/{__version__}",
                "Accept": "application/json",
            },
        )
        self._max_retries = max_retries
        self._upload_timeout = upload_timeout
        # Retry budget for presigned PUTs; None falls back to max_retries.
        self._upload_max_retries = upload_max_retries
        # Separate pooled client for presigned (S3) PUTs: unauthenticated (the
        # presigned URL carries its own signature; the API bearer token would
        # break it) and using the longer upload timeout. One long-lived client
        # so a bulk upload reuses connections instead of opening one per file.
        self._upload_client = httpx.Client(timeout=upload_timeout)
        # Per-request timeout applied instead of the pooled client's default.
        # Set on copies made by copy_with(); None means use the client default.
        self._timeout_override: float | None = None
        # Copies made by copy_with() share the pooled httpx clients but do not
        # own them: close() on a copy is a no-op so it can be used as a context
        # manager without bricking the original.
        self._owns_client = True

    def copy_with(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        upload_max_retries: int | None = None,
    ) -> HttpClient:
        """Return a copy with overrides, sharing the pooled connection and auth.

        The copy reuses this client's underlying ``httpx.Client`` (no new
        connections or token cache). It does not own the shared client, so
        closing the copy is a no-op; only closing the original releases the
        pool. The ``timeout`` override applies to API requests; OAuth token
        acquisition keeps its own 30s timeout.
        """
        new = _copy.copy(self)
        new._owns_client = False
        if timeout is not None:
            new._timeout_override = timeout
        if max_retries is not None:
            new._max_retries = max_retries
        if upload_max_retries is not None:
            new._upload_max_retries = upload_max_retries
        return new

    def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        idempotent: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        """Send an authenticated JSON request and return the parsed body.

        ``idempotency_key`` makes a mutating write safe to retry: it is sent as
        the ``Idempotency-Key`` header (the server records the first response and
        replays it for any retry carrying the same key) and, on its own, enables
        retries for the request.

        ``idempotent`` overrides retryability directly. When both are unset,
        retryability is inferred from the HTTP method, so read-only POSTs (e.g.
        search) must pass ``idempotent=True`` to be retried.

        A 409 carrying the ``request-in-progress`` problem type (the same key
        is still executing server-side) is retried like a transient, honoring
        its Retry-After. If the write outlives the retry budget, the final
        ``ConflictError``'s detail says it is still in progress — raise
        ``max_retries`` via ``with_options`` for known-slow writes. Untyped
        409s (real conflicts) are never retried.
        """
        if idempotent is not None:
            retry = idempotent
        else:
            retry = method.upper() in _IDEMPOTENT_METHODS or idempotency_key is not None
        headers = {IDEMPOTENCY_HEADER: idempotency_key} if idempotency_key else None
        resp = self._send(method, path, json=json, params=params, retry=retry, headers=headers)
        if resp.is_error:
            raise_for_problem(resp)
        if resp.status_code == HTTPStatus.NO_CONTENT or not resp.content:
            return None
        return resp.json()

    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any | None,
        params: Mapping[str, Any] | None,
        retry: bool,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        timeout = (
            self._timeout_override
            if self._timeout_override is not None
            else httpx.USE_CLIENT_DEFAULT
        )
        attempt = 0
        while True:
            try:
                resp = self._client.request(
                    method, path, json=json, params=params, headers=headers, timeout=timeout
                )
            except httpx.TransportError as exc:
                # No response was received (timeout, connection reset, …) — the
                # same transient class the Idempotency-Key protects against. Retry
                # on the same budget/backoff, then surface as an SDK error.
                if retry and attempt < self._max_retries:
                    delay = self._backoff(attempt)
                    logger.debug(
                        "retrying %s %s after transport error %r in %.2fs (attempt %d/%d)",
                        method, path, exc, delay, attempt + 1, self._max_retries,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise _wrap_transport_error(exc, method, path) from exc

            if _is_retryable_response(resp, retry) and attempt < self._max_retries:
                delay = self._retry_delay(resp, attempt)
                logger.debug(
                    "retrying %s %s after %s in %.2fs (attempt %d/%d)",
                    method,
                    path,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                time.sleep(delay)
                attempt += 1
                continue
            return resp

    @staticmethod
    def _backoff(attempt: int) -> float:
        """Exponential backoff with full jitter to avoid synchronized retries."""
        ceiling = min(_MAX_BACKOFF, 0.5 * (2.0**attempt))
        return random.uniform(0, ceiling)

    def _upload_retry_budget(self) -> int:
        """Retries for presigned PUTs: upload_max_retries, else max_retries."""
        if self._upload_max_retries is not None:
            return self._upload_max_retries
        return self._max_retries

    @staticmethod
    def _retry_delay(resp: httpx.Response, attempt: int) -> float:
        retry_after = resp.headers.get("Retry-After")
        if retry_after:
            try:
                return max(0.0, min(float(retry_after), _MAX_RETRY_AFTER))
            except ValueError:
                pass  # HTTP-date or garbage: fall back to jittered backoff
        return HttpClient._backoff(attempt)

    def put_presigned(
        self, url: str, data: bytes, headers: Mapping[str, str] | None
    ) -> None:
        """PUT raw bytes to a presigned (S3) URL. Intentionally unauthenticated:
        the presigned URL already carries its own signature, and attaching the
        API bearer token would break the signature.

        Transient failures (transport errors, 408/429/5xx) are retried with the
        same backoff as API requests — the bytes are immutable and S3 PUTs are
        idempotent, so replays are safe. The budget is ``upload_max_retries``
        (falling back to ``max_retries``). Other 4xx fail immediately: an
        expired or invalid signed URL surfaces as S3 403, and the recovery is
        re-planning the batch, not re-PUTting a dead URL.
        """
        budget = self._upload_retry_budget()
        attempt = 0
        while True:
            try:
                resp = self._upload_client.put(url, content=data, headers=dict(headers or {}))
            except httpx.TransportError as exc:
                if attempt < budget:
                    delay = self._backoff(attempt)
                    logger.debug(
                        "retrying presigned PUT after transport error %r in %.2fs "
                        "(attempt %d/%d)", exc, delay, attempt + 1, budget,
                    )
                    time.sleep(delay)
                    attempt += 1
                    continue
                raise UploadTransferError(f"presigned upload failed: {exc}") from exc
            except httpx.HTTPError as exc:
                raise UploadTransferError(f"presigned upload failed: {exc}") from exc

            if resp.status_code in _RETRY_STATUSES and attempt < budget:
                time.sleep(self._retry_delay(resp, attempt))
                attempt += 1
                continue
            if resp.is_error:
                raise UploadTransferError(
                    f"presigned upload returned {resp.status_code}",
                    status_code=resp.status_code,
                    request_id=request_id_of(resp),
                )
            return

    def get_presigned(self, url: str) -> bytes:
        """GET raw bytes from a signed download URL. Intentionally
        unauthenticated: the URL carries its own signature, and a bearer token
        would break it. Retries share the presigned-transfer budget
        (``upload_max_retries``, falling back to ``max_retries``); other 4xx
        fail fast — an expired signed URL needs a fresh ``download_url`` call,
        not a retry."""
        budget = self._upload_retry_budget()
        attempt = 0
        while True:
            try:
                resp = self._upload_client.get(url)
            except httpx.TransportError as exc:
                if attempt < budget:
                    time.sleep(self._backoff(attempt))
                    attempt += 1
                    continue
                raise DownloadTransferError(f"signed download failed: {exc}") from exc
            except httpx.HTTPError as exc:
                raise DownloadTransferError(f"signed download failed: {exc}") from exc

            if resp.status_code in _RETRY_STATUSES and attempt < budget:
                time.sleep(self._retry_delay(resp, attempt))
                attempt += 1
                continue
            if resp.is_error:
                raise DownloadTransferError(
                    f"signed download returned {resp.status_code}",
                    status_code=resp.status_code,
                    request_id=request_id_of(resp),
                )
            return resp.content

    def close(self) -> None:
        if self._owns_client:
            self._client.close()
            self._upload_client.close()


class AsyncHttpClient:
    """Async twin of :class:`HttpClient` (shares its constants and backoff)."""

    def __init__(
        self,
        *,
        base_url: str,
        auth: ClientCredentialsAuth,
        timeout: float,
        max_retries: int,
        upload_timeout: float,
        upload_max_retries: int | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=auth,
            timeout=timeout,
            headers={
                "User-Agent": f"infersoft-python/{__version__}",
                "Accept": "application/json",
            },
        )
        self._max_retries = max_retries
        self._upload_timeout = upload_timeout
        # Retry budget for presigned PUTs; None falls back to max_retries.
        self._upload_max_retries = upload_max_retries
        # One long-lived, unauthenticated client for presigned (S3) PUTs, so a
        # bulk upload pools connections instead of opening one per file. See the
        # sync HttpClient for the full rationale.
        self._upload_client = httpx.AsyncClient(timeout=upload_timeout)
        # See HttpClient._timeout_override / _owns_client.
        self._timeout_override: float | None = None
        self._owns_client = True

    def copy_with(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        upload_max_retries: int | None = None,
    ) -> AsyncHttpClient:
        """Return a copy with overrides, sharing the pooled connection and auth.

        The copy reuses this client's underlying ``httpx.AsyncClient`` (no new
        connections or token cache). It does not own the shared client, so
        closing the copy is a no-op; only closing the original releases the
        pool. The ``timeout`` override applies to API requests; OAuth token
        acquisition keeps its own 30s timeout.
        """
        new = _copy.copy(self)
        new._owns_client = False
        if timeout is not None:
            new._timeout_override = timeout
        if max_retries is not None:
            new._max_retries = max_retries
        if upload_max_retries is not None:
            new._upload_max_retries = upload_max_retries
        return new

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: Mapping[str, Any] | None = None,
        idempotent: bool | None = None,
        idempotency_key: str | None = None,
    ) -> Any:
        if idempotent is not None:
            retry = idempotent
        else:
            retry = method.upper() in _IDEMPOTENT_METHODS or idempotency_key is not None
        headers = {IDEMPOTENCY_HEADER: idempotency_key} if idempotency_key else None
        resp = await self._send(
            method, path, json=json, params=params, retry=retry, headers=headers
        )
        if resp.is_error:
            raise_for_problem(resp)
        if resp.status_code == HTTPStatus.NO_CONTENT or not resp.content:
            return None
        return resp.json()

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any | None,
        params: Mapping[str, Any] | None,
        retry: bool,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        timeout = (
            self._timeout_override
            if self._timeout_override is not None
            else httpx.USE_CLIENT_DEFAULT
        )
        attempt = 0
        while True:
            try:
                resp = await self._client.request(
                    method, path, json=json, params=params, headers=headers, timeout=timeout
                )
            except httpx.TransportError as exc:
                if retry and attempt < self._max_retries:
                    delay = HttpClient._backoff(attempt)
                    logger.debug(
                        "retrying %s %s after transport error %r in %.2fs (attempt %d/%d)",
                        method, path, exc, delay, attempt + 1, self._max_retries,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise _wrap_transport_error(exc, method, path) from exc

            if _is_retryable_response(resp, retry) and attempt < self._max_retries:
                delay = HttpClient._retry_delay(resp, attempt)
                logger.debug(
                    "retrying %s %s after %s in %.2fs (attempt %d/%d)",
                    method,
                    path,
                    resp.status_code,
                    delay,
                    attempt + 1,
                    self._max_retries,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue
            return resp

    def _upload_retry_budget(self) -> int:
        """Retries for presigned PUTs: upload_max_retries, else max_retries."""
        if self._upload_max_retries is not None:
            return self._upload_max_retries
        return self._max_retries

    async def put_presigned(
        self, url: str, data: bytes, headers: Mapping[str, str] | None
    ) -> None:
        """Async :meth:`HttpClient.put_presigned` (same retry semantics)."""
        budget = self._upload_retry_budget()
        attempt = 0
        while True:
            try:
                resp = await self._upload_client.put(
                    url, content=data, headers=dict(headers or {})
                )
            except httpx.TransportError as exc:
                if attempt < budget:
                    delay = HttpClient._backoff(attempt)
                    logger.debug(
                        "retrying presigned PUT after transport error %r in %.2fs "
                        "(attempt %d/%d)", exc, delay, attempt + 1, budget,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                raise UploadTransferError(f"presigned upload failed: {exc}") from exc
            except httpx.HTTPError as exc:
                raise UploadTransferError(f"presigned upload failed: {exc}") from exc

            if resp.status_code in _RETRY_STATUSES and attempt < budget:
                await asyncio.sleep(HttpClient._retry_delay(resp, attempt))
                attempt += 1
                continue
            if resp.is_error:
                raise UploadTransferError(
                    f"presigned upload returned {resp.status_code}",
                    status_code=resp.status_code,
                    request_id=request_id_of(resp),
                )
            return

    async def get_presigned(self, url: str) -> bytes:
        """Async :meth:`HttpClient.get_presigned` (same retry semantics)."""
        budget = self._upload_retry_budget()
        attempt = 0
        while True:
            try:
                resp = await self._upload_client.get(url)
            except httpx.TransportError as exc:
                if attempt < budget:
                    await asyncio.sleep(HttpClient._backoff(attempt))
                    attempt += 1
                    continue
                raise DownloadTransferError(f"signed download failed: {exc}") from exc
            except httpx.HTTPError as exc:
                raise DownloadTransferError(f"signed download failed: {exc}") from exc

            if resp.status_code in _RETRY_STATUSES and attempt < budget:
                await asyncio.sleep(HttpClient._retry_delay(resp, attempt))
                attempt += 1
                continue
            if resp.is_error:
                raise DownloadTransferError(
                    f"signed download returned {resp.status_code}",
                    status_code=resp.status_code,
                    request_id=request_id_of(resp),
                )
            return resp.content

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
            await self._upload_client.aclose()
