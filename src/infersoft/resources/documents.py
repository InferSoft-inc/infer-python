"""Documents resource, including the high-level batch upload workflow."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import os
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Union

from .._http import AsyncHttpClient, HttpClient, new_idempotency_key
from .._wait import (
    DEFAULT_DOCUMENT_WAIT_SECONDS,
    DEFAULT_POLL_INTERVAL,
    apoll_until,
    poll_until,
    warn_once_unknown_status,
)
from ..errors import UploadManyError
from ..models import (
    BatchUploadResponse,
    BulkDeleteResult,
    DocumentPage,
    DocumentStatus,
    DocumentSummary,
    DownloadResponse,
    FileOutcome,
    MoveResult,
    Project,
    Tag,
    UploadResult,
)
from ..selectors import _resolve_selectors

#: Known status VALUES (pseudo-members from unknown server statuses are
#: instances of DocumentStatus too, so membership is checked by value).
_KNOWN_DOCUMENT_STATUS_VALUES = frozenset(m.value for m in DocumentStatus)

PathLike = Union[str, "os.PathLike[str]"]
Selectors = dict[str, Any]

#: Maximum files accepted in a single ``upload()`` call. Mirrors the API's
#: ``BatchUploadRequest.files`` ``maxItems``. Callers that need to upload more
#: must split their files into batches of this size or fewer.
MAX_BATCH_FILES = 100

_DEFAULT_CONTENT_TYPE = "application/octet-stream"

logger = logging.getLogger("infersoft")

#: How ``documents.upload`` reacts when the server flags a file as a content
#: duplicate of an existing document (an advisory md5 match):
#: ``"allow"`` uploads it anyway (default, prior behavior); ``"notify"`` uploads
#: it but logs a warning naming the matched document(s); ``"block"`` skips the
#: byte upload and deletes the placeholder document the plan created. In every
#: mode the matches are reported on ``FileOutcome.duplicates``.
OnDuplicate = Literal["allow", "notify", "block"]


def _derive_remote_names(
    paths: Sequence[Path], raw_inputs: Sequence[PathLike], *, flatten: bool
) -> list[str]:
    """Pick the ``file_name`` to send for each local path.

    The API echoes ``file_name`` back verbatim as ``client_file_name`` and
    interprets any ``/`` in it as folder structure, enforcing name uniqueness
    *per folder*.

    - ``flatten=True``: send each file's basename. Raises ``ValueError`` if two
      or more files share a basename, since the flattened names would collide.
    - ``flatten=False``: send the path exactly as the caller provided it, with
      no normalization — clients that want their local layout replicated in the
      app rely on this.
    """
    if flatten:
        basenames = [p.name for p in paths]
        duplicates = sorted(name for name, count in Counter(basenames).items() if count > 1)
        if duplicates:
            raise ValueError(
                "upload(flatten=True) requires unique file names, but these "
                f"basenames are repeated: {', '.join(duplicates)}. Pass "
                "flatten=False to send each file's full path instead."
            )
        return basenames

    return [os.fspath(raw) for raw in raw_inputs]


def _flatten_extractions(
    doc: DocumentSummary, key_by: Literal["name", "prompt_id"]
) -> dict[str | int, Any]:
    """Flatten a document's extraction items to ``{field: parsed_value}``."""
    row: dict[str | int, Any] = {}
    for item in doc.extraction_results or []:
        key: str | int = (
            item.value.name if (key_by == "name" and item.value.name) else item.prompt_id
        )
        if key in row:
            raise ValueError(
                f"two prompts share the extraction key {key!r} on document {doc.id}; "
                "pass key_by='prompt_id' to disambiguate"
            )
        row[key] = item.value.parsed_value
    return row


