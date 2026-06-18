"""AsyncClient parity — a representative slice mirroring the sync behaviors."""

from __future__ import annotations

import pytest

from infersoft import NotFoundError

_PROJECT = {
    "id": 7,
    "organization_id": "org",
    "name": "Q1",
    "created_at": "2026-05-29T00:00:00Z",
}


def _page(items, has_more=False):
    return {"items": items, "page": 1, "page_size": 50, "has_more": has_more}


def _job(status, jid=5):
    return {
        "id": jid,
        "organization_id": "org",
        "organization_name": "Org",
        "total_docs": 1,
        "completed_docs": 0,
        "error_count": 0,
        "status": status,
        "createdAt": "2026-05-29T00:00:00Z",
    }


async def test_async_get_uses_token_and_returns_model(async_client, httpx_mock):
    httpx_mock.add_response(method="GET", url="https://api.test/api/projects/7", json=_PROJECT)

    proj = await async_client.projects.get(7)

    assert proj.id == 7
    # The async auth flow fetched a token first.
    assert any(r.url.path == "/oauth/token" for r in httpx_mock.get_requests())


async def test_async_upload_puts_bytes_unauthenticated(async_client, httpx_mock, tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF async")
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": "invoice.pdf",
                    "document_id": 42,
                    "put_url": "https://s3.test/p",
                    "required_headers": {"Content-Type": "application/pdf"},
                }
            ]
        },
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    result = await async_client.documents.upload(doc)

    assert result.document_ids == [42]
    put = next(r for r in httpx_mock.get_requests() if r.method == "PUT")
    assert put.content == b"%PDF async"
    assert "authorization" not in {k.lower() for k in put.headers}


async def test_async_upload_sends_content_md5_for_dedup(async_client, httpx_mock, tmp_path):
    # Async parity with test_upload.test_upload_sends_content_md5_for_dedup.
    import hashlib
    import json

    body = b"%PDF-1.7 dedup-me"
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(body)
    expected_md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()

    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": "invoice.pdf",
                    "document_id": 42,
                    "put_url": "https://s3.test/presigned",
                    "required_headers": {"Content-Type": "application/pdf"},
                }
            ]
        },
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/presigned", status_code=200)

    await async_client.documents.upload(doc)

    post = next(
        r for r in httpx_mock.get_requests() if r.method == "POST" and r.url.path == "/api/uploads"
    )
    sent = json.loads(post.content)["files"][0]
    assert sent["md5"] == expected_md5


async def test_async_iterate_paginates(async_client, httpx_mock):
    p2 = {**_PROJECT, "id": 8}
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects/search", json=_page([_PROJECT], True)
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects/search", json=_page([p2])
    )

    ids = [p.id async for p in async_client.projects.iterate()]

    assert ids == [7, 8]


async def test_async_write_sends_idempotency_key(async_client, httpx_mock):
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)

    await async_client.projects.create("Q1")

    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/projects")
    assert len(post.headers["Idempotency-Key"]) == 32


async def test_async_error_mapping(async_client, httpx_mock):
    httpx_mock.add_response(
        method="GET",
        url="https://api.test/api/projects/9",
        status_code=404,
        json={"title": "Not Found"},
    )

    with pytest.raises(NotFoundError):
        await async_client.projects.get(9)


async def test_async_jobs_wait_polls_to_terminal(async_client, httpx_mock, monkeypatch):
    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr("infersoft._wait.asyncio.sleep", _no_sleep)
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job("running"))
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job("completed"))

    job = await async_client.jobs.wait(5)

    assert job.status.value == "completed"
    assert len([r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/5"]) == 2
