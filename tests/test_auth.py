"""Token-endpoint reliability: bounded retries on transients, fail-fast on bad
credentials, and single-flight refresh under concurrency."""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from infersoft import AsyncClient, AuthenticationError, Client
from infersoft._auth import ClientCredentialsAuth

_KW = {
    "client_id": "id",
    "client_secret": "secret",
    "base_url": "https://api.test",
    "token_url": "https://auth.test/oauth/token",
    "audience": "https://api.test",
}
_TOKEN_URL = "https://auth.test/oauth/token"
_TOKEN = {"access_token": "tok", "expires_in": 3600, "token_type": "Bearer"}


def _job(jid: int) -> dict:
    return {
        "id": jid,
        "organization_id": "org",
        "organization_name": "Org",
        "total_docs": 1,
        "completed_docs": 0,
        "error_count": 0,
        "status": "completed",
        "createdAt": "2026-05-29T00:00:00Z",
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("infersoft._auth.time.sleep", lambda _s: None)

    async def _nap(_s):
        return None

    monkeypatch.setattr("infersoft._auth.asyncio.sleep", _nap)


def _token_requests(httpx_mock) -> list:
    return [r for r in httpx_mock.get_requests() if r.url.path == "/oauth/token"]


def test_token_transient_503_is_retried(httpx_mock):
    httpx_mock.add_response(url=_TOKEN_URL, status_code=503)
    httpx_mock.add_response(url=_TOKEN_URL, json=_TOKEN)
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job(5))

    job = Client(**_KW).jobs.get(5)

    assert job.id == 5
    assert len(_token_requests(httpx_mock)) == 2


def test_token_transport_error_is_retried(httpx_mock):
    httpx_mock.add_exception(httpx.ConnectError("reset"), url=_TOKEN_URL)
    httpx_mock.add_response(url=_TOKEN_URL, json=_TOKEN)
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job(5))

    job = Client(**_KW).jobs.get(5)

    assert job.id == 5
    assert len(_token_requests(httpx_mock)) == 2


def test_token_bad_credentials_fail_fast(httpx_mock):
    # 401 from the token endpoint = wrong credentials. Retrying is a pointless
    # storm — exactly ONE request, immediate AuthenticationError.
    httpx_mock.add_response(url=_TOKEN_URL, status_code=401, json={"error": "access_denied"})

    with pytest.raises(AuthenticationError, match="401"):
        Client(**_KW).jobs.get(5)

    assert len(_token_requests(httpx_mock)) == 1


def test_token_exhaustion_raises(httpx_mock):
    # max_retries=2 -> 3 attempts, then AuthenticationError carrying the status.
    for _ in range(3):
        httpx_mock.add_response(url=_TOKEN_URL, status_code=503, text="down")

    with pytest.raises(AuthenticationError, match="503"):
        Client(**_KW).jobs.get(5)

    assert len(_token_requests(httpx_mock)) == 3


async def test_async_token_herd_is_single_flight(httpx_mock):
    # N concurrent tasks with no cached token must produce exactly ONE token
    # request (the asyncio lock + double-check). Only one token response is
    # registered: a herd would hit "no response registered" and fail loudly.
    httpx_mock.add_response(url=_TOKEN_URL, json=_TOKEN)
    for i in range(1, 6):
        httpx_mock.add_response(
            method="GET", url=f"https://api.test/api/jobs/{i}", json=_job(i)
        )

    client = AsyncClient(**_KW)
    jobs = await asyncio.gather(*(client.jobs.get(i) for i in range(1, 6)))

    assert sorted(j.id for j in jobs) == [1, 2, 3, 4, 5]
    assert len(_token_requests(httpx_mock)) == 1


def test_forced_refresh_dedupes_when_already_refreshed(monkeypatch):
    # The force path (401 recovery): a caller that waited on the lock while
    # someone ELSE refreshed must reuse the fresh token, not force another
    # fetch. Simulated by a lock whose acquisition swaps in a fresh token.
    auth = ClientCredentialsAuth(
        client_id="i", client_secret="s", token_url=_TOKEN_URL, audience="a"
    )
    auth._token = "stale-tok"
    auth._expires_at = time.monotonic() + 3600

    fetches: list[int] = []
    monkeypatch.setattr(auth, "_fetch_token", lambda: fetches.append(1))

    class RefreshingLock:
        def __enter__(self):  # another caller refreshed while we waited
            auth._token = "fresh-tok"
            auth._expires_at = time.monotonic() + 3600

        def __exit__(self, *args):
            return None

    auth._lock = RefreshingLock()

    token = auth._ensure_token(force=True)

    assert token == "fresh-tok"
    assert fetches == []  # no redundant forced fetch


def test_forced_refresh_still_fetches_when_token_unchanged(monkeypatch):
    # The counterpart: if nobody else refreshed, force must actually re-fetch
    # even though the cached token still looks valid (the server 401'd it).
    auth = ClientCredentialsAuth(
        client_id="i", client_secret="s", token_url=_TOKEN_URL, audience="a"
    )
    auth._token = "revoked-tok"
    auth._expires_at = time.monotonic() + 3600

    def fake_fetch():
        auth._token = "new-tok"
        auth._expires_at = time.monotonic() + 3600

    monkeypatch.setattr(auth, "_fetch_token", fake_fetch)

    assert auth._ensure_token(force=True) == "new-tok"
