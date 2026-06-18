"""Phase 6: selector builders (build_*_selector + build_selectors)."""

from __future__ import annotations

import json

import pytest

import infersoft
from infersoft import (
    build_created_at_selector,
    build_document_class_selector,
    build_file_selector,
    build_folder_selector,
    build_is_valid_selector,
    build_name_selector,
    build_page_count_selector,
    build_project_selector,
    build_selectors,
    build_size_selector,
    build_tag_selector,
)


def test_individual_builders_emit_correct_type_and_fields():
    assert build_file_selector([1, 2]) == {"type": "fileSelector", "files": [1, 2]}
    assert build_folder_selector(9) == {"type": "folderSelector", "folder_id": 9}
    assert build_name_selector("draft") == {"type": "nameSelector", "name": "draft"}
    assert build_document_class_selector(["invoice"]) == {
        "type": "documentClassSelector",
        "classes": ["invoice"],
    }
    assert build_tag_selector([3], tagged_from="2026-01-01") == {
        "type": "tagSelector",
        "tags": [3],
        "tagged_from": "2026-01-01",
    }
    assert build_is_valid_selector() == {"type": "isValidSelector"}
    assert build_project_selector(7) == {"type": "projectSelector", "project_id": 7}
    # The window emits added_from/added_to (matching the server's storage field
    # names); linked_from/linked_to would be silently dropped server-side.
    assert build_project_selector(7, added_from="2026-01-01", added_to="2026-02-01") == {
        "type": "projectSelector",
        "project_id": 7,
        "added_from": "2026-01-01",
        "added_to": "2026-02-01",
    }


def test_range_selectors_require_a_bound():
    for fn in (build_created_at_selector, build_size_selector, build_page_count_selector):
        with pytest.raises(ValueError):
            fn()
    assert build_size_selector(size_from=1000) == {"type": "sizeSelector", "size_from": 1000}


def test_build_selectors_wraps_include_and_exclude():
    out = build_selectors(
        include=[build_file_selector([1]), build_tag_selector([2])],
        exclude=[build_name_selector("x")],
    )
    assert out == {
        "include": [
            {"type": "fileSelector", "files": [1]},
            {"type": "tagSelector", "tags": [2]},
        ],
        "exclude": [{"type": "nameSelector", "name": "x"}],
    }
    assert build_selectors(include=[build_is_valid_selector()]) == {
        "include": [{"type": "isValidSelector"}],
        "exclude": [],
    }


def test_also_available_under_selectors_module():
    # Both the top-level export and the `selectors` submodule work.
    assert infersoft.selectors.build_file_selector([1]) == build_file_selector([1])


def test_builders_compose_with_a_resource_call(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/documents/bulk_delete",
        json={"matched": 2, "dry_run": True},
    )

    selectors = build_selectors(
        include=[build_folder_selector(5)], exclude=[build_name_selector("draft")]
    )
    client.documents.bulk_delete(selectors, dry_run=True)

    post = next(r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/bulk_delete")
    assert json.loads(post.content)["selectors"] == selectors
