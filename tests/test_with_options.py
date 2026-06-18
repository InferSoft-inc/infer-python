"""Per-request overrides via client.with_options(timeout=, max_retries=)."""

from __future__ import annotations

import pytest

from infersoft import ServerError

_PROJECT = {
    "id": 7,
    "organization_id": "org",
    "name": "P",
    "created_at": "2026-05-29T00:00:00Z",
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("infersoft._http.time.sleep", lambda _s: None)


def test_timeout_override_reaches_the_wire(client, httpx_mock):
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)

    client.with_options(timeout=99.0).projects.get(7)
    client.projects.get(7)  # original keeps the client default

    first, second = (r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/7")
    assert first.extensions["timeout"]["read"] == 99.0
    assert second.extensions["timeout"]["read"] == 30.0


def test_max_retries_zero_fails_fast_without_affecting_original(client, httpx_mock):
    # Copy with retries disabled: a single 503 must surface immediately.
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/projects/7", status_code=503, json={"title": "x"}
    )
    with pytest.raises(ServerError):
        client.with_options(max_retries=0).projects.get(7)
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/7"]) == 1

    # The original client still retries (503 then 200).
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/projects/7", status_code=503, json={"title": "x"}
    )
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)
    assert client.projects.get(7).id == 7


def test_copy_shares_connection_and_token_cache(client, httpx_mock):
    # Only ONE token response is registered (in the fixture); if the copy didn't
    # share the token cache, its call would need a second fetch and fail.
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)

    client.projects.get(7)
    client.with_options(timeout=60).projects.get(7)

    tokens = [r for r in httpx_mock.get_requests() if r.url.path == "/oauth/token"]
    assert len(tokens) == 1


def test_copy_as_context_manager_does_not_close_original(client, httpx_mock):
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)

    with client.with_options(timeout=300) as copy:
        copy.projects.get(7)

    # The shared pool must survive the copy's __exit__; only the original owns it.
    assert client.projects.get(7).id == 7
    client.close()
    assert client._http._client.is_closed


def test_max_retries_can_be_raised_above_the_default(client, httpx_mock):
    # Four 503s then success: only with max_retries >= 4 can this complete.
    for _ in range(4):
        httpx_mock.add_response(
            method="GET",
            url="https://api.test/api/projects/7",
            status_code=503,
            json={"title": "x"},
        )
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)

    assert client.with_options(max_retries=5).projects.get(7).id == 7
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/7"]) == 5


def test_combined_timeout_and_max_retries_both_apply(client, httpx_mock):
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/projects/7", status_code=503, json={"title": "x"}
    )

    with pytest.raises(ServerError):
        client.with_options(timeout=120.0, max_retries=0).projects.get(7)

    gets = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/7"]
    assert len(gets) == 1  # max_retries=0 applied
    assert gets[0].extensions["timeout"]["read"] == 120.0  # timeout applied too


def test_timeout_override_does_not_touch_presigned_put(client, httpx_mock, tmp_path):
    doc = tmp_path / "big.pdf"
    doc.write_bytes(b"%PDF big")
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": "big.pdf",
                    "document_id": 1,
                    "put_url": "https://s3.test/p",
                    "required_headers": {},
                }
            ]
        },
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    client.with_options(timeout=10.0).documents.upload(doc)

    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/uploads")
    put = next(r for r in httpx_mock.get_requests() if r.method == "PUT")
    assert post.extensions["timeout"]["read"] == 10.0  # API call gets the override
    assert put.extensions["timeout"]["read"] == 300.0  # presigned PUT keeps upload_timeout


async def test_async_with_options_timeout_and_fail_fast(async_client, httpx_mock, monkeypatch):
    async def _nap(_s):
        return None

    monkeypatch.setattr("infersoft._http.asyncio.sleep", _nap)
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)
    await async_client.with_options(timeout=77.0).projects.get(7)
    get = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/7")
    assert get.extensions["timeout"]["read"] == 77.0

    httpx_mock.add_response(
        method="GET", url="https://api.test/api/projects/8", status_code=503, json={"title": "x"}
    )
    with pytest.raises(ServerError):
        await async_client.with_options(max_retries=0).projects.get(8)
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/8"]) == 1


async def test_async_copy_leaves_original_untouched(async_client, httpx_mock):
    # Guard against a mutate-in-place regression in AsyncHttpClient.copy_with:
    # after using a copy, the ORIGINAL must keep its default timeout and retries.
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)
    await async_client.with_options(timeout=77.0, max_retries=0).projects.get(7)

    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/9", json=_PROJECT)
    await async_client.projects.get(9)
    original_get = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/9")
    assert original_get.extensions["timeout"]["read"] == 30.0
    assert async_client._http._max_retries == 2
