"""Tier-1 composites: max_credits, ensure_path, get_or_create, jobs.results, get_values."""

from __future__ import annotations

import json

import pytest

from infersoft import Client, CreditsLimitExceededError, DocumentSummary

_PROJECT = {
    "id": 7,
    "organization_id": "org",
    "name": "Q1 Invoices",
    "created_at": "2026-05-29T00:00:00Z",
}


def _folder(fid: int, name: str, parent_id=None) -> dict:
    return {
        "id": fid,
        "organization_id": "org",
        "name": name,
        "parent_id": parent_id,
        "created_at": "2026-05-29T00:00:00Z",
        "updated_at": "2026-05-29T00:00:00Z",
        "has_children": False,
    }


def _doc_with_extractions(did: int, items: list[dict]) -> dict:
    return {
        "id": did,
        "organization_id": "org",
        "name": f"doc{did}.pdf",
        "status": "ready",
        "is_valid": True,
        "created_at": "2026-05-29T00:00:00Z",
        "has_active_workflow": False,
        "extraction_results": items,
    }


def _item(prompt_id: int, name: str | None, parsed) -> dict:
    return {"prompt_id": prompt_id, "value": {"name": name, "parsed_value": parsed}}


def _page(items: list[dict]) -> dict:
    return {"items": items, "page": 1, "page_size": 50, "has_more": False}


def _project_page(items: list[dict]) -> dict:
    return {"items": items, "page": 1, "page_size": 50, "has_more": False}


# ----- jobs.run(max_credits=...) -------------------------------------------------


def test_max_credits_blocks_start_and_carries_estimate(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/estimate",
        json={"total_credits": 100, "page_count": 40, "id": "cred-1"},
    )

    with pytest.raises(CreditsLimitExceededError) as exc_info:
        client.jobs.run(step="extractor", document_ids=[1], prompts=[5], max_credits=50)

    err = exc_info.value
    assert err.estimate.total_credits == 100 and err.max_credits == 50
    assert err.estimate.id == "cred-1"  # caller can still jobs.start() explicitly
    assert all(r.url.path != "/api/jobs/start" for r in httpx_mock.get_requests())


def test_max_credits_allows_start_within_budget(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/estimate",
        json={"total_credits": 100, "page_count": 40, "id": "cred-1"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/start",
        json={
            "id": 9,
            "organization_id": "org",
            "organization_name": "Org",
            "total_docs": 1,
            "completed_docs": 0,
            "error_count": 0,
            "status": "running",
            "createdAt": "2026-05-29T00:00:00Z",
        },
    )

    job = client.jobs.run(step="extractor", document_ids=[1], prompts=[5], max_credits=100)
    assert job.id == 9


# ----- folders.ensure_path -------------------------------------------------------


def test_ensure_path_returns_leaf_folder(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/folders/paths",
        json={"results": [{"folders": [_folder(1, "2026"), _folder(2, "Q1", parent_id=1)]}]},
    )

    leaf = client.folders.ensure_path("2026/Q1")

    assert leaf.id == 2 and leaf.name == "Q1"
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/folders/paths")
    assert json.loads(post.content) == {"paths": ["2026/Q1"]}


def test_ensure_path_rejects_empty_path():
    bare = Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )
    with pytest.raises(ValueError):
        bare.folders.ensure_path("  / ")


# ----- projects.get_or_create ----------------------------------------------------


def test_get_or_create_returns_existing_exact_match(client, httpx_mock):
    fuzzy_hit = {**_PROJECT, "id": 8, "name": "Q1 Invoices (old)"}
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects/search",
        json=_project_page([fuzzy_hit, _PROJECT]),
    )

    project = client.projects.get_or_create("Q1 Invoices")

    assert project.id == 7  # the exact match, not the fuzzy one
    assert all(r.url.path != "/api/projects" for r in httpx_mock.get_requests())


