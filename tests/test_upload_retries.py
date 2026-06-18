"""Presigned S3 PUT retries: transient failures retried, terminal 4xx fail fast,
budget configurable via upload_max_retries (falling back to max_retries)."""

from __future__ import annotations

import httpx
import pytest

from infersoft import UploadTransferError

_PLAN = {
    "items": [
        {
            "client_file_name": "a.pdf",
            "document_id": 1,
            "put_url": "https://s3.test/p",
            "required_headers": {},
        }
    ]
}


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("infersoft._http.time.sleep", lambda _s: None)

    async def _nap(_s):
        return None

    monkeypatch.setattr("infersoft._http.asyncio.sleep", _nap)


@pytest.fixture
def doc(tmp_path):
    f = tmp_path / "a.pdf"
    f.write_bytes(b"%PDF bytes")
    return f


def _stub_plan(httpx_mock):
    httpx_mock.add_response(method="POST", url="https://api.test/api/uploads", json=_PLAN)


def _puts(httpx_mock):
    return [r for r in httpx_mock.get_requests() if r.method == "PUT"]


def test_transient_503_is_retried(client, httpx_mock, doc):
    _stub_plan(httpx_mock)
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=503)
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    result = client.documents.upload(doc)

    assert result.outcomes[0].uploaded
    assert len(_puts(httpx_mock)) == 2


def test_transport_error_is_retried(client, httpx_mock, doc):
    _stub_plan(httpx_mock)
    httpx_mock.add_exception(
        httpx.ConnectError("reset"), method="PUT", url="https://s3.test/p"
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    result = client.documents.upload(doc)

    assert result.outcomes[0].uploaded
    assert len(_puts(httpx_mock)) == 2


def test_exhaustion_raises_with_status(client, httpx_mock, doc):
    # Default max_retries=2 -> 3 attempts total, then UploadTransferError.
    _stub_plan(httpx_mock)
    for _ in range(3):
        httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=503)

    with pytest.raises(UploadTransferError) as exc_info:
        client.documents.upload(doc)

    assert exc_info.value.status_code == 503
    assert len(_puts(httpx_mock)) == 3


def test_terminal_403_fails_fast(client, httpx_mock, doc):
    # An expired/invalid presigned URL is S3 403 — retrying a dead URL is
    # waste; the recovery is re-planning, so exactly ONE request goes out.
    _stub_plan(httpx_mock)
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=403)

    with pytest.raises(UploadTransferError) as exc_info:
        client.documents.upload(doc)

    assert exc_info.value.status_code == 403
    assert len(_puts(httpx_mock)) == 1


def test_upload_max_retries_overrides_budget(client, httpx_mock, doc):
    # A dedicated PUT budget above the API default (2): 4 failures then
    # success only completes when upload_max_retries raises the ceiling.
    _stub_plan(httpx_mock)
    for _ in range(4):
        httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=503)
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    result = client.with_options(upload_max_retries=4).documents.upload(doc)

    assert result.outcomes[0].uploaded
    assert len(_puts(httpx_mock)) == 5


def test_upload_max_retries_zero_fails_fast_without_touching_api_budget(
    client, httpx_mock, doc
):
    # upload_max_retries=0 disables PUT retries only; the plan POST (API path,
    # governed by max_retries) still retries its own transient failure.
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/uploads", status_code=503, json={"title": "x"}
    )
    httpx_mock.add_response(method="POST", url="https://api.test/api/uploads", json=_PLAN)
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=503)

    with pytest.raises(UploadTransferError):
        client.with_options(upload_max_retries=0).documents.upload(doc)

    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/uploads"]
    assert len(posts) == 2  # API retry budget untouched
    assert len(_puts(httpx_mock)) == 1  # PUT budget is zero


async def test_async_transient_then_success(async_client, httpx_mock, doc):
    _stub_plan(httpx_mock)
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=503)
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    result = await async_client.documents.upload(doc)

    assert result.outcomes[0].uploaded
    assert len(_puts(httpx_mock)) == 2


async def test_async_exhaustion_raises(async_client, httpx_mock, doc):
    _stub_plan(httpx_mock)
    for _ in range(3):
        httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=503)

    with pytest.raises(UploadTransferError) as exc_info:
        await async_client.documents.upload(doc)

    assert exc_info.value.status_code == 503
    assert len(_puts(httpx_mock)) == 3