def _collect_upload_inputs(
    files: PathLike | Sequence[PathLike], *, pattern: str, flatten: bool
) -> tuple[list[Path], list[str]]:
    """Resolve ``upload_many`` input into (paths, remote names).

    A directory is globbed with ``pattern`` (hidden dotfiles are skipped — think
    ``.DS_Store``), sorted for deterministic batch composition; ``flatten=False``
    then derives names relative to that directory, replicating its layout.
    An explicit list behaves exactly like ``upload()``. Duplicate-name checks run
    across the WHOLE set, not per batch.
    """
    if isinstance(files, (str, os.PathLike)) and Path(files).is_dir():
        base = Path(files)
        paths = sorted(p for p in base.glob(pattern) if p.is_file() and not p.name.startswith("."))
        if not paths:
            raise ValueError(f"upload_many() found no files matching {pattern!r} under {base}")
        if flatten:
            names = _derive_remote_names(paths, paths, flatten=True)
        else:
            names = [p.relative_to(base).as_posix() for p in paths]
        return paths, names

    raw_inputs: list[PathLike] = [files] if isinstance(files, (str, os.PathLike)) else list(files)
    paths = [Path(raw) for raw in raw_inputs]
    if not paths:
        raise ValueError("upload_many() requires at least one file")
    for path in paths:
        if not path.is_file():
            raise FileNotFoundError(str(path))
    return paths, _derive_remote_names(paths, raw_inputs, flatten=flatten)


def _md5_hex(path: Path) -> str:
    """Streaming md5 (lowercase hex) of a local file.

    Sent as the upload ``md5`` so the server can flag duplicate uploads against
    documents it already holds. Read in chunks so large files aren't buffered.
    """
    h = hashlib.md5(usedforsecurity=False)  # dedup key, not security → FIPS-safe
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_download_target(directory: Path, file_name: str) -> Path:
    """Resolve a download's output path, neutralizing path separators.

    Document names may contain ``/`` (``flatten=False`` uploads replicate
    client paths) or hostile components like ``..``; only the final component
    is used, and the result must stay inside the destination directory — no
    implicit subdirectories, no traversal.
    """
    base = PurePosixPath(file_name).name
    if base in ("", ".", ".."):
        raise ValueError(f"cannot derive a file name from {file_name!r}")
    target = directory / base
    if not target.resolve().is_relative_to(directory.resolve()):
        raise ValueError(f"refusing to write outside {directory}: {file_name!r}")
    return target


