"""Smoke test for the documents.upload workflow using a mocked HTTP transport."""

from __future__ import annotations

import json

import pytest

from infersoft import MAX_BATCH_FILES, Client


def test_upload_puts_bytes_to_presigned_url(client, httpx_mock, tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.7 fake")

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
            ],
            "project": {
                "id": 7,
                "organization_id": "org_1",
                "name": "Q1 Invoices",
                "created_at": "2026-05-29T00:00:00Z",
            },
        },
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/presigned", status_code=200)

    result = client.documents.upload(doc, project_name="Q1 Invoices")

    assert result.document_ids == [42]
    assert result.succeeded[0].uploaded is True
    assert result.project is not None and result.project.id == 7

    put = next(r for r in httpx_mock.get_requests() if r.method == "PUT")
    assert put.headers["Content-Type"] == "application/pdf"
    assert put.content == b"%PDF-1.7 fake"
    assert "authorization" not in {k.lower() for k in put.headers}


def test_upload_skips_name_conflict_without_put_url(client, httpx_mock, tmp_path):
    doc = tmp_path / "dup.pdf"
    doc.write_bytes(b"data")

    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": "dup.pdf",
                    "document_id": 99,
                    "error": {"code": "NAME_ALREADY_EXISTS", "message": "exists"},
                }
            ]
        },
    )

    result = client.documents.upload(doc)

    assert result.document_ids == [99]
    assert result.failed[0].uploaded is False
    assert result.failed[0].error is not None
    assert result.failed[0].error.code == "NAME_ALREADY_EXISTS"
    assert all(r.method != "PUT" for r in httpx_mock.get_requests())


def test_upload_flatten_rejects_duplicate_basenames(tmp_path):
    # Default flatten=True sends bare basenames, so two files that share one
    # would collide. The SDK rejects the batch before any network call.
    (tmp_path / "jan").mkdir()
    (tmp_path / "feb").mkdir()
    a = tmp_path / "jan" / "invoice.pdf"
    b = tmp_path / "feb" / "invoice.pdf"
    a.write_bytes(b"jan-bytes")
    b.write_bytes(b"feb-bytes")

    bare_client = Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )
    with pytest.raises(ValueError, match="invoice.pdf"):
        bare_client.documents.upload([a, b])


def test_upload_unflattened_sends_path_verbatim(client, httpx_mock, tmp_path):
    # flatten=False replicates the caller's exact path as the file name, so the
    # app mirrors the local folder layout.
    sub = tmp_path / "2024" / "jan"
    sub.mkdir(parents=True)
    doc = sub / "invoice.pdf"
    doc.write_bytes(b"data")
    full = str(doc)

    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": full,
                    "document_id": 5,
                    "put_url": "https://s3.test/x",
                    "required_headers": {"Content-Type": "application/pdf"},
                }
            ]
        },
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/x", status_code=200)

    result = client.documents.upload(doc, flatten=False)

    assert result.document_ids == [5]
    assert result.succeeded[0].uploaded is True

    post = next(
        r for r in httpx_mock.get_requests() if r.method == "POST" and r.url.path == "/api/uploads"
    )
    sent = json.loads(post.content)["files"][0]["file_name"]
    assert sent == full


def test_upload_rejects_more_than_max_batch_files(tmp_path):
    # The caller is responsible for chunking; the SDK rejects oversized batches
    # before making any network call.
    paths = []
    for i in range(MAX_BATCH_FILES + 1):
        p = tmp_path / f"f{i}.pdf"
        p.write_bytes(b"x")
        paths.append(p)

    bare_client = Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )
    with pytest.raises(ValueError, match=f"at most {MAX_BATCH_FILES} files"):
        bare_client.documents.upload(paths)


def test_upload_sends_content_md5_for_dedup(client, httpx_mock, tmp_path):
    import hashlib

    body = b"%PDF-1.7 dedup-me"
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(body)
    expected_md5 = hashlib.md5(body, usedforsecurity=False).hexdigest()  # lowercase hex, verbatim

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

    client.documents.upload(doc)

    post = next(
        r for r in httpx_mock.get_requests() if r.method == "POST" and r.url.path == "/api/uploads"
    )
    sent = json.loads(post.content)["files"][0]
    assert sent["md5"] == expected_md5


# --- on_duplicate (allow / notify / block) ------------------------------------

_DUP_ITEM = {
    "client_file_name": "invoice.pdf",
    "document_id": 99,
    "put_url": "https://s3.test/p",
    "required_headers": {"Content-Type": "application/pdf"},
    "duplicates": [{"document_id": 42, "name": "orig.pdf", "created_at": "2026-06-15T00:00:00Z"}],
}


def _dup_doc(tmp_path):
    doc = tmp_path / "invoice.pdf"
    doc.write_bytes(b"%PDF-1.7 dup-me")
    return doc


def test_upload_on_duplicate_allow_uploads_and_surfaces(client, httpx_mock, tmp_path):
    doc = _dup_doc(tmp_path)
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/uploads", json={"items": [_DUP_ITEM]}
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    result = client.documents.upload(doc)  # default "allow"

    o = result.outcomes[0]
    assert o.uploaded is True and o.skipped_as_duplicate is False
    assert [d.document_id for d in o.duplicates] == [42]  # match surfaced regardless of mode
    assert any(r.method == "PUT" for r in httpx_mock.get_requests())


def test_upload_on_duplicate_block_skips_and_deletes_placeholder(client, httpx_mock, tmp_path):
    doc = _dup_doc(tmp_path)
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/uploads", json={"items": [_DUP_ITEM]}
    )
    httpx_mock.add_response(
        method="DELETE", url="https://api.test/api/documents/99", status_code=204
    )

    result = client.documents.upload(doc, on_duplicate="block")

    o = result.outcomes[0]
    assert o.skipped_as_duplicate is True and o.uploaded is False
    assert [d.document_id for d in o.duplicates] == [42]
    assert result.skipped == [o] and result.failed == []
    reqs = httpx_mock.get_requests()
    assert not any(r.method == "PUT" for r in reqs), "block must not upload the bytes"
    assert any(r.method == "DELETE" and r.url.path == "/api/documents/99" for r in reqs)


def test_upload_on_duplicate_notify_uploads_and_warns(client, httpx_mock, tmp_path, caplog):
    import logging

    doc = _dup_doc(tmp_path)
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/uploads", json={"items": [_DUP_ITEM]}
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)

    with caplog.at_level(logging.WARNING, logger="infersoft"):
        result = client.documents.upload(doc, on_duplicate="notify")

    o = result.outcomes[0]
    assert o.uploaded is True
    assert [d.document_id for d in o.duplicates] == [42]
    assert "matches existing document" in caplog.text
