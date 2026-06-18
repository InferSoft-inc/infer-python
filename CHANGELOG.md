# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Nothing yet.

## [0.1.1] - 2026-06-18

### Added
- Transport-level resilience: connection failures and timeouts are now retried on the
  same budget/backoff as transient status codes, and — once the budget is exhausted —
  raised as `APITimeoutError` / `APIConnectionError` (both under `InfersoftError`)
  instead of leaking raw `httpx` exceptions. The original error is preserved as
  `__cause__`.
- `documents.upload_many()` (sync and async) — explicitly-bulk upload that chunks any
  number of files into batches of up to `MAX_BATCH_FILES` (`upload()` stays strict).
  Accepts a directory (globbed via `pattern`, hidden files skipped; `flatten=False`
  replicates its layout), validates duplicate names across the whole set, threads a
  `project_name`-created project's id across batches, derives per-batch idempotency
  keys (`<base>-0000`, …) for resumable re-runs, reports progress via `on_batch`, and
  raises the new `UploadManyError` (with `.partial` / `.batches_completed`) on a
  mid-run failure.
- `client.extract()` / `AsyncClient.extract()` — the end-to-end pipeline in one call:
  upload (or take `document_ids`/`selectors`) → wait ready → run the extractor (with
  `max_credits` passthrough) → wait for completion → documents plus values flattened to
  `{document_id: {field: parsed_value}}` (`ExtractResult`). A `failed` job raises the new
  `ExtractError` carrying the partial state (`.job`, `.upload`) for manual resumption;
  `partial_success` returns normally.
- Convenience composites (sync and async): `jobs.run(max_credits=…)` budget guard
  raising the new `CreditsLimitExceededError` (carries the estimate; job not started),
  `folders.ensure_path(path)` returning the leaf folder, `projects.get_or_create(name)`
  with creation-race recovery, `jobs.results(job, prompts=…)` correlating a job back to
  its documents and extraction values, `documents.get_values(...)` flattening extraction
  results to `{document_id: {field: parsed_value}}`, and a `DocumentSummary.extractions`
  accessor keyed by prompt id.
- `with_options(timeout=…, max_retries=…)` on `Client` and `AsyncClient` — returns a
  cheap copy sharing the connection pool and cached token, applying the overrides to
  its requests (e.g. a long timeout for one heavy call, or `max_retries=0` to fail
  fast). Closing a copy is a no-op, so copies are safe as context managers; the
  override covers API requests (token acquisition and presigned upload PUTs keep
  their own timeouts).
- `AsyncClient` — full async parity with `Client` (same constructor and resources;
  every method is a coroutine, `iterate(...)` is an async generator, plus `aclose()`
  and async context-manager support). Shares the auth, retry, idempotency, pagination,
  and waiter behavior via httpx's async transport.
- `documents.move_to_folder` and `documents.bulk_delete`, completing the
  documents resource (`MoveResult`, `BulkDeleteResult` models).
- `projects.assign_documents`, completing the projects resource
  (`AssignDocumentsResult` model).
- `client.folders` resource — `create`, `get`, `rename`, `search`, `iterate`, `move`,
  `bulk_delete`, `resolve_paths` (`Folder`, `FolderPage`, `FolderMoveResult`,
  `FolderBulkDeleteResult`, `FolderPathResult`, `FolderPathsResult` models).
- `client.prompts` resource — `search`, `iterate` (`PromptMeta`, `PromptPage`,
  `DataTypeName` models).
- Typed extraction results: `DocumentSummary.extraction_results` is now
  `list[DocumentExtractionResultItem]` (with `ExtractionResultValue`,
  `ExtractionTracebackItem`, `ExtractionTracebackBBox`), populated when prompt IDs
  are passed to `documents.search` / `documents.get`.
- Selector builders: `build_*_selector` constructors for all 14 API selectors plus
  `build_selectors(include=, exclude=)`, exported at the top level (and under
  `infersoft.selectors`), so `selectors` payloads no longer need raw dicts.
- `document_ids=` shortcut on every selector-taking method (`documents.search`,
  `move_to_folder`, `bulk_delete`, `projects.assign_documents`, `jobs.estimate`/`run`)
  — wraps a list of IDs into a file selector. Mutually exclusive with `selectors`.
- Polling waiters for async work: `jobs.wait()` / `jobs.run(wait=True)` (poll to a
  terminal status) and `documents.wait_until_ready()` / `documents.upload(wait=True)`
  (poll until documents are `ready`). `max_wait_seconds=None`/`0` waits indefinitely;
  a positive budget raises the new `WaitTimeoutError`. Cadence via `poll_interval`.
- OAuth2 client-credentials auth with automatic token caching and refresh.
- Resources: `documents` (`upload`/`get`/`search`/`iterate`/`delete`), `projects`
  (`create`/`get`/`search`/`iterate`), and `jobs` (`estimate`/`start`/`run`/`get`/
  `search`/`iterate`).
- End-to-end `documents.upload()` that hides the presign + S3 PUT choreography, with a
  `flatten` switch controlling shared-basename handling.
- Auto-paginating `iterate()` generators on every list resource.
- `request_id` captured from response headers (and the S3 `x-amz-*` id) and surfaced on
  `APIError` / `UploadTransferError`. The API does not emit one yet; the attribute
  populates automatically once it does.
- Retry of idempotent requests on transient failures (408/429/5xx) with exponential
  backoff + full jitter; `Retry-After` honored.
- Writes (`upload`, `projects.create`, `jobs.estimate`, `jobs.start`) send an
  auto-generated `Idempotency-Key` so the server deduplicates retries, making them
  safe to retry. Each method accepts an optional `idempotency_key` to reuse a key
  across process restarts. Read-only POST searches are retried without a key.
- Logging via the stdlib `"infersoft"` logger (HTTP retries logged at DEBUG).

### Changed
- Response models now use pydantic `extra="allow"` (was `extra="ignore"`), so unknown
  server fields are preserved and reachable via `model_extra`.