class DocumentsResource:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def upload(
        self,
        files: PathLike | Sequence[PathLike],
        *,
        flatten: bool = True,
        project_id: int | None = None,
        project_name: str | None = None,
        tag_ids: Sequence[int] | None = None,
        tag_names: Sequence[str] | None = None,
        content_type: str | None = None,
        on_duplicate: OnDuplicate = "allow",
        idempotency_key: str | None = None,
        wait: bool = False,
        max_wait_seconds: float | None = DEFAULT_DOCUMENT_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> UploadResult:
        """Upload one or more local files end to end.

        Plans the batch with ``POST /api/uploads``, then PUTs each file's bytes
        directly to the presigned URL the API returns. Items the server reports
        as conflicts (e.g. a name that already exists) are surfaced in the
        result rather than uploaded.

        ``flatten`` controls the name sent for each file. When ``True`` (the
        default) only the basename is sent and a ``ValueError`` is raised if two
        files share a basename. When ``False`` the path is sent exactly as given,
        so the local folder layout is replicated in the app.

        The batch request carries an ``Idempotency-Key`` so a transient failure
        is retried without creating duplicate documents. A fresh key is
        generated per call; pass ``idempotency_key`` to reuse one across process
        restarts for exactly-once semantics. (The presigned PUTs are naturally
        idempotent and need no key.)

        With ``wait=True`` the call blocks (via :meth:`wait_until_ready`) until
        every uploaded document reaches ``ready`` before returning.
        ``max_wait_seconds`` defaults to 10 minutes (``WaitTimeoutError`` after
        that); pass ``None`` for no deadline.
        """
        raw_inputs: list[PathLike] = (
            [files] if isinstance(files, (str, os.PathLike)) else list(files)
        )
        paths = [Path(raw) for raw in raw_inputs]
        if not paths:
            raise ValueError("upload() requires at least one file")
        if len(paths) > MAX_BATCH_FILES:
            raise ValueError(
                f"upload() accepts at most {MAX_BATCH_FILES} files per call (got "
                f"{len(paths)}); split your files into batches of {MAX_BATCH_FILES} "
                "or fewer and call upload() once per batch."
            )

        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(str(path))

        # file_name is the lookup key tying request entries to response items.
        remote_names = _derive_remote_names(paths, raw_inputs, flatten=flatten)
        result = self._upload_batch(
            paths,
            remote_names,
            project_id=project_id,
            project_name=project_name,
            tag_ids=tag_ids,
            tag_names=tag_names,
            content_type=content_type,
            on_duplicate=on_duplicate,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        if wait:
            self.wait_until_ready(
                result.document_ids,
                max_wait_seconds=max_wait_seconds,
                poll_interval=poll_interval,
            )
        return result

    def _upload_batch(
        self,
        paths: Sequence[Path],
        remote_names: Sequence[str],
        *,
        project_id: int | None,
        project_name: str | None,
        tag_ids: Sequence[int] | None,
        tag_names: Sequence[str] | None,
        content_type: str | None,
        on_duplicate: OnDuplicate = "allow",
        idempotency_key: str,
    ) -> UploadResult:
        """Plan one batch and PUT each file's bytes (the core of upload)."""
        by_name: dict[str, Path] = {}
        request_files = []
        for path, name in zip(paths, remote_names, strict=True):
            guessed = content_type or mimetypes.guess_type(path.name)[0] or _DEFAULT_CONTENT_TYPE
            by_name[name] = path
            request_files.append(
                {
                    "file_name": name,
                    "content_type": guessed,
                    "size": path.stat().st_size,
                    "md5": _md5_hex(path),
                }
            )

        body: dict[str, Any] = {"files": request_files}
        if project_id is not None:
            body["project_id"] = project_id
        if project_name is not None:
            body["project_name"] = project_name
        if tag_ids:
            body["tag_ids"] = list(tag_ids)
        if tag_names:
            body["tag_names"] = list(tag_names)

        raw = self._http.request("POST", "/api/uploads", json=body, idempotency_key=idempotency_key)
        response = BatchUploadResponse.model_validate(raw)

        outcomes: list[FileOutcome] = []
        for item in response.items:
            outcome = FileOutcome(
                file_name=item.client_file_name,
                document_id=item.document_id,
                duplicates=item.duplicates,
            )
            if item.put_url:
                if item.duplicates and on_duplicate == "block":
                    # Known content duplicate: skip the byte upload and remove
                    # the placeholder document the plan created for it.
                    if item.document_id is not None:
                        self.delete(item.document_id)
                    outcome.skipped_as_duplicate = True
                else:
                    if item.duplicates and on_duplicate == "notify":
                        logger.warning(
                            "upload %r matches existing document(s) %s",
                            item.client_file_name,
                            [d.document_id for d in item.duplicates],
                        )
                    src_path = by_name.get(item.client_file_name)
                    if src_path is not None:
                        self._http.put_presigned(
                            item.put_url, src_path.read_bytes(), item.required_headers
                        )
                        outcome.uploaded = True
            else:
                outcome.error = item.error
            outcomes.append(outcome)

        return UploadResult(outcomes=outcomes, project=response.project, tags=response.tags)

    def upload_many(
        self,
        files: PathLike | Sequence[PathLike],
        *,
        pattern: str = "**/*",
        flatten: bool = True,
        batch_size: int = MAX_BATCH_FILES,
        project_id: int | None = None,
        project_name: str | None = None,
        tag_ids: Sequence[int] | None = None,
        tag_names: Sequence[str] | None = None,
        content_type: str | None = None,
        on_duplicate: OnDuplicate = "allow",
        idempotency_key: str | None = None,
        on_batch: Callable[[UploadResult], None] | None = None,
        wait: bool = False,
        max_wait_seconds: float | None = DEFAULT_DOCUMENT_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> UploadResult:
        """Upload any number of files, in batches of up to ``batch_size``.

        The explicitly-bulk sibling of :meth:`upload` (which stays strict and
        rejects more than ``MAX_BATCH_FILES``). Accepts a list of paths, or a
        directory — globbed with ``pattern`` (hidden dotfiles skipped); with
        ``flatten=False`` a directory's layout is replicated via names relative
        to it. Duplicate-name validation runs across the whole set up front.

        When ``project_name`` is given, the first batch creates the project and
        the remaining batches are pinned to its id (no duplicate projects); tags
        are pinned the same way. Each batch sends a derived ``Idempotency-Key``
        (``<base>-0000``, ``-0001``, …), so re-running with the same
        ``idempotency_key`` *and the same files in the same order* dedupes
        batch-by-batch. ``on_batch`` is called with each batch's result as it
        completes.

        A failing batch raises ``UploadManyError``; earlier batches are not
        rolled back (see ``.partial`` / ``.batches_completed``). With ``wait=True``
        readiness is awaited once at the end, over all uploaded documents.
        """
        paths, names = _collect_upload_inputs(files, pattern=pattern, flatten=flatten)
        if not 1 <= batch_size <= MAX_BATCH_FILES:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_FILES}")

        base_key = idempotency_key or new_idempotency_key()
        outcomes: list[FileOutcome] = []
        project: Project | None = None
        tags_by_id: dict[int, Tag] = {}
        eff_project_id, eff_project_name = project_id, project_name
        eff_tag_ids, eff_tag_names = tag_ids, tag_names

        for batch_no, start in enumerate(range(0, len(paths), batch_size)):
            try:
                result = self._upload_batch(
                    paths[start : start + batch_size],
                    names[start : start + batch_size],
                    project_id=eff_project_id,
                    project_name=eff_project_name,
                    tag_ids=eff_tag_ids,
                    tag_names=eff_tag_names,
                    content_type=content_type,
                    on_duplicate=on_duplicate,
                    idempotency_key=f"{base_key}-{batch_no:04d}",
                )
            except Exception as exc:
                partial = UploadResult(
                    outcomes=outcomes, project=project, tags=list(tags_by_id.values())
                )
                raise UploadManyError(
                    f"upload_many() batch {batch_no + 1} failed; {batch_no} batch(es) "
                    "were already uploaded (see .partial)",
                    partial=partial,
                    batches_completed=batch_no,
                ) from exc

            outcomes.extend(result.outcomes)
            if result.project is not None:
                project = result.project
                # Pin later batches to the created project (no duplicates).
                eff_project_id, eff_project_name = project.id, None
            if result.tags:
                for tag in result.tags:
                    tags_by_id[tag.id] = tag
                eff_tag_ids = [t.id for t in tags_by_id.values()]
                eff_tag_names = None
            if on_batch is not None:
                on_batch(result)

        total = UploadResult(outcomes=outcomes, project=project, tags=list(tags_by_id.values()))
        if wait:
            self.wait_until_ready(
                total.document_ids,
                max_wait_seconds=max_wait_seconds,
                poll_interval=poll_interval,
            )
        return total

    def wait_until_ready(
        self,
        document_ids: Sequence[int],
        *,
        max_wait_seconds: float | None = DEFAULT_DOCUMENT_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> list[DocumentSummary]:
        """Poll until every given document reaches ``ready`` status.

        Returns the documents once all are ready. ``max_wait_seconds`` defaults
        to 10 minutes, after which a ``WaitTimeoutError`` is raised — so stuck
        documents (or a future status this SDK build does not know) fail loudly
        instead of hanging. Pass ``max_wait_seconds=None`` for no deadline.
        """
        ids = list(document_ids)
        if not ids:
            return []

        def fetch() -> list[DocumentSummary]:
            return list(self.iterate(document_ids=ids))

        seen_unknown: set[str] = set()

        def ready(docs: list[DocumentSummary]) -> bool:
            ready_ids = set()
            for d in docs:
                warn_once_unknown_status(
                    seen_unknown, d.status.value, _KNOWN_DOCUMENT_STATUS_VALUES, "document"
                )
                if d.status == DocumentStatus.ready:
                    ready_ids.add(d.id)
            return all(i in ready_ids for i in ids)

        return poll_until(
            fetch,
            ready,
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
            timeout_message=f"documents {ids} did not all become ready within {max_wait_seconds}s",
        )

    def download_url(self, document_id: int) -> DownloadResponse:
        """Get a short-lived signed URL for a document's underlying file.

        The response carries no bytes: fetch ``url`` before ``expires_at``
        (unauthenticated — the URL carries its own signature). Use
        :meth:`download` to resolve, fetch, and save in one call.
        """
        raw = self._http.request("GET", f"/api/documents/{document_id}/download")
        return DownloadResponse.model_validate(raw)

    def download(
        self,
        document_id: int,
        dest_dir: PathLike,
        *,
        file_name: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Download a document's file into ``dest_dir`` and return the path.

        Resolves a signed URL via :meth:`download_url` and fetches the bytes
        immediately, so URL expiry is not the caller's concern. The file name
        defaults to the document's server-side name (one extra ``get`` call;
        pass ``file_name`` to skip it). Names are reduced to their final path
        component and never escape ``dest_dir`` — server names may contain
        ``/`` when uploaded with ``flatten=False``. Existing files raise
        ``FileExistsError`` unless ``overwrite=True``.
        """
        directory = Path(dest_dir)
        if not directory.is_dir():
            raise NotADirectoryError(f"dest_dir does not exist or is not a directory: {directory}")
        if file_name is None:
            file_name = self.get(document_id).name
        target = _resolve_download_target(directory, file_name)
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))

        signed = self.download_url(document_id)
        data = self._http.get_presigned(signed.url)
        target.write_bytes(data)
        return target

    def get(self, document_id: int, *, prompt_ids: Sequence[int] | None = None) -> DocumentSummary:
        params = None
        if prompt_ids:
            params = {"prompt_ids": ",".join(str(p) for p in prompt_ids)}
        raw = self._http.request("GET", f"/api/documents/{document_id}", params=params)
        return DocumentSummary.model_validate(raw)

    def search(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> DocumentPage:
        selectors = _resolve_selectors(selectors, document_ids, required=False)
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if selectors is not None:
            body["selectors"] = selectors
        if prompts:
            body["prompts"] = list(prompts)
        # Read-only search: safe to retry on transient failures.
        raw = self._http.request("POST", "/api/documents/search", json=body, idempotent=True)
        return DocumentPage.model_validate(raw)

    def iterate(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> Iterator[DocumentSummary]:
        """Yield every matching document, fetching pages on demand."""
        selectors = _resolve_selectors(selectors, document_ids, required=False)
        page = 1
        while True:
            result = self.search(
                selectors,
                prompts=prompts,
                page=page,
                page_size=page_size,
                order_by=order_by,
                order_dir=order_dir,
            )
            yield from result.items
            if not result.has_more:
                return
            page += 1

    def get_values(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int],
        key_by: Literal["name", "prompt_id"] = "name",
    ) -> dict[int, dict[str | int, Any]]:
        """Fetch extraction values as plain dicts: ``{document_id: {field: value}}``.

        The happy-path last mile over ``iterate(prompts=...)``: each document's
        extraction items are flattened to ``parsed_value`` keyed by prompt name
        (or by ``prompt_id`` with ``key_by="prompt_id"``; items without a name
        also fall back to the id). Traceback/confidence/issues are dropped —
        use ``iterate``/``search`` directly for the audit path. Raises
        ``ValueError`` if two prompts share a name within one document.
        """
        out: dict[int, dict[str | int, Any]] = {}
        for doc in self.iterate(selectors, document_ids=document_ids, prompts=prompts):
            out[doc.id] = _flatten_extractions(doc, key_by)
        return out

    def move_to_folder(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        target_folder_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> MoveResult:
        """Move documents into a folder.

        Target them with ``selectors`` or, as a shortcut, ``document_ids``. Omit
        ``target_folder_id`` (or pass ``None``) to move them to the root.
        """
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {"selectors": selectors}
        if target_folder_id is not None:
            body["target_folder_id"] = target_folder_id
        raw = self._http.request(
            "POST",
            "/api/documents/move-to-folder",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return MoveResult.model_validate(raw)

    def bulk_delete(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> BulkDeleteResult:
        """Delete documents matched by ``selectors`` (or ``document_ids``).

        Pass ``dry_run=True`` to get the matched count without deleting anything.
        """
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {"selectors": selectors, "dry_run": dry_run}
        raw = self._http.request(
            "POST",
            "/api/documents/bulk_delete",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return BulkDeleteResult.model_validate(raw)

    def delete(self, document_id: int) -> None:
        self._http.request("DELETE", f"/api/documents/{document_id}")


class AsyncDocumentsResource:
    def __init__(self, http: AsyncHttpClient) -> None:
        self._http = http

    async def upload(
        self,
        files: PathLike | Sequence[PathLike],
        *,
        flatten: bool = True,
        project_id: int | None = None,
        project_name: str | None = None,
        tag_ids: Sequence[int] | None = None,
        tag_names: Sequence[str] | None = None,
        content_type: str | None = None,
        on_duplicate: OnDuplicate = "allow",
        idempotency_key: str | None = None,
        wait: bool = False,
        max_wait_seconds: float | None = DEFAULT_DOCUMENT_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> UploadResult:
        """Async :meth:`DocumentsResource.upload`."""
        raw_inputs: list[PathLike] = (
            [files] if isinstance(files, (str, os.PathLike)) else list(files)
        )
        paths = [Path(raw) for raw in raw_inputs]
        if not paths:
            raise ValueError("upload() requires at least one file")
        if len(paths) > MAX_BATCH_FILES:
            raise ValueError(
                f"upload() accepts at most {MAX_BATCH_FILES} files per call (got "
                f"{len(paths)}); split your files into batches of {MAX_BATCH_FILES} "
                "or fewer and call upload() once per batch."
            )

        for path in paths:
            if not path.is_file():
                raise FileNotFoundError(str(path))

        remote_names = _derive_remote_names(paths, raw_inputs, flatten=flatten)
        result = await self._upload_batch(
            paths,
            remote_names,
            project_id=project_id,
            project_name=project_name,
            tag_ids=tag_ids,
            tag_names=tag_names,
            content_type=content_type,
            on_duplicate=on_duplicate,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        if wait:
            await self.wait_until_ready(
                result.document_ids,
                max_wait_seconds=max_wait_seconds,
                poll_interval=poll_interval,
            )
        return result

    async def _upload_batch(
        self,
        paths: Sequence[Path],
        remote_names: Sequence[str],
        *,
        project_id: int | None,
        project_name: str | None,
        tag_ids: Sequence[int] | None,
        tag_names: Sequence[str] | None,
        content_type: str | None,
        on_duplicate: OnDuplicate = "allow",
        idempotency_key: str,
    ) -> UploadResult:
        """Plan one batch and PUT each file's bytes (the core of upload)."""
        by_name: dict[str, Path] = {}
        request_files = []
        for path, name in zip(paths, remote_names, strict=True):
            guessed = content_type or mimetypes.guess_type(path.name)[0] or _DEFAULT_CONTENT_TYPE
            by_name[name] = path
            # Offload the blocking stat + whole-file md5 read to threads so they
            # don't freeze the event loop (matches the read_bytes offload below).
            st = await asyncio.to_thread(path.stat)
            md5 = await asyncio.to_thread(_md5_hex, path)
            request_files.append(
                {
                    "file_name": name,
                    "content_type": guessed,
                    "size": st.st_size,
                    "md5": md5,
                }
            )

        body: dict[str, Any] = {"files": request_files}
        if project_id is not None:
            body["project_id"] = project_id
        if project_name is not None:
            body["project_name"] = project_name
        if tag_ids:
            body["tag_ids"] = list(tag_ids)
        if tag_names:
            body["tag_names"] = list(tag_names)

        raw = await self._http.request(
            "POST", "/api/uploads", json=body, idempotency_key=idempotency_key
        )
        response = BatchUploadResponse.model_validate(raw)

        outcomes: list[FileOutcome] = []
        for item in response.items:
            outcome = FileOutcome(
                file_name=item.client_file_name,
                document_id=item.document_id,
                duplicates=item.duplicates,
            )
            if item.put_url:
                if item.duplicates and on_duplicate == "block":
                    # Known content duplicate: skip the byte upload and remove
                    # the placeholder document the plan created for it.
                    if item.document_id is not None:
                        await self.delete(item.document_id)
                    outcome.skipped_as_duplicate = True
                else:
                    if item.duplicates and on_duplicate == "notify":
                        logger.warning(
                            "upload %r matches existing document(s) %s",
                            item.client_file_name,
                            [d.document_id for d in item.duplicates],
                        )
                    src_path = by_name.get(item.client_file_name)
                    if src_path is not None:
                        # Offload the blocking disk read to a thread so it
                        # doesn't freeze the event loop while reading the file.
                        data = await asyncio.to_thread(src_path.read_bytes)
                        await self._http.put_presigned(item.put_url, data, item.required_headers)
                        outcome.uploaded = True
            else:
                outcome.error = item.error
            outcomes.append(outcome)

        return UploadResult(outcomes=outcomes, project=response.project, tags=response.tags)

    async def upload_many(
        self,
        files: PathLike | Sequence[PathLike],
        *,
        pattern: str = "**/*",
        flatten: bool = True,
        batch_size: int = MAX_BATCH_FILES,
        project_id: int | None = None,
        project_name: str | None = None,
        tag_ids: Sequence[int] | None = None,
        tag_names: Sequence[str] | None = None,
        content_type: str | None = None,
        on_duplicate: OnDuplicate = "allow",
        idempotency_key: str | None = None,
        on_batch: Callable[[UploadResult], None] | None = None,
        wait: bool = False,
        max_wait_seconds: float | None = DEFAULT_DOCUMENT_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> UploadResult:
        """Async :meth:`DocumentsResource.upload_many` (``on_batch`` stays a
        plain callable)."""
        paths, names = _collect_upload_inputs(files, pattern=pattern, flatten=flatten)
        if not 1 <= batch_size <= MAX_BATCH_FILES:
            raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_FILES}")

        base_key = idempotency_key or new_idempotency_key()
        outcomes: list[FileOutcome] = []
        project: Project | None = None
        tags_by_id: dict[int, Tag] = {}
        eff_project_id, eff_project_name = project_id, project_name
        eff_tag_ids, eff_tag_names = tag_ids, tag_names

        for batch_no, start in enumerate(range(0, len(paths), batch_size)):
            try:
                result = await self._upload_batch(
                    paths[start : start + batch_size],
                    names[start : start + batch_size],
                    project_id=eff_project_id,
                    project_name=eff_project_name,
                    tag_ids=eff_tag_ids,
                    tag_names=eff_tag_names,
                    content_type=content_type,
                    on_duplicate=on_duplicate,
                    idempotency_key=f"{base_key}-{batch_no:04d}",
                )
            except Exception as exc:
                partial = UploadResult(
                    outcomes=outcomes, project=project, tags=list(tags_by_id.values())
                )
                raise UploadManyError(
                    f"upload_many() batch {batch_no + 1} failed; {batch_no} batch(es) "
                    "were already uploaded (see .partial)",
                    partial=partial,
                    batches_completed=batch_no,
                ) from exc

            outcomes.extend(result.outcomes)
            if result.project is not None:
                project = result.project
                eff_project_id, eff_project_name = project.id, None
            if result.tags:
                for tag in result.tags:
                    tags_by_id[tag.id] = tag
                eff_tag_ids = [t.id for t in tags_by_id.values()]
                eff_tag_names = None
            if on_batch is not None:
                on_batch(result)

        total = UploadResult(outcomes=outcomes, project=project, tags=list(tags_by_id.values()))
        if wait:
            await self.wait_until_ready(
                total.document_ids,
                max_wait_seconds=max_wait_seconds,
                poll_interval=poll_interval,
            )
        return total

    async def wait_until_ready(
        self,
        document_ids: Sequence[int],
        *,
        max_wait_seconds: float | None = DEFAULT_DOCUMENT_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> list[DocumentSummary]:
        """Async :meth:`DocumentsResource.wait_until_ready`."""
        ids = list(document_ids)
        if not ids:
            return []

        async def fetch() -> list[DocumentSummary]:
            return [d async for d in self.iterate(document_ids=ids)]

        seen_unknown: set[str] = set()

        def ready(docs: list[DocumentSummary]) -> bool:
            ready_ids = set()
            for d in docs:
                warn_once_unknown_status(
                    seen_unknown, d.status.value, _KNOWN_DOCUMENT_STATUS_VALUES, "document"
                )
                if d.status == DocumentStatus.ready:
                    ready_ids.add(d.id)
            return all(i in ready_ids for i in ids)

        return await apoll_until(
            fetch,
            ready,
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
            timeout_message=f"documents {ids} did not all become ready within {max_wait_seconds}s",
        )

    async def download_url(self, document_id: int) -> DownloadResponse:
        """Async :meth:`DocumentsResource.download_url`."""
        raw = await self._http.request("GET", f"/api/documents/{document_id}/download")
        return DownloadResponse.model_validate(raw)

    async def download(
        self,
        document_id: int,
        dest_dir: PathLike,
        *,
        file_name: str | None = None,
        overwrite: bool = False,
    ) -> Path:
        """Async :meth:`DocumentsResource.download` (file written off-loop)."""
        directory = Path(dest_dir)
        if not directory.is_dir():
            raise NotADirectoryError(f"dest_dir does not exist or is not a directory: {directory}")
        if file_name is None:
            file_name = (await self.get(document_id)).name
        target = _resolve_download_target(directory, file_name)
        if target.exists() and not overwrite:
            raise FileExistsError(str(target))

        signed = await self.download_url(document_id)
        data = await self._http.get_presigned(signed.url)
        # Blocking disk write off the event loop, like the upload-side reads.
        await asyncio.to_thread(target.write_bytes, data)
        return target

    async def get(
        self, document_id: int, *, prompt_ids: Sequence[int] | None = None
    ) -> DocumentSummary:
        params = None
        if prompt_ids:
            params = {"prompt_ids": ",".join(str(p) for p in prompt_ids)}
        raw = await self._http.request("GET", f"/api/documents/{document_id}", params=params)
        return DocumentSummary.model_validate(raw)

    async def search(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> DocumentPage:
        selectors = _resolve_selectors(selectors, document_ids, required=False)
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if selectors is not None:
            body["selectors"] = selectors
        if prompts:
            body["prompts"] = list(prompts)
        raw = await self._http.request("POST", "/api/documents/search", json=body, idempotent=True)
        return DocumentPage.model_validate(raw)

    async def iterate(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> AsyncIterator[DocumentSummary]:
        """Yield every matching document, fetching pages on demand."""
        selectors = _resolve_selectors(selectors, document_ids, required=False)
        page = 1
        while True:
            result = await self.search(
                selectors,
                prompts=prompts,
                page=page,
                page_size=page_size,
                order_by=order_by,
                order_dir=order_dir,
            )
            for item in result.items:
                yield item
            if not result.has_more:
                return
            page += 1

    async def get_values(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int],
        key_by: Literal["name", "prompt_id"] = "name",
    ) -> dict[int, dict[str | int, Any]]:
        """Async :meth:`DocumentsResource.get_values`."""
        out: dict[int, dict[str | int, Any]] = {}
        async for doc in self.iterate(selectors, document_ids=document_ids, prompts=prompts):
            out[doc.id] = _flatten_extractions(doc, key_by)
        return out

    async def move_to_folder(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        target_folder_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> MoveResult:
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {"selectors": selectors}
        if target_folder_id is not None:
            body["target_folder_id"] = target_folder_id
        raw = await self._http.request(
            "POST",
            "/api/documents/move-to-folder",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return MoveResult.model_validate(raw)

    async def bulk_delete(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        dry_run: bool = False,
        idempotency_key: str | None = None,
    ) -> BulkDeleteResult:
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {"selectors": selectors, "dry_run": dry_run}
        raw = await self._http.request(
            "POST",
            "/api/documents/bulk_delete",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return BulkDeleteResult.model_validate(raw)

    async def delete(self, document_id: int) -> None:
        await self._http.request("DELETE", f"/api/documents/{document_id}")
