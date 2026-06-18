"""documents.upload_many(): chunked bulk upload."""

from __future__ import annotations

import json

import pytest

from infersoft import BadRequestError, Client, UploadManyError

_PROJECT = {
    "id": 7,
    "organization_id": "org",
    "name": "Bulk",
    "created_at": "2026-05-29T00:00:00Z",
}


def _make_files(tmp_path, count: int) -> list:
    paths = []
    for i in range(count):
        p = tmp_path / f"f{i}.pdf"
        p.write_bytes(b"%PDF " + str(i).encode())
        paths.append(p)
    return paths


def _upload_response(names: list[str], first_id: int, project: dict | None = None) -> dict:
    body: dict = {
        "items": [
            {
                "client_file_name": name,
                "document_id": first_id + i,
                "put_url": f"https://s3.test/{first_id + i}",
                "required_headers": {},
            }
            for i, name in enumerate(names)
        ]
    }
    if project is not None:
        body["project"] = project
    return body


def _mock_batch(httpx_mock, names: list[str], first_id: int, project: dict | None = None):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json=_upload_response(names, first_id, project),
    )
    for i in range(len(names)):
        httpx_mock.add_response(
            method="PUT", url=f"https://s3.test/{first_id + i}", status_code=200
        )


def test_upload_many_chunks_and_derives_idempotency_keys(client, httpx_mock, tmp_path):
    paths = _make_files(tmp_path, 5)
    _mock_batch(httpx_mock, ["f0.pdf", "f1.pdf"], 10)
    _mock_batch(httpx_mock, ["f2.pdf", "f3.pdf"], 12)
    _mock_batch(httpx_mock, ["f4.pdf"], 14)

    result = client.documents.upload_many(paths, batch_size=2, idempotency_key="bulk-key")

    assert result.document_ids == [10, 11, 12, 13, 14]
    assert len(result.succeeded) == 5

    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/uploads"]
    assert [len(json.loads(p.content)["files"]) for p in posts] == [2, 2, 1]
    assert [p.headers["Idempotency-Key"] for p in posts] == [
        "bulk-key-0000",
        "bulk-key-0001",
        "bulk-key-0002",
    ]


def test_upload_many_threads_created_project_across_batches(client, httpx_mock, tmp_path):
    paths = _make_files(tmp_path, 2)
    _mock_batch(httpx_mock, ["f0.pdf"], 10, project=_PROJECT)
    _mock_batch(httpx_mock, ["f1.pdf"], 11, project=_PROJECT)

    result = client.documents.upload_many(paths, batch_size=1, project_name="Bulk")

    assert result.project is not None and result.project.id == 7
    bodies = [
        json.loads(r.content) for r in httpx_mock.get_requests() if r.url.path == "/api/uploads"
    ]
    assert bodies[0].get("project_name") == "Bulk" and "project_id" not in bodies[0]
    # Batch 2 is pinned to the created project's id — no duplicate project.
    assert bodies[1].get("project_id") == 7 and "project_name" not in bodies[1]


def test_upload_many_validates_duplicate_basenames_across_batches(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    pa = tmp_path / "a" / "same.pdf"
    pb = tmp_path / "b" / "same.pdf"
    pa.write_bytes(b"x")
    pb.write_bytes(b"y")
    bare = Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )
    # Even though batch_size=1 would put them in different batches, the
    # duplicate check runs across the WHOLE set before any HTTP.
    with pytest.raises(ValueError, match="same.pdf"):
        bare.documents.upload_many([pa, pb], batch_size=1)


def test_upload_many_directory_skips_hidden_and_keeps_layout(client, httpx_mock, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.pdf").write_bytes(b"a")
    (tmp_path / "sub" / "b.pdf").write_bytes(b"b")
    (tmp_path / ".DS_Store").write_bytes(b"junk")
    _mock_batch(httpx_mock, ["a.pdf", "sub/b.pdf"], 10)

    client.documents.upload_many(tmp_path, flatten=False)

    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/uploads")
    sent = [f["file_name"] for f in json.loads(post.content)["files"]]
    assert sent == ["a.pdf", "sub/b.pdf"]  # sorted, hidden file skipped


def test_upload_many_failure_carries_partial_state(client, httpx_mock, tmp_path):
    paths = _make_files(tmp_path, 2)
    _mock_batch(httpx_mock, ["f0.pdf"], 10)
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        status_code=400,
        json={"title": "Bad Request", "detail": "boom"},
    )

    with pytest.raises(UploadManyError) as exc_info:
        client.documents.upload_many(paths, batch_size=1)

    err = exc_info.value
    assert err.batches_completed == 1
    assert err.partial.document_ids == [10]  # batch 1 is not rolled back
    assert isinstance(err.__cause__, BadRequestError)


def test_upload_many_on_batch_callback_and_wait(client, httpx_mock, tmp_path):
    paths = _make_files(tmp_path, 2)
    _mock_batch(httpx_mock, ["f0.pdf"], 10)
    _mock_batch(httpx_mock, ["f1.pdf"], 11)
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/search",
        json={
            "items": [
                {
                    "id": did,
                    "organization_id": "org",
                    "name": f"f{i}.pdf",
                    "status": "ready",
                    "is_valid": True,
                    "created_at": "2026-05-29T00:00:00Z",
                    "has_active_workflow": False,
                }
                for i, did in enumerate([10, 11])
            ],
            "page": 1,
            "page_size": 50,
            "has_more": False,
        },
    )

    seen: list[list[int]] = []
    result = client.documents.upload_many(
        paths, batch_size=1, on_batch=lambda r: seen.append(r.document_ids), wait=True
    )

    assert seen == [[10], [11]]
    # The readiness wait ran once, over all aggregated ids.
    search = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/search")
    assert json.loads(search.content)["selectors"]["include"] == [
        {"type": "fileSelector", "files": [10, 11]}
    ]
    assert result.document_ids == [10, 11]


def test_upload_many_rejects_bad_batch_size(tmp_path):
    p = tmp_path / "a.pdf"
    p.write_bytes(b"x")
    bare = Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )
    with pytest.raises(ValueError, match="batch_size"):
        bare.documents.upload_many([p], batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        bare.documents.upload_many([p], batch_size=101)


async def test_async_upload_many_chunks(async_client, httpx_mock, tmp_path):
    paths = _make_files(tmp_path, 3)
    _mock_batch(httpx_mock, ["f0.pdf", "f1.pdf"], 10)
    _mock_batch(httpx_mock, ["f2.pdf"], 12)

    result = await async_client.documents.upload_many(paths, batch_size=2)

    assert result.document_ids == [10, 11, 12]
    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/uploads"]
    assert len(posts) == 2