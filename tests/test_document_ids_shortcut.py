"""The document_ids shortcut wraps a file selector across selector-taking methods."""

from __future__ import annotations

import json

import pytest

from infersoft import Client

_FILE_SEL = {"include": [{"type": "fileSelector", "files": [1, 2, 3]}], "exclude": []}


def _bare_client() -> Client:
    # No httpx_mock: these calls raise before any network request.
    return Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )
_PROJECT = {
    "id": 7,
    "organization_id": "org",
    "name": "P",
    "created_at": "2026-05-29T00:00:00Z",
}


def _body(httpx_mock, path):
    post = next(r for r in httpx_mock.get_requests() if r.url.path == path)
    return json.loads(post.content)


def test_bulk_delete_by_document_ids(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/bulk_delete",
        json={"matched": 3, "dry_run": False},
    )

    client.documents.bulk_delete(document_ids=[1, 2, 3])

    assert _body(httpx_mock, "/api/documents/bulk_delete")["selectors"] == _FILE_SEL


def test_move_to_folder_by_document_ids(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/move-to-folder",
        json={"matched": 3, "moved": 3, "skipped": 0},
    )

    client.documents.move_to_folder(document_ids=[1, 2, 3], target_folder_id=9)

    body = _body(httpx_mock, "/api/documents/move-to-folder")
    assert body == {"selectors": _FILE_SEL, "target_folder_id": 9}


def test_assign_documents_by_document_ids(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects/assign-documents",
        json={"project": _PROJECT, "matched": 3, "added": 3, "skipped": 0},
    )

    client.projects.assign_documents(document_ids=[1, 2, 3], project_id=7)

    assert _body(httpx_mock, "/api/projects/assign-documents")["selectors"] == _FILE_SEL


def test_jobs_run_by_document_ids(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/estimate",
        json={"total_credits": 10, "page_count": 3, "id": "cred-1"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/start",
        json={
            "id": 50,
            "organization_id": "org",
            "organization_name": "Org",
            "total_docs": 3,
            "completed_docs": 0,
            "error_count": 0,
            "status": "running",
            "createdAt": "2026-05-29T00:00:00Z",
        },
    )

    job = client.jobs.run(step="extractor", document_ids=[1, 2, 3], prompts=[5])

    assert job.id == 50
    est_body = _body(httpx_mock, "/api/jobs/credits/estimate")
    assert est_body["selectors"] == _FILE_SEL
    assert est_body["steps"] == ["extractor"] and est_body["prompts"] == [5]


def test_search_by_document_ids(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/search",
        json={"items": [], "page": 1, "page_size": 50, "has_more": False},
    )

    client.documents.search(document_ids=[1, 2, 3], prompts=[5])

    assert _body(httpx_mock, "/api/documents/search")["selectors"] == _FILE_SEL


def test_both_selectors_and_document_ids_is_rejected():
    with pytest.raises(ValueError, match="not both"):
        _bare_client().documents.bulk_delete({"include": []}, document_ids=[1])


def test_required_selector_missing_is_rejected():
    with pytest.raises(ValueError, match="selectors.*document_ids"):
        _bare_client().documents.move_to_folder(target_folder_id=9)
