# API reference

Every public method of the Infersoft Python SDK, mapped to the REST endpoint(s) it
calls. Parameter-level documentation lives in each method's docstring (your IDE shows
it on hover); this file is the map of what exists and what it does on the wire.

The async client (`AsyncClient`) mirrors this surface 1:1 — every method below exists
with identical signatures and semantics, awaited (`await client.documents.upload(...)`,
`async for` on iterators).

Conventions used below:

- **write** — sends an `Idempotency-Key` (auto-generated per call; pass
  `idempotency_key=` to control it) and is retried safely on transient failures.
- **read** — retried automatically; never keyed.
- **write (naturally idempotent)** — a mutation that converges on retry
  (a DELETE, a get-or-create); retried safely without a key.
- **composite** — a client-side workflow; every wire call it makes is listed.

## Client

- `Client(client_id=, client_secret=, base_url=, token_url=, audience=, scope=, timeout=30.0, upload_timeout=300.0, max_retries=2, upload_max_retries=None)`
  — construction; credentials/endpoints fall back to `INFERSOFT_*` env vars. OAuth2
  client-credentials tokens are fetched lazily and cached.
- `client.with_options(timeout=, max_retries=, upload_max_retries=) -> Client`
  — a copy with per-call-site overrides; shares the connection pool and token cache.
- `client.extract(files=None, *, document_ids=None, selectors=None, prompts, project_id=None, project_name=None, flatten=True, max_credits=None, key_by="name", raise_on_failure=True, max_wait_seconds=1800.0, poll_interval=2.0) -> ExtractResult`
  — **composite**: the whole pipeline. Wire calls: when `files=` is used,
  `POST /api/uploads` + presigned PUTs + a polled `POST /api/documents/search`
  readiness wait; then always `POST /api/jobs/credits/estimate`,
  `POST /api/jobs/start`, polled `GET /api/jobs/{id}`, and paged
  `POST /api/documents/search` with `prompts` (results).
- `client.close()` / `async client.aclose()` — release the pools (also usable as a
  context manager).

## Documents

- `client.documents.upload(files, *, flatten=True, project_id=None, project_name=None, tag_ids=None, tag_names=None, content_type=None, on_duplicate="allow", idempotency_key=None, wait=False, max_wait_seconds=600.0, poll_interval=2.0) -> UploadResult`
  — `on_duplicate` is `"allow"` (default) / `"notify"` / `"block"` for content matches (see README → Uploads).
  — **write/composite**: `POST /api/uploads` (plans the batch, ≤100 files), then one
  presigned `PUT` per file (unauthenticated, to S3); with `wait=True`, polled
  `POST /api/documents/search` until every document is `ready`.
- `client.documents.upload_many(files, *, pattern="**/*", flatten=True, batch_size=100, ..., on_batch=None, wait=False, ...) -> UploadResult`
  — **write/composite**: bulk uploads of any size; directory globbing; one
  `upload` batch per `batch_size` files with deterministic per-batch keys derived from
  the base `idempotency_key`.
- `client.documents.wait_until_ready(document_ids, *, max_wait_seconds=600.0, poll_interval=2.0) -> list[DocumentSummary]`
  — **read**: polled `POST /api/documents/search` until all are `ready`.
- `client.documents.download_url(document_id) -> DownloadResponse`
  — **read**: `GET /api/documents/{id}/download` (short-lived signed URL + `expires_at`).
- `client.documents.download(document_id, dest_dir, *, file_name=None, overwrite=False) -> Path`
  — **composite**: `GET /api/documents/{id}` (name, skipped when `file_name=` given) +
  `GET /api/documents/{id}/download` + a signed `GET` for the bytes (unauthenticated);
  writes the file into `dest_dir`.
- `client.documents.get(document_id, *, prompt_ids=None) -> DocumentSummary`
  — **read**: `GET /api/documents/{id}` (`?prompt_ids=` populates `extraction_results`).
- `client.documents.search(selectors=None, *, document_ids=None, prompts=None, page=1, page_size=50, order_by="id", order_dir="asc") -> DocumentPage`
  — **read**: `POST /api/documents/search`.
- `client.documents.iterate(selectors=None, *, document_ids=None, prompts=None, page_size=50, ...) -> Iterator[DocumentSummary]`
  — **read**: paged `POST /api/documents/search`, fetched on demand.
- `client.documents.get_values(selectors=None, *, document_ids=None, prompts, key_by="name") -> dict[int, dict[str | int, Any]]`
  — **read/composite**: paged `POST /api/documents/search` with `prompts`, flattened
  to `{document_id: {field: parsed_value}}`.
- `client.documents.move_to_folder(selectors=None, *, document_ids=None, target_folder_id=None, idempotency_key=None) -> MoveResult`
  — **write**: `POST /api/documents/move-to-folder`.
- `client.documents.bulk_delete(selectors=None, *, document_ids=None, dry_run=False, idempotency_key=None) -> BulkDeleteResult`
  — **write**: `POST /api/documents/bulk_delete`.
- `client.documents.delete(document_id) -> None`
  — **write (naturally idempotent)**: `DELETE /api/documents/{id}`.

## Folders

- `client.folders.create(name, *, parent_id=None, idempotency_key=None) -> Folder`
  — **write**: `POST /api/folders`.
