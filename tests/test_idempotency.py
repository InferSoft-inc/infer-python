"""Idempotency-Key behavior for write operations."""

from __future__ import annotations

import pytest

from infersoft import APIError, UnprocessableEntityError

_PROJECT = {
    "id": 7,
    "organization_id": "org",
    "name": "X",
    "created_at": "2026-05-29T00:00:00Z",
}
_EMPTY_PAGE = {"items": [], "page": 1, "page_size": 50, "has_more": False}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("infersoft._http.time.sleep", lambda _seconds: None)


def test_write_sends_generated_idempotency_key(client, httpx_mock):
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)

    client.projects.create("X")

    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/projects")
    key = post.headers["Idempotency-Key"]
    assert len(key) == 32 and all(c in "0123456789abcdef" for c in key)


def test_user_supplied_idempotency_key_is_used(client, httpx_mock):
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)

    client.projects.create("X", idempotency_key="my-key-123")

    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/projects")
    assert post.headers["Idempotency-Key"] == "my-key-123"


def test_write_is_retried_and_reuses_the_same_key(client, httpx_mock):
    # The key now makes writes retry-safe, so a transient failure is retried —
    # and every attempt must carry the *same* key so the server can dedupe.
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects", status_code=503, json={"title": "down"}
    )
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)

    proj = client.projects.create("X")

    assert proj.id == 7
    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects"]
    assert len(posts) == 2
    assert len({r.headers["Idempotency-Key"] for r in posts}) == 1


def test_search_does_not_send_idempotency_key(client, httpx_mock):
    # Read-only POST searches must not carry a key (they are not idempotency
    # endpoints on the server, and a key would be pointless/confusing).
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects/search", json=_EMPTY_PAGE
    )

    client.projects.search()

    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/search")
    assert "idempotency-key" not in {k.lower() for k in post.headers}


def test_idempotency_key_reuse_raises_unprocessable_entity(client, httpx_mock):
    # The server returns 422 when a key is reused with a different body. The SDK
    # must surface that as a specific, catchable error — not a generic APIError —
    # so callers can react to "reused key with different params" precisely.
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects",
        status_code=422,
        json={
            "title": "Idempotency-Key reuse",
            "detail": "This Idempotency-Key was already used with different request parameters.",
        },
    )

    with pytest.raises(UnprocessableEntityError) as exc_info:
        client.projects.create("X", idempotency_key="reused-key")

    err = exc_info.value
    assert err.status_code == 422
    assert isinstance(err, APIError)  # still catchable by the broad base class
    # A 422 is not retried (not in the retryable set), so only one request went out.
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/api/projects"]) == 1


# -------- in-flight 409 retry --------

_IN_PROGRESS_TYPE = "https://api.infersoft.com/problems/request-in-progress"
_IN_PROGRESS_BODY = {
    "type": _IN_PROGRESS_TYPE,
    "title": "Request in progress",
    "status": 409,
    "detail": "A request with this Idempotency-Key is already being processed.",
}


def test_in_progress_409_is_retried_with_same_key(client, httpx_mock, monkeypatch):
    # The designed path: a keyed write whose first attempt finds the key still
    # executing retries with the SAME key, honoring Retry-After, and wins.
    delays: list[float] = []
    monkeypatch.setattr("infersoft._http.time.sleep", delays.append)
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects",
        status_code=409,
        headers={"Retry-After": "2"},
        json=_IN_PROGRESS_BODY,
    )
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)

    proj = client.projects.create("X", idempotency_key="k-slow")

    assert proj.id == 7
    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects"]
    assert len(posts) == 2
    assert {r.headers["Idempotency-Key"] for r in posts} == {"k-slow"}
    assert delays == [2.0]  # the server's Retry-After drove the wait


def test_untyped_409_stays_terminal(client, httpx_mock):
    # The boundary that must not move: a real conflict (no problem type) is
    # NEVER retried, keyed or not.
    from infersoft import ConflictError

    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects",
        status_code=409,
        json={"title": "Conflict", "status": 409, "detail": "project already exists"},
    )

    with pytest.raises(ConflictError):
        client.projects.create("X")

    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects"]
    assert len(posts) == 1


def test_typed_409_not_retried_when_request_not_retryable(client, httpx_mock):
    # The retry flag gates it: an explicitly non-idempotent request treats even
    # the typed 409 as terminal.
    from infersoft import ConflictError

    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects",
        status_code=409,
        headers={"Retry-After": "2"},
        json=_IN_PROGRESS_BODY,
    )

    with pytest.raises(ConflictError):
        client._http.request("POST", "/api/projects", json={"name": "X"}, idempotent=False)

    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects"]
    assert len(posts) == 1


def test_in_progress_409_budget_exhaustion(client, httpx_mock):
    # A write that outlives the budget (default 2 retries -> 3 attempts) still
    # ends in ConflictError — but one whose detail says it is in progress.
    from infersoft import ConflictError

    for _ in range(3):
        httpx_mock.add_response(
            method="POST",
            url="https://api.test/api/projects",
            status_code=409,
            headers={"Retry-After": "2"},
            json=_IN_PROGRESS_BODY,
        )

    with pytest.raises(ConflictError, match="already being processed"):
        client.projects.create("X", idempotency_key="k-slow")

    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects"]
    assert len(posts) == 3


async def test_async_in_progress_409_is_retried(async_client, httpx_mock, monkeypatch):
    delays: list[float] = []

    async def _record(seconds):
        delays.append(seconds)

    monkeypatch.setattr("infersoft._http.asyncio.sleep", _record)
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects",
        status_code=409,
        headers={"Retry-After": "2"},
        json=_IN_PROGRESS_BODY,
    )
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)

    proj = await async_client.projects.create("X", idempotency_key="k-slow")

    assert proj.id == 7
    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects"]
    assert len(posts) == 2
    assert delays == [2.0]
