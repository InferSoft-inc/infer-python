"""Phase 2: projects.assign_documents."""

from __future__ import annotations

import json

_PROJECT = {
    "id": 7,
    "organization_id": "org",
    "name": "Q1 Invoices",
    "created_at": "2026-05-29T00:00:00Z",
}


def test_assign_documents_to_existing_project(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects/assign-documents",
        json={"project": _PROJECT, "matched": 6, "added": 5, "skipped": 1},
    )

    sel = {"include": [{"type": "tagSelector", "tags": [3]}]}
    result = client.projects.assign_documents(sel, project_id=7)

    assert result.project.id == 7
    assert (result.matched, result.added, result.skipped) == (6, 5, 1)
    post = next(
        r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/assign-documents"
    )
    body = json.loads(post.content)
    assert body == {"selectors": sel, "project_id": 7}
    assert post.headers["Idempotency-Key"]  # write carries a key


def test_assign_documents_creating_new_project(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects/assign-documents",
        json={"project": _PROJECT, "matched": 2, "added": 2, "skipped": 0},
    )

    client.projects.assign_documents({"include": []}, project_name="Q1 Invoices")

    post = next(
        r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/assign-documents"
    )
    body = json.loads(post.content)
    assert body["project_name"] == "Q1 Invoices"
    assert "project_id" not in body