- `client.folders.get(folder_id) -> Folder` — **read**: `GET /api/folders/{id}`.
- `client.folders.rename(folder_id, name, *, idempotency_key=None) -> Folder`
  — **write**: `PATCH /api/folders/{id}`.
- `client.folders.search(*, parent_id=None, page=1, page_size=50, ...) -> FolderPage`
  — **read**: `POST /api/folders/search`.
- `client.folders.iterate(*, parent_id=None, page_size=50, ...) -> Iterator[Folder]`
  — **read**: paged `POST /api/folders/search`.
- `client.folders.move(folder_ids, *, target_parent_id=None, idempotency_key=None) -> FolderMoveResult`
  — **write**: `POST /api/folders/move`.
- `client.folders.bulk_delete(folder_ids, *, idempotency_key=None) -> FolderBulkDeleteResult`
  — **write**: `POST /api/folders/bulk_delete`.
- `client.folders.resolve_paths(paths, *, parent_id=None) -> FolderPathsResult`
  — **write (naturally idempotent)**: `POST /api/folders/paths` — get-or-create by
  path; the server creates missing segments and returns the folders per path.
- `client.folders.ensure_path(path, *, parent_id=None) -> Folder`
  — **composite** over `resolve_paths`: a single `POST /api/folders/paths` (the
  server creates any missing segments); returns the leaf folder.

## Projects

- `client.projects.create(name, *, idempotency_key=None) -> Project`
  — **write**: `POST /api/projects`.
- `client.projects.get(project_id) -> Project` — **read**: `GET /api/projects/{id}`.
- `client.projects.get_or_create(name, *, idempotency_key=None) -> Project`
  — **composite**: `POST /api/projects/search` (exact-name match), then
  `POST /api/projects` when absent (recovers the existing project on a creation race).
- `client.projects.search(q=None, *, page=1, page_size=50, ...) -> ProjectPage`
  — **read**: `POST /api/projects/search`.
- `client.projects.iterate(q=None, *, page_size=50, ...) -> Iterator[Project]`
  — **read**: paged `POST /api/projects/search`.
- `client.projects.assign_documents(selectors=None, *, document_ids=None, project_id=None, project_name=None, idempotency_key=None) -> AssignDocumentsResult`
  — **write**: `POST /api/projects/assign-documents`.

## Prompts

- `client.prompts.search(q=None, *, document_class=None, include_deleted=False, page=1, page_size=50, ...) -> PromptPage`
  — **read**: `POST /api/prompts/search`.
- `client.prompts.iterate(q=None, *, document_class=None, include_deleted=False, ...) -> Iterator[PromptMeta]`
  — **read**: paged `POST /api/prompts/search`.

## Jobs

- `client.jobs.estimate(*, step, selectors=None, document_ids=None, prompts=None, synchronous=False, idempotency_key=None) -> CreditsEstimate`
  — **write**: `POST /api/jobs/credits/estimate`.
- `client.jobs.start(credits_id, *, project_id=None, idempotency_key=None) -> Job`
  — **write**: `POST /api/jobs/start`.
- `client.jobs.run(*, step, selectors=None, document_ids=None, prompts=None, synchronous=False, project_id=None, max_credits=None, wait=False, max_wait_seconds=1800.0, poll_interval=2.0) -> Job`
  — **composite**: `POST /api/jobs/credits/estimate` + `POST /api/jobs/start`
  (gated by `max_credits`); with `wait=True`, polled `GET /api/jobs/{id}`.
- `client.jobs.get(job_id) -> Job` — **read**: `GET /api/jobs/{id}`.
- `client.jobs.wait(job, *, max_wait_seconds=1800.0, poll_interval=2.0) -> Job`
  — **read**: polled `GET /api/jobs/{id}` until a terminal status.
- `client.jobs.results(job, *, prompts=None, page_size=50, ...) -> Iterator[DocumentSummary]`
  — **read/composite**: paged `POST /api/documents/search` with a job selector (and
  `prompts` to populate extraction values).
- `client.jobs.search(*, statuses=None, created_from=None, created_to=None, page=1, page_size=50, order_by="id", order_dir="desc") -> JobPage`
  — **read**: `POST /api/jobs/search`.
- `client.jobs.iterate(*, statuses=None, created_from=None, created_to=None, ...) -> Iterator[Job]`
  — **read**: paged `POST /api/jobs/search`.

## Selector builders

Top-level helpers that build the selector dicts accepted by every `selectors=`
parameter. Combine them with `build_selectors`:

```python
from infersoft import build_selectors, build_folder_selector, build_created_at_selector

selectors = build_selectors(
    include=[build_folder_selector(7), build_created_at_selector(created_from="2026-01-01")],
)
```

- `build_selectors(*, include=None, exclude=None) -> dict`
- `build_file_selector(ids)` · `build_folder_selector(folder_id)` ·
  `build_job_selector(ids)` · `build_project_selector(project_id, *, added_from=None, added_to=None)` ·
  `build_tag_selector(ids, *, tagged_from=None, tagged_to=None)` ·
  `build_source_document_selector(ids)` · `build_name_selector(value)` ·
  `build_document_class_selector(classes)` ·
  `build_created_at_selector(*, created_from=None, created_to=None)` ·
  `build_page_count_selector(*, page_count_from=None, page_count_to=None)` ·
  `build_size_selector(*, size_from=None, size_to=None)` ·
  `build_is_valid_selector()` · `build_has_children_selector()` ·
  `build_has_running_workflow_selector()`
