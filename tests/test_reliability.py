"""Retry-safety, backoff, and error-metadata behavior."""

from __future__ import annotations

import httpx
import pytest

from infersoft import (
    APIConnectionError,
    APITimeoutError,
    InfersoftError,
    NotFoundError,
    ServerError,
)

_JOB = {
    "id": 5,
    "organization_id": "org",
    "organization_name": "Org",
    "total_docs": 0,
    "completed_docs": 0,
    "error_count": 0,
    "status": "completed",
    "createdAt": "2026-05-29T00:00:00Z",
}
_EMPTY_PAGE = {"items": [], "page": 1, "page_size": 50, "has_more": False}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    # Keep retry tests fast and deterministic (sync and async backoff).
    monkeypatch.setattr("infersoft._http.time.sleep", lambda _seconds: None)

    async def _anap(_seconds):
        return None

    monkeypatch.setattr("infersoft._http.asyncio.sleep", _anap)


def test_idempotent_get_is_retried_then_succeeds(client, httpx_mock):
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/jobs/5", status_code=503, json={"title": "down"}
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_JOB)

    job = client.jobs.get(5)

    assert job.id == 5
    gets = [r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/5"]
    assert len(gets) == 2


def test_unkeyed_post_is_not_retried(client, httpx_mock):
    # Retry-safety for writes is opt-in: a bare POST with no idempotency key and
    # no explicit idempotent flag is still not retried at the HTTP layer. (The
    # resource methods opt in by attaching a key — see test_idempotency.)
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/raw", status_code=500, json={"title": "boom"}
    )

    with pytest.raises(ServerError):
        client._http.request("POST", "/api/raw", json={})

    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/raw"]
    assert len(posts) == 1


def test_readonly_search_post_is_retried(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/search",
        status_code=503,
        json={"title": "unavailable"},
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_EMPTY_PAGE
    )

    page = client.documents.search()

    assert page.page == 1
    searches = [r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/search"]
    assert len(searches) == 2


def test_api_error_carries_request_id(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/documents/1",
        status_code=404,
        json={"title": "Not Found", "detail": "no such document"},
        headers={"x-request-id": "req-abc123"},
    )

    with pytest.raises(NotFoundError) as exc_info:
        client.documents.get(1)

    err = exc_info.value
    assert err.status_code == 404
    assert err.request_id == "req-abc123"
    assert "req-abc123" in str(err)


def test_transport_error_is_retried_then_succeeds(client, httpx_mock):
    httpx_mock.add_exception(
        httpx.ReadTimeout("slow"), method="GET", url="https://api.test/api/jobs/5"
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_JOB)

    job = client.jobs.get(5)  # retried past the timeout
    assert job.id == 5


def test_transport_timeout_exhausts_budget_and_wraps(client, httpx_mock):
    # max_retries=2 → 3 attempts; all time out.
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ConnectTimeout("nope"), method="GET", url="https://api.test/api/jobs/5"
        )

    with pytest.raises(APITimeoutError) as exc_info:
        client.jobs.get(5)

    err = exc_info.value
    # Standard hierarchy: timeout is a connection error is an SDK error.
    assert isinstance(err, APIConnectionError) and isinstance(err, InfersoftError)
    assert isinstance(err.__cause__, httpx.ConnectTimeout)  # original preserved


def test_connection_error_wraps_as_api_connection_error(client, httpx_mock):
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ConnectError("refused"), method="GET", url="https://api.test/api/jobs/5"
        )

    with pytest.raises(APIConnectionError) as exc_info:
        client.jobs.get(5)

    assert not isinstance(exc_info.value, APITimeoutError)  # not a timeout
    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


def test_unkeyed_post_transport_error_is_not_retried(client, httpx_mock):
    # Non-retryable request: a single transport error surfaces immediately
    # (only one exception is registered; a retry would find no mock).
    httpx_mock.add_exception(
        httpx.ConnectError("refused"), method="POST", url="https://api.test/api/raw"
    )

    with pytest.raises(APIConnectionError):
        client._http.request("POST", "/api/raw", json={})


async def test_async_transport_error_is_retried_then_succeeds(async_client, httpx_mock):
    httpx_mock.add_exception(
        httpx.ReadTimeout("slow"), method="GET", url="https://api.test/api/jobs/5"
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_JOB)

    job = await async_client.jobs.get(5)
    assert job.id == 5


async def test_async_transport_error_wraps_after_budget(async_client, httpx_mock):
    for _ in range(3):
        httpx_mock.add_exception(
            httpx.ConnectError("refused"), method="GET", url="https://api.test/api/jobs/5"
        )

    with pytest.raises(APIConnectionError) as exc_info:
        await async_client.jobs.get(5)
    assert isinstance(exc_info.value.__cause__, httpx.ConnectError)


# -------- Retry-After clamping --------


def _recording_sleep(monkeypatch) -> list[float]:
    """Replace the no-op sleep with a recorder so tests can assert delays."""
    delays: list[float] = []
    monkeypatch.setattr("infersoft._http.time.sleep", delays.append)
    return delays


def test_retry_after_is_capped(client, httpx_mock, monkeypatch):
    # A day-long Retry-After must not stall the client: clamp to 60s.
    delays = _recording_sleep(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/jobs/5",
        status_code=429,
        headers={"Retry-After": "86400"},
        json={"title": "slow down"},
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_JOB)

    client.jobs.get(5)

    assert delays == [60.0]


def test_negative_retry_after_does_not_crash(client, httpx_mock, monkeypatch):
    # time.sleep(-5) raises ValueError; the clamp turns a negative hint into
    # an immediate retry instead of crashing the retry loop.
    delays = _recording_sleep(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/jobs/5",
        status_code=429,
        headers={"Retry-After": "-5"},
        json={"title": "weird"},
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_JOB)

    client.jobs.get(5)

    assert delays == [0.0]


def test_unparseable_retry_after_falls_back_to_jitter(client, httpx_mock, monkeypatch):
    # HTTP-date or garbage: the jittered backoff takes over (bounded by
    # _MAX_BACKOFF), not a parse of the header.
    delays = _recording_sleep(monkeypatch)
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/jobs/5",
        status_code=429,
        headers={"Retry-After": "Fri, 31 Dec 2027 23:59:59 GMT"},
        json={"title": "dated"},
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_JOB)

    client.jobs.get(5)

    assert len(delays) == 1
    assert 0.0 <= delays[0] <= 8.0  # _MAX_BACKOFF ceiling, attempt 0


async def test_async_retry_after_is_capped(async_client, httpx_mock, monkeypatch):
    # Twin parity: the async client clamps through the same _retry_delay.
    delays: list[float] = []

    async def _record(seconds):
        delays.append(seconds)

    monkeypatch.setattr("infersoft._http.asyncio.sleep", _record)
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/jobs/5",
        status_code=429,
        headers={"Retry-After": "86400"},
        json={"title": "slow down"},
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_JOB)

    await async_client.jobs.get(5)

    assert delays == [60.0]
