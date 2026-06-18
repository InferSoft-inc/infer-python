"""Typed request/response models mirroring the Infersoft OpenAPI schema.

Response models use ``extra="allow"`` so that new server-side fields never
break an older SDK build *and* remain reachable (via ``model_extra`` / attribute
access) before a client upgrades to a release that types them.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema


class _Model(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)


# Synthesized pseudo-members for unknown enum values, keyed by (enum class, raw
# value), so repeated unknowns are identity-stable like real members.
_UNKNOWN_MEMBERS: dict[tuple[type, str], ExtensibleStrEnum] = {}


class ExtensibleStrEnum(str, Enum):
    """A string enum that accepts unknown values instead of raising.

    Known members behave exactly like a normal ``str`` enum — autocomplete,
    ``is`` identity, ``.value``, ``in``, and ``==`` all work as usual. A value
    the server adds *after* this SDK was built (e.g. a new document status) is
    accepted as a pseudo-member whose ``.value`` is the raw string, rather than
    failing the whole response with a ``ValidationError``. This mirrors the
    ``extra="allow"`` posture of the response models: additive server changes
    never break an older client.

    Compare against known members as usual (``status is DocumentStatus.ready``);
    an unknown value won't equal any known member, so forward-compatible code
    falls through to a default branch. The member is a ``str``, so
    ``status == "some_new_state"`` works directly; read the raw value with
    ``status.value`` (note ``str(member)`` yields ``"DocumentStatus.<name>"``,
    as with any ``str`` enum).
    """

    @classmethod
    def _missing_(cls, value: object) -> ExtensibleStrEnum | None:
        # Non-strings are genuine type errors; let Enum raise as normal.
        if not isinstance(value, str):
            return None
        cached = _UNKNOWN_MEMBERS.get((cls, value))
        if cached is not None:
            return cached
        # Craft a pseudo-member: a str-subclass instance of this enum carrying
        # the raw value. It is intentionally *not* added to the enum's member
        # map, so iteration/``__members__`` still expose only known values.
        member = str.__new__(cls, value)
        member._name_ = value
        member._value_ = value
        _UNKNOWN_MEMBERS[(cls, value)] = member
        return member

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        # Validate by calling ``cls(value)`` so unknown values flow through
        # ``_missing_`` (forward compatible). Pydantic's default enum schema
        # does a strict membership check and would reject them outright.
        def validate(value: object) -> ExtensibleStrEnum:
            if isinstance(value, cls):
                return value
            return cls(value)

        return core_schema.no_info_plain_validator_function(
            validate,
            # Dump to the raw string in JSON (matching pydantic's default enum
            # serialization); Python-mode dumps keep the member object.
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda member: member.value, when_used="json"
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema_: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # Returned WITHOUT calling handler(core_schema_): the handler would
        # descend into the plain validator function above, which has no JSON
        # schema representation and raises PydanticInvalidForJsonSchema.
        #
        # Deliberately NOT a closed ``enum`` schema: unknown server values are
        # accepted at runtime (see _missing_), so the schema must admit any
        # string — a closed enum would make downstream validators built from
        # this schema (FastAPI responses, codegen, JSON-schema checkers) reject
        # the very values the runtime tolerates. Known members are surfaced as
        # examples/documentation instead.
        return {
            "type": "string",
            "title": cls.__name__,
            "description": (
                f"Known values: {', '.join(m.value for m in cls)}. "
                "Other strings are accepted for forward compatibility."
            ),
            "examples": [m.value for m in cls],
        }


# ----- Uploads -----------------------------------------------------------------


class UploadFile(_Model):
    file_name: str
    content_type: str
    size: int


class Tag(_Model):
    id: int
    name: str


class UploadErrorInfo(_Model):
    code: str
    message: str
    file_size: int | None = None
    max_size: int | None = None
    retryable: bool | None = None


class UploadDuplicate(_Model):
    """An existing document whose content md5 matches an upload item.

    Advisory only — the upload still proceeds and a ``put_url`` is issued; the
    caller chooses how to react via ``on_duplicate`` on ``documents.upload``.
    """

    document_id: int
    name: str
    file_size: int | None = None
    page_count: int | None = None
    created_at: datetime | None = None


class UploadItem(_Model):
    client_file_name: str
    document_id: int | None = None
    upload_mode: str | None = None
    put_url: str | None = None
    required_headers: dict[str, str] | None = None
    expires_at: datetime | None = None
    error: UploadErrorInfo | None = None
    duplicates: list[UploadDuplicate] = Field(default_factory=list)


class Project(_Model):
    id: int
    organization_id: str
    name: str
    created_at: datetime


class BatchUploadResponse(_Model):
    items: list[UploadItem]
    project: Project | None = None
    tags: list[Tag] = Field(default_factory=list)


class FileOutcome(_Model):
    """Per-file result of a high-level ``documents.upload`` call."""

    file_name: str
    document_id: int | None = None
    uploaded: bool = False
    error: UploadErrorInfo | None = None
    #: Existing documents whose content md5 matched this file (advisory hint).
    duplicates: list[UploadDuplicate] = Field(default_factory=list)
    #: True when ``on_duplicate='block'`` skipped this file (a known duplicate);
    #: the placeholder document the plan created was removed.
    skipped_as_duplicate: bool = False


class UploadResult(_Model):
    outcomes: list[FileOutcome]
    project: Project | None = None
    tags: list[Tag] = Field(default_factory=list)

    @property
    def document_ids(self) -> list[int]:
        return [o.document_id for o in self.outcomes if o.document_id is not None]

    @property
    def succeeded(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.uploaded]

    @property
    def failed(self) -> list[FileOutcome]:
        """Items that errored — excludes ones skipped as duplicates."""
        return [o for o in self.outcomes if not o.uploaded and not o.skipped_as_duplicate]

    @property
    def skipped(self) -> list[FileOutcome]:
        """Items skipped because they duplicated existing content (``on_duplicate='block'``)."""
        return [o for o in self.outcomes if o.skipped_as_duplicate]


# ----- Documents ---------------------------------------------------------------


class DataTypeName(ExtensibleStrEnum):
    """Canonical data type of a prompt value (shared by prompts and extraction)."""

    string = "String"
    number = "Number"
    boolean = "Boolean"
    date = "Date"


class ExtractionTracebackBBox(_Model):
    top: float = Field(alias="Top")
    left: float = Field(alias="Left")
    width: float = Field(alias="Width")
    height: float = Field(alias="Height")


class ExtractionTracebackItem(_Model):
    bbox: ExtractionTracebackBBox
    page: int
    confidence: float
    issues: list[str] = Field(default_factory=list)


class ExtractionResultValue(_Model):
    name: str | None = None
    data_type: DataTypeName | None = None
    parsed_value: Any | None = None
    raw_value: str | None = None
    traceback: list[ExtractionTracebackItem] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    readability: float | None = None


class DocumentExtractionResultItem(_Model):
    """One prompt's extracted value, returned when prompt IDs are passed to
    ``documents.search`` / ``documents.get``."""

    prompt_id: int
    value: ExtractionResultValue


class DownloadResponse(_Model):
    """A short-lived signed URL for downloading a document's file.

    Carries no bytes: fetch ``url`` before ``expires_at``. The composite
    ``documents.download`` resolves and fetches in one call; ``expires_at``
    matters to callers using ``documents.download_url`` to fetch themselves.
    """

    url: str
    expires_at: datetime


class DocumentStatus(ExtensibleStrEnum):
    uploading = "uploading"
    processing = "processing"
    ready = "ready"


class DocumentSummary(_Model):
    id: int
    organization_id: str
    name: str
    status: DocumentStatus
    is_valid: bool
    created_at: datetime
    has_active_workflow: bool
    file_size: int | None = None
    page_count: int | None = None
    source_document: int | None = None
    folder_id: int | None = None
    document_class: str | None = None
    document_subclass: str | None = None
    extraction_results: list[DocumentExtractionResultItem] | None = None

    @property
    def extractions(self) -> dict[int, ExtractionResultValue]:
        """Extraction values keyed by prompt id.

        Populated when prompt IDs were passed to ``documents.search`` / ``get``;
        empty otherwise.
        """
        return {item.prompt_id: item.value for item in self.extraction_results or []}


class DocumentPage(_Model):
    items: list[DocumentSummary]
    page: int
    page_size: int
    has_more: bool


class MoveResult(_Model):
    """Outcome of ``documents.move_to_folder``."""

    matched: int
    moved: int
    skipped: int


class BulkDeleteResult(_Model):
    """Outcome of ``documents.bulk_delete`` (``matched`` is the count that
    matched the selectors; nothing is deleted when ``dry_run`` is true)."""

    matched: int
    dry_run: bool


# ----- Projects ----------------------------------------------------------------


class ProjectPage(_Model):
    items: list[Project]
    page: int
    page_size: int
    has_more: bool


class AssignDocumentsResult(_Model):
    """Outcome of ``projects.assign_documents``."""

    project: Project
    matched: int
    added: int
    skipped: int


# ----- Jobs --------------------------------------------------------------------


class JobStatus(ExtensibleStrEnum):
    running = "running"
    completed = "completed"
    failed = "failed"
    partial_success = "partial_success"
    creating_workflows = "creating_workflows"


class CreditsEstimate(_Model):
    total_credits: int
    page_count: int
    id: str


class Job(_Model):
    id: int
    organization_id: str
    organization_name: str
    total_docs: int
    completed_docs: int
    error_count: int
    status: JobStatus
    stages: list[str] = Field(default_factory=list)
    created_at: datetime = Field(alias="createdAt")
    project_id: int | None = None
    selectors: dict[str, Any] | None = None
    credits: int | None = None


class JobPage(_Model):
    items: list[Job]
    page: int
    page_size: int
    has_more: bool


# ----- Folders -----------------------------------------------------------------


class Folder(_Model):
    id: int
    organization_id: str
    name: str
    parent_id: int | None = None
    created_at: datetime
    updated_at: datetime
    has_children: bool


class FolderPage(_Model):
    items: list[Folder]
    page: int
    page_size: int
    has_more: bool


class FolderMoveResult(_Model):
    """Outcome of ``folders.move``; the ``skipped_*`` lists explain non-moves."""

    matched: int
    moved_ids: list[int] = Field(default_factory=list)
    skipped_already_in_target_ids: list[int] = Field(default_factory=list)
    skipped_name_conflict_ids: list[int] = Field(default_factory=list)
    skipped_duplicate_in_selection_ids: list[int] = Field(default_factory=list)
    skipped_invalid_parent_ids: list[int] = Field(default_factory=list)


class FolderBulkDeleteResult(_Model):
    """Outcome of ``folders.bulk_delete``."""

    requested: int
    deleted: int
    deleted_folder_ids: list[int] = Field(default_factory=list)
    not_found_folder_ids: list[int] = Field(default_factory=list)


class FolderPathResult(_Model):
    """One resolved path: its folders ordered from the base parent to the leaf."""

    folders: list[Folder] = Field(default_factory=list)


class FolderPathsResult(_Model):
    """Outcome of ``folders.resolve_paths``; ``results`` preserves input order."""

    results: list[FolderPathResult] = Field(default_factory=list)


# ----- Prompts -----------------------------------------------------------------


class PromptMeta(_Model):
    id: int
    organization_id: str
    name: str
    description: str | None = None
    data_type: DataTypeName
    document_class: str
    deleted: bool
    created_at: datetime
    updated_at: datetime


class PromptPage(_Model):
    items: list[PromptMeta]
    page: int
    page_size: int
    has_more: bool


# ----- Extract pipeline ----------------------------------------------------------


class ExtractResult(_Model):
    """Outcome of the end-to-end ``client.extract`` pipeline.

    ``values`` is the happy-path payload: ``{document_id: {field: parsed_value}}``.
    ``documents`` keeps the full typed extraction items (traceback, confidence,
    issues) and ``upload`` is present only when local files were uploaded.
    Files that failed to upload are skipped, not raised: check
    ``upload.failed`` before treating ``values`` as covering every input.
    """

    job: Job
    upload: UploadResult | None = None
    documents: list[DocumentSummary] = Field(default_factory=list)
    values: dict[int, dict[str | int, Any]] = Field(default_factory=dict)
