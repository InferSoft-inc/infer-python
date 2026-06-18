"""Presigned PUTs use one long-lived, unauthenticated, pooled upload client
(not a fresh client per file)."""

from __future__ import annotations

import httpx

_UPLOAD_RESP = {
    "items": [
        {
            "client_file_name": "a.pdf",
            "document_id": 1,
            "put_url": "https://s3.test/a",
            "required_headers": {},
        },
        {
            "client_file_name": "b.pdf",
            "document_id": 2,
            "put_url": "https://s3.test/b",
            "required_headers": {},
        },
    ]
}


def _stub_two_file_upload(httpx_mock):
    httpx_mock.add_response(method="POST", url="https://api.test/api/uploads", json=_UPLOAD_RESP)
    httpx_mock.add_response(method="PUT", url="https://s3.test/a", status_code=200)
    httpx_mock.add_response(method="PUT", url="https://s3.test/b", status_code=200)


def test_presigned_put_is_unauthenticated_and_pooled(client, httpx_mock, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF a")
    (tmp_path / "b.pdf").write_bytes(b"%PDF b")
    _stub_two_file_upload(httpx_mock)

    before = client._http._upload_client
    # A separate client from the authenticated API client.
    assert isinstance(before, httpx.Client) and before is not client._http._client
    client.documents.upload([tmp_path / "a.pdf", tmp_path / "b.pdf"])

    # The PUTs carry no bearer token; the API POST does.
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/uploads")
    puts = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert "authorization" in post.headers
    assert len(puts) == 2
    for put in puts:
        assert "authorization" not in put.headers

    # Same client instance throughout — not recreated per file.
    assert client._http._upload_client is before


def test_close_also_closes_the_upload_client(client, httpx_mock, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF a")
    (tmp_path / "b.pdf").write_bytes(b"%PDF b")
    _stub_two_file_upload(httpx_mock)
    client.documents.upload([tmp_path / "a.pdf", tmp_path / "b.pdf"])

    client.close()
    assert client._http._client.is_closed
    assert client._http._upload_client.is_closed


def test_copy_shares_and_does_not_close_the_upload_client(client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/projects/7",
        json={"id": 7, "organization_id": "o", "name": "P", "created_at": "2026-05-29T00:00:00Z"},
    )
    with client.with_options(timeout=60) as copy:
        # The copy reuses the original's upload client (no new pool).
        assert copy._http._upload_client is client._http._upload_client
        copy.projects.get(7)

    # The copy's __exit__ must not close the shared upload client.
    assert not client._http._upload_client.is_closed


async def test_async_aclose_also_closes_the_upload_client(async_client, httpx_mock, tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF a")
    (tmp_path / "b.pdf").write_bytes(b"%PDF b")
    _stub_two_file_upload(httpx_mock)
    await async_client.documents.upload([tmp_path / "a.pdf", tmp_path / "b.pdf"])

    assert isinstance(async_client._http._upload_client, httpx.AsyncClient)
    puts = [r for r in httpx_mock.get_requests() if r.method == "PUT"]
    assert len(puts) == 2 and all("authorization" not in p.headers for p in puts)

    await async_client.aclose()
    assert async_client._http._client.is_closed
    assert async_client._http._upload_client.is_closed