def test_get_or_create_creates_when_absent(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects/search", json=_project_page([])
    )
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)

    project = client.projects.get_or_create("Q1 Invoices")
    assert project.id == 7


def test_get_or_create_resolves_creation_race(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects/search", json=_project_page([])
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects",
        status_code=409,
        json={"title": "Conflict", "detail": "name exists"},
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects/search", json=_project_page([_PROJECT])
    )

    project = client.projects.get_or_create("Q1 Invoices")
    assert project.id == 7


# ----- jobs.results --------------------------------------------------------------


def test_jobs_results_correlates_documents_via_job_selector(client, httpx_mock):
    doc = _doc_with_extractions(1, [_item(5, "Total", 1234.5)])
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([doc])
    )

    docs = list(client.jobs.results(42, prompts=[5]))

    assert [d.id for d in docs] == [1]
    assert docs[0].extractions[5].parsed_value == 1234.5
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/search")
    body = json.loads(post.content)
    assert body["selectors"] == {"include": [{"type": "jobSelector", "jobs": [42]}], "exclude": []}
    assert body["prompts"] == [5]


# ----- extractions accessor + get_values -----------------------------------------


def test_extractions_accessor_keys_by_prompt_id():
    doc = DocumentSummary.model_validate(
        _doc_with_extractions(1, [_item(5, "Total", 1234.5), _item(6, "Vendor", "ACME")])
    )
    assert doc.extractions[5].parsed_value == 1234.5
    assert doc.extractions[6].name == "Vendor"

    bare = DocumentSummary.model_validate(
        {k: v for k, v in _doc_with_extractions(2, []).items() if k != "extraction_results"}
    )
    assert bare.extractions == {}


def test_get_values_flattens_by_name(client, httpx_mock):
    docs = [
        _doc_with_extractions(1, [_item(5, "Total", 1234.5), _item(6, "Vendor", "ACME")]),
        _doc_with_extractions(2, [_item(5, "Total", 9.0), _item(6, None, True)]),
    ]
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page(docs)
    )

    values = client.documents.get_values(document_ids=[1, 2], prompts=[5, 6])

    assert values == {
        1: {"Total": 1234.5, "Vendor": "ACME"},
        2: {"Total": 9.0, 6: True},  # unnamed item falls back to prompt_id
    }


def test_get_values_key_by_prompt_id_and_collision_raises(client, httpx_mock):
    collide = _doc_with_extractions(1, [_item(5, "Total", 1.0), _item(6, "Total", 2.0)])
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([collide])
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([collide])
    )

    with pytest.raises(ValueError, match="prompt_id"):
        client.documents.get_values(document_ids=[1], prompts=[5, 6])

    values = client.documents.get_values(document_ids=[1], prompts=[5, 6], key_by="prompt_id")
    assert values == {1: {5: 1.0, 6: 2.0}}


# ----- async parity (representative slice) ---------------------------------------


async def test_async_jobs_results_and_max_credits(async_client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/estimate",
        json={"total_credits": 100, "page_count": 40, "id": "cred-1"},
    )
    with pytest.raises(CreditsLimitExceededError):
        await async_client.jobs.run(
            step="extractor", document_ids=[1], prompts=[5], max_credits=50
        )

    doc = _doc_with_extractions(1, [_item(5, "Total", 1234.5)])
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([doc])
    )
    docs = [d async for d in async_client.jobs.results(42, prompts=[5])]
    assert docs[0].extractions[5].parsed_value == 1234.5


async def test_async_get_or_create_and_get_values(async_client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/projects/search", json=_project_page([])
    )
    httpx_mock.add_response(method="POST", url="https://api.test/api/projects", json=_PROJECT)
    project = await async_client.projects.get_or_create("Q1 Invoices")
    assert project.id == 7

    doc = _doc_with_extractions(1, [_item(5, "Total", 1234.5)])
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([doc])
    )
    values = await async_client.documents.get_values(document_ids=[1], prompts=[5])
    assert values == {1: {"Total": 1234.5}}