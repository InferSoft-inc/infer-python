"""documents.download_url + the download composite."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from infersoft import DownloadTransferError

_SIGNED_URL = "https://signed.test/f?sig=abc"
_DL = {"url": _SIGNED_URL, "expires_at": "2026-06-11T12:00:00Z"}


def _doc(name: str) -> dict:
    return {
        "id": 42,
        "organization_id": "org",
        "name": name,
        "status": "ready",
        "is_valid": True,
        "created_at": "2026-05-29T00:00:00Z",
        "has_active_workflow": False,
    }


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("infersoft._http.time.sleep", lambda _s: None)

    async def _nap(_s):
        return None

    monkeypatch.setattr("infersoft._http.asyncio.sleep", _nap)


def _stub_endpoint(httpx_mock):
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/documents/42/download", json=_DL
    )


def test_download_url_parses_model(client, httpx_mock):
    _stub_endpoint(httpx_mock)

    resp = client.documents.download_url(42)

    assert resp.url == _SIGNED_URL
    assert resp.expires_at == datetime(2026, 6, 11, 12, 0, tzinfo=timezone.utc)


def test_download_happy_path_uses_document_name(client, httpx_mock, tmp_path):
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/documents/42", json=_doc("invoice.pdf")
    )
    _stub_endpoint(httpx_mock)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, content=b"%PDF data")

    path = client.documents.download(42, tmp_path)

    assert path == tmp_path / "invoice.pdf"
    assert path.read_bytes() == b"%PDF data"
    # The signed fetch must be unauthenticated: the URL carries its own
    # signature and a bearer token would break it.
    signed = next(r for r in httpx_mock.get_requests() if r.url.host == "signed.test")
    assert "authorization" not in signed.headers


def test_download_file_name_override_skips_get(client, httpx_mock, tmp_path):
    _stub_endpoint(httpx_mock)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, content=b"bytes")

    path = client.documents.download(42, tmp_path, file_name="custom.pdf")

    assert path == tmp_path / "custom.pdf"
    # No GET /api/documents/42 — the override skipped the metadata round-trip.
    api_gets = [r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/42"]
    assert api_gets == []


@pytest.mark.parametrize("name", ["../escape.pdf", "a/b/c.pdf", "deep/../../x.pdf"])
def test_download_neutralizes_path_separators(client, httpx_mock, tmp_path, name):
    # flatten=False uploads mean server names can carry paths; only the final
    # component is used and the file must stay inside dest_dir.
    _stub_endpoint(httpx_mock)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, content=b"bytes")

    path = client.documents.download(42, tmp_path, file_name=name)

    assert path.parent == tmp_path
    assert path.name in {"escape.pdf", "c.pdf", "x.pdf"}
    assert path.read_bytes() == b"bytes"


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("name", ["..", ".", "/"])
def test_download_rejects_nameless_inputs(client, tmp_path, name):
    # Inputs with no usable final component must fail before any request.
    with pytest.raises(ValueError):
        client.documents.download(42, tmp_path, file_name=name)


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_download_dest_dir_must_exist(client, tmp_path):
    with pytest.raises(NotADirectoryError):
        client.documents.download(42, tmp_path / "missing")


def test_download_overwrite_semantics(client, httpx_mock, tmp_path):
    existing = tmp_path / "f.pdf"
    existing.write_bytes(b"old")

    # Default: refuse to clobber — and fail BEFORE any download request.
    with pytest.raises(FileExistsError):
        client.documents.download(42, tmp_path, file_name="f.pdf")

    # overwrite=True replaces the content.
    _stub_endpoint(httpx_mock)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, content=b"new")
    path = client.documents.download(42, tmp_path, file_name="f.pdf", overwrite=True)
    assert path.read_bytes() == b"new"


def test_download_transient_503_is_retried(client, httpx_mock, tmp_path):
    _stub_endpoint(httpx_mock)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, status_code=503)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, content=b"bytes")

    path = client.documents.download(42, tmp_path, file_name="f.pdf")

    assert path.read_bytes() == b"bytes"
    signed = [r for r in httpx_mock.get_requests() if r.url.host == "signed.test"]
    assert len(signed) == 2


def test_download_exhaustion_raises(client, httpx_mock, tmp_path):
    _stub_endpoint(httpx_mock)
    for _ in range(3):  # default budget: 2 retries -> 3 attempts
        httpx_mock.add_response(method="GET", url=_SIGNED_URL, status_code=503)

    with pytest.raises(DownloadTransferError) as exc_info:
        client.documents.download(42, tmp_path, file_name="f.pdf")

    assert exc_info.value.status_code == 503


def test_download_expired_url_403_fails_fast(client, httpx_mock, tmp_path):
    # An expired signed URL cannot be fixed by retrying — exactly one request;
    # the recovery is a fresh download_url call.
    _stub_endpoint(httpx_mock)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, status_code=403)

    with pytest.raises(DownloadTransferError) as exc_info:
        client.documents.download(42, tmp_path, file_name="f.pdf")

    assert exc_info.value.status_code == 403
    signed = [r for r in httpx_mock.get_requests() if r.url.host == "signed.test"]
    assert len(signed) == 1


async def test_async_download_happy_path(async_client, httpx_mock, tmp_path):
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/documents/42", json=_doc("invoice.pdf")
    )
    _stub_endpoint(httpx_mock)
    httpx_mock.add_response(method="GET", url=_SIGNED_URL, content=b"%PDF data")

    path = await async_client.documents.download(42, tmp_path)

    assert path == tmp_path / "invoice.pdf"
    assert path.read_bytes() == b"%PDF data"
    signed = next(r for r in httpx_mock.get_requests() if r.url.host == "signed.test")
    assert "authorization" not in signed.headers


async def test_async_download_retry_and_exhaustion(async_client, httpx_mock, tmp_path):
    _stub_endpoint(httpx_mock)
    for _ in range(3):
        httpx_mock.add_response(method="GET", url=_SIGNED_URL, status_code=503)

    with pytest.raises(DownloadTransferError):
        await async_client.documents.download(42, tmp_path, file_name="f.pdf")

    signed = [r for r in httpx_mock.get_requests() if r.url.host == "signed.test"]
    assert len(signed) == 3
