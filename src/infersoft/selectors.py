"""Builders for the selectors the API accepts in search / bulk / move / job calls.

Each ``build_*_selector`` returns a plain selector ``dict`` with the correct ``type``
discriminator; combine them into a Selectors object with :func:`build_selectors`::

    from infersoft import build_selectors, build_file_selector, build_name_selector

    client.documents.bulk_delete(
        build_selectors(
            include=[build_file_selector([1, 2])],
            exclude=[build_name_selector("draft")],
        )
    )

These are a convenience only — anywhere a ``selectors`` argument is accepted you can
still pass a raw dict, and methods also accept a ``document_ids=`` shortcut. Date/time
bounds are ISO-8601 strings. Scope matches the OpenAPI ``Selector`` union (14 selectors);
the API may support more.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

Selector = dict[str, Any]


def build_file_selector(ids: Sequence[int]) -> Selector:
    """Match documents by id."""
    return {"type": "fileSelector", "files": list(ids)}


def build_folder_selector(folder_id: int) -> Selector:
    """Match documents in a folder."""
    return {"type": "folderSelector", "folder_id": folder_id}


def build_name_selector(value: str) -> Selector:
    """Match by document name."""
    return {"type": "nameSelector", "name": value}


def build_document_class_selector(classes: Sequence[str]) -> Selector:
    """Match by document class."""
    return {"type": "documentClassSelector", "classes": list(classes)}


def build_tag_selector(
    ids: Sequence[int], *, tagged_from: str | None = None, tagged_to: str | None = None
) -> Selector:
    """Match documents carrying any of the given tag ids."""
    sel: Selector = {"type": "tagSelector", "tags": list(ids)}
    if tagged_from is not None:
        sel["tagged_from"] = tagged_from
    if tagged_to is not None:
        sel["tagged_to"] = tagged_to
    return sel


def build_created_at_selector(
    *, created_from: str | None = None, created_to: str | None = None
) -> Selector:
    """Match documents created within a time range (pass at least one bound)."""
    if created_from is None and created_to is None:
        raise ValueError("build_created_at_selector() requires created_from and/or created_to")
    sel: Selector = {"type": "createdAtSelector"}
    if created_from is not None:
        sel["created_from"] = created_from
    if created_to is not None:
        sel["created_to"] = created_to
    return sel


def build_size_selector(*, size_from: int | None = None, size_to: int | None = None) -> Selector:
    """Match documents by byte size range (pass at least one bound)."""
    if size_from is None and size_to is None:
        raise ValueError("build_size_selector() requires size_from and/or size_to")
    sel: Selector = {"type": "sizeSelector"}
    if size_from is not None:
        sel["size_from"] = size_from
    if size_to is not None:
        sel["size_to"] = size_to
    return sel


def build_page_count_selector(
    *, page_count_from: int | None = None, page_count_to: int | None = None
) -> Selector:
    """Match documents by page-count range (pass at least one bound)."""
    if page_count_from is None and page_count_to is None:
        raise ValueError(
            "build_page_count_selector() requires page_count_from and/or page_count_to"
        )
    sel: Selector = {"type": "pageCountSelector"}
    if page_count_from is not None:
        sel["page_count_from"] = page_count_from
    if page_count_to is not None:
        sel["page_count_to"] = page_count_to
    return sel


def build_source_document_selector(ids: Sequence[int]) -> Selector:
    """Match splits derived from the given source document ids."""
    return {"type": "sourceDocumentSelector", "source_documents": list(ids)}


def build_project_selector(
    project_id: int, *, added_from: str | None = None, added_to: str | None = None
) -> Selector:
    """Match documents in a project.

    ``added_from`` / ``added_to`` filter by when each document was added to the
    project (``project_documents.created_at``); both bounds are inclusive.
    """
    sel: Selector = {"type": "projectSelector", "project_id": project_id}
    if added_from is not None:
        sel["added_from"] = added_from
    if added_to is not None:
        sel["added_to"] = added_to
    return sel


def build_job_selector(ids: Sequence[int]) -> Selector:
    """Match documents touched by the given job ids."""
    return {"type": "jobSelector", "jobs": list(ids)}


def build_is_valid_selector() -> Selector:
    """Flag selector: valid documents (use in ``exclude`` for invalid)."""
    return {"type": "isValidSelector"}


def build_has_children_selector() -> Selector:
    """Flag selector: documents that have child splits."""
    return {"type": "hasChildrenSelector"}


def build_has_running_workflow_selector() -> Selector:
    """Flag selector: documents with a running workflow."""
    return {"type": "hasRunningWorkflowSelector"}


def build_selectors(
    *,
    include: Sequence[Selector] | None = None,
    exclude: Sequence[Selector] | None = None,
) -> dict[str, Any]:
    """Combine selectors into a Selectors object (``include`` AND, ``exclude`` NOT)."""
    return {"include": list(include or []), "exclude": list(exclude or [])}


def _resolve_selectors(
    selectors: dict[str, Any] | None,
    document_ids: Sequence[int] | None,
    *,
    required: bool,
) -> dict[str, Any] | None:
    """Resolve a resource method's selector input.

    Callers may pass a ready ``selectors`` object, or — as a shortcut — a list of
    ``document_ids`` that is wrapped into a file selector. At most one of the two;
    when ``required`` is true (the call cannot run without a target set), exactly
    one must be given.
    """
    if selectors is not None and document_ids is not None:
        raise ValueError("pass either `selectors` or `document_ids`, not both")
    if document_ids is not None:
        return build_selectors(include=[build_file_selector(document_ids)])
    if required and selectors is None:
        raise ValueError("pass `selectors` or `document_ids`")
    return selectors
