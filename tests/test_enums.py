"""Extensible (forward-compatible) enums: unknown server values are accepted
as pseudo-members instead of raising, mirroring ``extra="allow"`` on models."""

from __future__ import annotations

import json

import pytest

from infersoft import DataTypeName, DocumentStatus, JobStatus
from infersoft.models import DocumentSummary, Job

_DOC = {
    "id": 1,
    "organization_id": "org",
    "name": "f.pdf",
    "is_valid": True,
    "created_at": "2026-05-29T00:00:00Z",
    "has_active_workflow": False,
}
_JOB = {
    "id": 1,
    "organization_id": "org",
    "organization_name": "Acme",
    "total_docs": 1,
    "completed_docs": 0,
    "error_count": 0,
    "createdAt": "2026-05-29T00:00:00Z",
}


def test_known_value_resolves_to_canonical_member() -> None:
    # Known values must stay identity-stable so ``is`` comparisons keep working.
    assert DocumentStatus("ready") is DocumentStatus.ready
    assert JobStatus("completed") is JobStatus.completed
    assert DataTypeName("Number") is DataTypeName.number


def test_unknown_value_is_accepted_not_raised() -> None:
    member = DocumentStatus("quarantined")
    assert isinstance(member, DocumentStatus)
    assert member.value == "quarantined"
    assert member == "quarantined"  # str subclass: direct comparison works
    # Doesn't collide with known members.
    assert member != DocumentStatus.ready
    assert member not in {DocumentStatus.ready, DocumentStatus.processing}


def test_unknown_members_are_identity_stable() -> None:
    assert DocumentStatus("quarantined") is DocumentStatus("quarantined")
    assert JobStatus("queued") is JobStatus("queued")


def test_iteration_and_members_expose_only_known_values() -> None:
    DocumentStatus("transient_unknown")  # synthesize a pseudo-member
    assert "transient_unknown" not in DocumentStatus.__members__
    assert [m.value for m in DocumentStatus] == ["uploading", "processing", "ready"]


def test_model_parses_unknown_status_forward_compatible() -> None:
    # The whole point: a status the SDK doesn't know about must not break parse.
    doc = DocumentSummary.model_validate({**_DOC, "status": "quarantined"})
    assert doc.status == "quarantined"
    assert isinstance(doc.status, DocumentStatus)

    job = Job.model_validate({**_JOB, "status": "queued"})
    assert job.status == "queued" and isinstance(job.status, JobStatus)


def test_model_parses_known_status_to_real_member() -> None:
    doc = DocumentSummary.model_validate({**_DOC, "status": "ready"})
    assert doc.status is DocumentStatus.ready


def test_unknown_status_is_not_terminal_or_ready() -> None:
    # Guards the wait-loop semantics: an unknown status must not be mistaken for
    # ``ready`` (documents) or a terminal job state (jobs).
    doc = DocumentSummary.model_validate({**_DOC, "status": "quarantined"})
    assert doc.status != DocumentStatus.ready

    job = Job.model_validate({**_JOB, "status": "queued"})
    terminal = {JobStatus.completed, JobStatus.failed, JobStatus.partial_success}
    assert job.status not in terminal


def test_serialization_round_trips_raw_value() -> None:
    doc = DocumentSummary.model_validate({**_DOC, "status": "quarantined"})
    # JSON dumps the raw string (matching pydantic's default enum serialization).
    assert json.loads(doc.model_dump_json())["status"] == "quarantined"
    # Python-mode dump keeps the member (a str subclass equal to the raw value).
    dumped = doc.model_dump()["status"]
    assert dumped == "quarantined" and isinstance(dumped, DocumentStatus)


def test_non_string_value_still_raises() -> None:
    with pytest.raises(ValueError):
        DocumentStatus(123)


def _enum_schema(model_schema: dict, prop: str, enum_name: str) -> dict:
    """Resolve an enum's (inlined) schema from a model property.

    Pydantic inlines our literal schema into the property — directly for
    required fields, inside ``anyOf`` (next to null) for optional ones.
    """
    node = model_schema["properties"][prop]
    candidates = [node, *node.get("anyOf", [])]
    for c in candidates:
        if c.get("title") == enum_name:
            return c
    raise AssertionError(f"{enum_name} schema not found in property {prop}: {node}")


@pytest.mark.parametrize("mode", ["validation", "serialization"])
def test_model_json_schema_generates_for_enum_models(mode: str) -> None:
    # The P1 regression guard: extensible enums must not break JSON-schema
    # generation (FastAPI response models, LLM tool schemas) in either mode.
    from infersoft.models import ExtractionResultValue

    for model, prop, enum_name in (
        (DocumentSummary, "status", "DocumentStatus"),
        (Job, "status", "JobStatus"),
        (ExtractionResultValue, "data_type", "DataTypeName"),
    ):
        schema = model.model_json_schema(mode=mode)
        d = _enum_schema(schema, prop, enum_name)
        assert d["type"] == "string"


def test_enum_json_schema_is_open_not_closed() -> None:
    # The schema must admit ANY string (mirroring runtime leniency): known
    # members ride along as examples, but a closed `enum` key would make
    # downstream validators reject future server values — do not "fix" this
    # back into an enum.
    schema = Job.model_json_schema()
    d = _enum_schema(schema, "status", "JobStatus")
    assert "enum" not in d
    assert set(d["examples"]) == {m.value for m in JobStatus}
    assert "forward compatibility" in d["description"]
