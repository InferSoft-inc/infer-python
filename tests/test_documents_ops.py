"""Phase 1: documents move_to_folder / bulk_delete."""

from __future__ import annotations

import json


def _idem(post):
    return post.headers["Idempotency-Key"]


def test_move_to_folder_sends_selectors_and_target(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/move-to-folder",
        json={"matched": 5, "moved": 4, "skipped": 1},
    )

    sel = {"include": [{"type": "fileSelector", "files": [1, 2, 3]}]}
    result = client.documents.move_to_folder(sel, target_folder_id=42)

    assert (result.matched, result.moved, result.skipped) == (5, 4, 1)
    post = next(
        r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/move-to-folder"
    )
    body = json.loads(post.content)
    assert body == {"selectors": sel, "target_folder_id": 42}
    assert _idem(post)  # write carries an idempotency key


def test_move_to_folder_omits_target_for_root(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/move-to-folder",
        json={"matched": 1, "moved": 1, "skipped": 0},
    )

    client.documents.move_to_folder({"include": []})

    post = next(
        r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/move-to-folder"
    )
    assert "target_folder_id" not in json.loads(post.content)


def test_bulk_delete_dry_run(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/bulk_delete",
        json={"matched": 9, "dry_run": True},
    )

    result = client.documents.bulk_delete({"include": []}, dry_run=True)

    assert result.matched == 9 and result.dry_run is True
    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/bulk_delete")
    assert json.loads(post.content)["dry_run"] is True
