"""Auto-paginating iterate() helpers."""

from __future__ import annotations


def _project(pid: int, name: str) -> dict:
    return {
        "id": pid,
        "organization_id": "org",
        "name": name,
        "created_at": "2026-05-29T00:00:00Z",
    }


def test_iterate_fetches_pages_until_exhausted(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects/search",
        json={"items": [_project(1, "A")], "page": 1, "page_size": 1, "has_more": True},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects/search",
        json={"items": [_project(2, "B")], "page": 2, "page_size": 1, "has_more": False},
    )

    projects = list(client.projects.iterate(page_size=1))

    assert [p.id for p in projects] == [1, 2]
    searches = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/search"]
    assert len(searches) == 2


def test_iterate_stops_after_single_page(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/projects/search",
        json={"items": [_project(1, "A")], "page": 1, "page_size": 50, "has_more": False},
    )

    projects = list(client.projects.iterate())

    assert [p.id for p in projects] == [1]
    searches = [r for r in httpx_mock.get_requests() if r.url.path == "/api/projects/search"]
    assert len(searches) == 1
