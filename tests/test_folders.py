"""Phase 3: the folders resource."""

from __future__ import annotations

import json


def _folder(fid: int, name: str, *, parent_id=None, has_children=False) -> dict:
    return {
        "id": fid,
        "organization_id": "org",
        "name": name,
        "parent_id": parent_id,
        "created_at": "2026-05-29T00:00:00Z",
        "updated_at": "2026-05-29T00:00:00Z",
        "has_children": has_children,
    }


def test_create_folder(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/folders", json=_folder(1, "Invoices")
    )

    folder = client.folders.create("Invoices", parent_id=5)

    assert folder.id == 1 and folder.name == "Invoices"
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/folders")
    assert json.loads(post.content) == {"name": "Invoices", "parent_id": 5}
    assert post.headers["Idempotency-Key"]


def test_get_folder(client, httpx_mock):
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/folders/1",
        json=_folder(1, "Invoices", has_children=True),
    )

    folder = client.folders.get(1)
    assert folder.has_children is True


def test_rename_folder_uses_patch(client, httpx_mock):
    httpx_mock.add_response(
        method="PATCH", url="https://api.test/api/folders/1", json=_folder(1, "Renamed")
    )

    folder = client.folders.rename(1, "Renamed")

    assert folder.name == "Renamed"
    patch = next(r for r in httpx_mock.get_requests() if r.method == "PATCH")
    assert json.loads(patch.content) == {"name": "Renamed"}
    assert patch.headers["Idempotency-Key"]


def test_search_returns_page_no_key(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/folders/search",
        json={"items": [_folder(1, "A")], "page": 1, "page_size": 50, "has_more": False},
    )

    page = client.folders.search(parent_id=2)

    assert page.items[0].id == 1
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/folders/search")
    assert json.loads(post.content)["parent_id"] == 2
    assert "idempotency-key" not in {k.lower() for k in post.headers}  # read: no key


def test_iterate_paginates(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/folders/search",
        json={"items": [_folder(1, "A")], "page": 1, "page_size": 1, "has_more": True},
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/folders/search",
        json={"items": [_folder(2, "B")], "page": 2, "page_size": 1, "has_more": False},
    )

    ids = [f.id for f in client.folders.iterate(page_size=1)]
    assert ids == [1, 2]


def test_move_returns_skip_breakdown(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/folders/move",
        json={
            "matched": 3,
            "moved_ids": [1],
            "skipped_already_in_target_ids": [2],
            "skipped_name_conflict_ids": [3],
            "skipped_duplicate_in_selection_ids": [],
            "skipped_invalid_parent_ids": [],
        },
    )

    result = client.folders.move([1, 2, 3], target_parent_id=9)

    assert result.moved_ids == [1]
    assert result.skipped_name_conflict_ids == [3]
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/folders/move")
    assert json.loads(post.content) == {"folder_ids": [1, 2, 3], "target_parent_id": 9}


def test_bulk_delete(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/folders/bulk_delete",
        json={
            "requested": 2,
            "deleted": 1,
            "deleted_folder_ids": [1],
            "not_found_folder_ids": [99],
        },
    )

    result = client.folders.bulk_delete([1, 99])

    assert result.deleted == 1 and result.not_found_folder_ids == [99]


def test_resolve_paths_returns_nested_folders_no_key(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/folders/paths",
        json={"results": [{"folders": [_folder(1, "2026"), _folder(2, "jan", parent_id=1)]}]},
    )

    result = client.folders.resolve_paths(["2026/jan"])

    assert [f.name for f in result.results[0].folders] == ["2026", "jan"]
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/folders/paths")
    assert json.loads(post.content) == {"paths": ["2026/jan"]}
    assert "idempotency-key" not in {k.lower() for k in post.headers}  # get-or-create, no key
