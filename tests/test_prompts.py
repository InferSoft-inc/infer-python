"""Phase 4: the prompts resource (read-only)."""

from __future__ import annotations

import json

from infersoft import DataTypeName


def _prompt(pid: int, name: str, data_type: str = "String") -> dict:
    return {
        "id": pid,
        "organization_id": "org",
        "name": name,
        "description": "desc",
        "data_type": data_type,
        "document_class": "invoice",
        "deleted": False,
        "created_at": "2026-05-29T00:00:00Z",
        "updated_at": "2026-05-29T00:00:00Z",
    }


def test_search_sends_query_and_parses_enum(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/prompts/search",
        json={
            "items": [_prompt(1, "Total", "Number")],
            "page": 1,
            "page_size": 50,
            "has_more": False,
        },
    )

    page = client.prompts.search("tot", document_class="invoice")

    assert page.items[0].data_type is DataTypeName.number
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/prompts/search")
    body = json.loads(post.content)
    assert body["q"] == "tot" and body["document_class"] == "invoice"
    assert body["include_deleted"] is False and body["order_by"] == "name"
    assert "idempotency-key" not in {k.lower() for k in post.headers}  # read: no key


def test_iterate_paginates(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/prompts/search",
        json={"items": [_prompt(1, "A")], "page": 1, "page_size": 1, "has_more": True},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/prompts/search",
        json={"items": [_prompt(2, "B")], "page": 2, "page_size": 1, "has_more": False},
    )

    ids = [p.id for p in client.prompts.iterate(page_size=1)]
    assert ids == [1, 2]
    searches = [r for r in httpx_mock.get_requests() if r.url.path == "/api/prompts/search"]
    assert len(searches) == 2


def test_search_parses_display_metadata(client, httpx_mock):
    with_meta = _prompt(1, "Amount")
    with_meta["display_type"] = "currency"
    with_meta["group_name"] = "Financials"
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/prompts/search",
        json={
            "items": [with_meta, _prompt(2, "Plain")],
            "page": 1,
            "page_size": 50,
            "has_more": False,
        },
    )

    page = client.prompts.search()

    assert page.items[0].display_type == "currency"
    assert page.items[0].group_name == "Financials"
    assert page.items[1].display_type is None
    assert page.items[1].group_name is None


def test_search_parses_example_json(client, httpx_mock):
    with_example = _prompt(1, "Amount")
    with_example["example"] = {"example": "$1,234.50", "explanation": "Total contract value."}
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/prompts/search",
        json={
            "items": [with_example, _prompt(2, "Plain")],
            "page": 1,
            "page_size": 50,
            "has_more": False,
        },
    )

    page = client.prompts.search()

    assert page.items[0].example == {
        "example": "$1,234.50",
        "explanation": "Total contract value.",
    }
    assert page.items[1].example is None
