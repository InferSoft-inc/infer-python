# InferSoft Python SDK

[![CI](https://github.com/InferSoft-inc/infer-python/actions/workflows/ci.yml/badge.svg)](https://github.com/InferSoft-inc/infer-python/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/infersoft)](https://pypi.org/project/infersoft/)
[![Python](https://img.shields.io/pypi/pyversions/infersoft)](https://pypi.org/project/infersoft/)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](https://github.com/InferSoft-inc/infer-python/blob/main/LICENSE)

Python client for the [InferSoft](https://platform.infersoft.com) document-processing REST API.

Full endpoint reference: [api.md](https://github.com/InferSoft-inc/infer-python/blob/main/api.md) — every method, mapped to the REST calls it makes.

## Install

```bash
pip install infersoft
```

Requires Python 3.10+. Two runtime dependencies: `httpx` and `pydantic` v2.

## Authentication

The SDK uses OAuth2 **client credentials** — create them for your
organization at [platform.infersoft.com](https://platform.infersoft.com).
The SDK fetches a bearer token,
caches it until shortly before expiry, and refreshes automatically. Configure
via constructor arguments or environment variables:

| Argument        | Env var                    | Default                                                |
| --------------- | -------------------------- | ------------------------------------------------------ |
| `client_id`     | `INFERSOFT_CLIENT_ID`      | — (required)                                           |
| `client_secret` | `INFERSOFT_CLIENT_SECRET`  | — (required)                                           |
| `base_url`      | `INFERSOFT_BASE_URL`       | `https://api.infersoft.com`                            |
| `token_url`     | `INFERSOFT_TOKEN_URL`      | https://dev-noabnisxxguu0jp0.us.auth0.com/oauth/token  |
| `audience`      | `INFERSOFT_AUDIENCE`       | `https://api.infersoft.com`                            |
| `scope`         | — (constructor only)       | none (optional OAuth scopes)                           |

## Quickstart

```python
from infersoft import Client

# Credentials can also come from INFERSOFT_CLIENT_ID / INFERSOFT_CLIENT_SECRET.
client = Client(client_id="...", client_secret="...")

# The whole pipeline in one call: upload -> wait ready -> run the extractor ->
# wait for completion -> values as plain dicts {document_id: {field: value}}.
result = client.extract(["invoice.pdf", "receipt.pdf"], prompts=[553])
print(result.values)              # {42: {"Total Amount": 1234.5}, 43: {...}}
print(result.job.status.value)    # "completed" / "partial_success"

# Files the SERVER rejects (e.g. name conflicts) are skipped, not raised —
# values covers only the files that made it. Check before trusting the result:
if result.upload and result.upload.failed:
    for f in result.upload.failed:
        print(f"skipped {f.file_name}: {f.error.code if f.error else 'upload failed'}")

# Also works over documents you already have (or any selectors):
result = client.extract(document_ids=[42, 43], prompts=[553], max_credits=500)
```

A **prompt** is a field extractor configured in your InferSoft workspace
(e.g. "Total Amount"); jobs run one **step** at a time (`"splitter"`,
`"classifier"`, or `"extractor"`). List your prompts and their IDs with:

```python
for p in client.prompts.iterate():
    print(p.id, p.name)
```

Six runnable end-to-end programs live in [examples/](https://github.com/InferSoft-inc/infer-python/tree/main/examples)
— from the quickstart to a resumable bulk import, CSV export, concurrent
downloads, and production-shaped error handling — all reading credentials
from the `INFERSOFT_*` environment variables above.

Every step is also available individually:

```python
# Upload handles the full presign -> PUT choreography for you.
# Pass a single path or a list (up to MAX_BATCH_FILES per call).
result = client.documents.upload(["invoice.pdf", "receipt.pdf"], project_name="Q1 Invoices")
print(result.document_ids)        # [42, 43]
print(result.failed)              # files the server reported as conflicts/errors

# Run a single-stage extraction job over those documents.
job = client.jobs.run(step="extractor", document_ids=result.document_ids, prompts=[553])
print(job.id, job.status)
```

## The extract pipeline

`client.extract(...)` is the end-to-end composite — upload (optional) → wait
until ready → estimate credits → start the job → wait for completion → fetch
values — returning an `ExtractResult` with `.values`
(`{document_id: {field: parsed_value}}`), `.documents` (full typed extraction
items), `.job`, and `.upload`.

**Inputs.** Exactly one of `files=`, `document_ids=`, or `selectors=`.
`project_name=` requires `files=` (the upload is what creates the project);
`flatten` passes through to the upload. `key_by="prompt_id"` keys values by
prompt id instead of field name (use it when two prompts share a name —
colliding names raise `ValueError`).

**Budget gate.** `max_credits=` aborts between estimate and start when the
estimate is too high, raising `CreditsLimitExceededError` with the estimate
attached — the job is never started (the estimate call itself has already
run).

**Failure semantics.**

| Outcome | Behavior |
|---------|----------|
| The server rejects some files (e.g. name conflicts) | Returns normally; `values` covers the survivors — check `result.upload.failed` |
| The server rejects every file | Raises `ExtractError` (nothing to run) |
| A file transfer fails after retries | Raises `UploadTransferError` mid-pipeline — no result; documents already planned may exist server-side |
| Job ends `failed` | Raises `ExtractError` with `.job`/`.upload` attached (pass `raise_on_failure=False` to get the result instead) |
| Job ends `partial_success` | Returns normally — inspect `result.job.error_count` and per-document statuses |
| A wait times out | Raises `WaitTimeoutError`; uploaded documents and the running job are NOT rolled back |

Recovery: an `ExtractError` carries the state to resume manually —
`client.jobs.wait(error.job)` to keep waiting, `client.jobs.results(error.job)`
for values once it finishes. A `WaitTimeoutError` carries no handles — find the
job again with `client.jobs.search(statuses=["running"])`. `max_wait_seconds`
is a **per-phase** budget (upload readiness and job completion each get it),
defaulting to 30 minutes per phase.

## Async

`AsyncClient` mirrors `Client` exactly — same constructor and methods, every call
awaited, and `iterate(...)` yields with `async for`:

```python
import asyncio
from infersoft import AsyncClient

async def main():
    async with AsyncClient(client_id="...", client_secret="...") as client:
        result = await client.documents.upload(["invoice.pdf"], wait=True)
        job = await client.jobs.run(
            step="extractor", document_ids=result.document_ids, prompts=[553], wait=True
        )
        async for doc in client.documents.iterate(document_ids=result.document_ids, prompts=[553]):
            print(doc.id, doc.extraction_results)

asyncio.run(main())
```

Concurrency is the point — fan out independent calls with `asyncio.gather`:

```python
# Inside an async function:
paths = await asyncio.gather(
    *(client.documents.download(doc_id, "exports/") for doc_id in ids)
)
```

File reads and writes inside upload/download run off the event loop
(`asyncio.to_thread`), so heavy file I/O never blocks your other tasks.

## Uploads

`upload()` plans a batch with the API, then PUTs each file's bytes straight to
storage via presigned URLs — one call, the whole choreography. It also sends an
md5 of each file's contents so the server can flag uploads that duplicate
documents you already have. Per-file outcomes land on the result:

```python
result = client.documents.upload(["invoice.pdf", "receipt.pdf"], project_name="Q1 Invoices")
print(result.document_ids)        # [42, 43]
print(result.failed)              # files the server reported as conflicts/errors
```

**Duplicates.** When a file's content matches a document you already have, the
match is reported on `outcome.duplicates` (a list of `UploadDuplicate`). The
`on_duplicate` argument controls what `upload()`/`upload_many()` *does* about it:

- `"allow"` (default) — upload anyway; the match is still reported.
- `"notify"` — upload anyway, and log a warning naming the matched document(s).
- `"block"` — skip the byte upload for duplicates and delete the placeholder the
  plan created; those files land on `result.skipped` (not `result.failed`).

```python
result = client.documents.upload(paths, on_duplicate="block")
print([o.file_name for o in result.skipped])   # duplicates that were not uploaded
```

**File names and folder layout.** Each file is stored under the name you send.
By default (`flatten=True`) only the basename is sent, and the call raises
`ValueError` if two files in the batch share one:

```python
# Raises ValueError: a/report.pdf and b/report.pdf both flatten to "report.pdf".
client.documents.upload(["a/report.pdf", "b/report.pdf"])
```

Pass `flatten=False` to send each path exactly as given — no normalization —
so your local folder layout is replicated in the app:

```python
# Stored as "2024/jan/report.pdf" and "2024/feb/report.pdf".
client.documents.upload(["2024/jan/report.pdf", "2024/feb/report.pdf"], flatten=False)
```

**Bulk imports.** A single `upload()` batch is capped at 100 files (it raises
`ValueError` beyond that — no silent chunking). For larger sets use the
explicitly-bulk `upload_many()`, which chunks into batches for you and also
accepts a whole directory (globbed via `pattern`; hidden files skipped):

```python
result = client.documents.upload_many(paths, project_name="Bulk import")
result = client.documents.upload_many("exports/2026", flatten=False)  # keeps layout

# Progress + a resumable idempotency key for re-runs.
client.documents.upload_many(
    paths,
    idempotency_key="import-2026-q1",   # batches derive -0000, -0001, ... keys
    on_batch=lambda r: print(f"uploaded {len(r.succeeded)} files"),
    wait=True,
)
```

With `project_name`, the first batch creates the project and later batches are
pinned to its id (no duplicate projects). A failing batch raises
`UploadManyError` carrying `.partial` (earlier batches are **not** rolled
back) and `.batches_completed`; re-running with the same `idempotency_key`, the
same files **in the same order**, and the same `batch_size` dedupes
batch-by-batch (keys are derived per batch position).

## Downloads

`download()` fetches a document's file into a folder and returns the written
path; `download_url()` returns the short-lived signed URL when you want the
bytes yourself:

```python
path = client.documents.download(42, "exports/")     # -> exports/<document name>
signed = client.documents.download_url(42)           # .url, .expires_at
```

The file name defaults to the document's server-side name (one extra lookup —
pass `file_name=` to skip it). Names are reduced to their final path component
and can never escape the destination directory, even for documents uploaded
with `flatten=False` paths. Existing files raise `FileExistsError` unless
`overwrite=True`; the destination directory must already exist.

Transfers retry like uploads (the `upload_max_retries` budget); a failed
download raises `DownloadTransferError`. Signed URLs expire quickly — if one
does (an HTTP 403), `download()` called again simply resolves a fresh one.

## Pagination

`search()` returns one explicit page (`.items`, `.page`, `.has_more`). For most
use cases prefer `iterate()`, which transparently fetches pages on demand:

```python
for doc in client.documents.iterate(order_dir="desc"):
    print(doc.id, doc.name)
```

## Waiting for completion

Jobs and document processing are asynchronous. Pass `wait=True` to block until
they finish, or call the waiters directly:

```python
# Upload and block until the documents are processable, then extract and wait.
result = client.documents.upload(["invoice.pdf"], wait=True)
job = client.jobs.run(step="extractor", document_ids=result.document_ids,
                      prompts=[553], wait=True)
print(job.status.value)  # "completed" / "failed" / "partial_success"

# Or wait explicitly (e.g. on a job you started elsewhere):
client.jobs.wait(job_id, max_wait_seconds=600)
client.documents.wait_until_ready([1, 2, 3])
```

**Deadlines.** Waits are finite by default — 30 minutes for job waits
(`jobs.wait`, `jobs.run(wait=True)`, `client.extract`), 10 minutes for
document readiness (`wait_until_ready`, `upload(wait=True)`,
`upload_many(wait=True)`) — and raise `WaitTimeoutError` once exceeded, so a
stuck resource fails loudly instead of hanging. A timeout does **not** cancel
the server-side work: the job keeps running, and you can resume waiting later
with `jobs.wait(job_id)`. Pass `max_wait_seconds=None` to wait without a
deadline; tune the cadence with `poll_interval` (default 2s).

**Forward compatibility.** Status fields are extensible: a status this SDK
build doesn't know yet parses fine and is treated as non-terminal — the wait
keeps polling, logs one warning naming the unknown value (so a hang is
diagnosable), and the deadline applies as usual. Upgrade the SDK when a new
terminal status matters to your code.

## Convenience helpers

Common multi-call patterns are bundled into single methods:

```python
from infersoft import CreditsLimitExceededError

# Budget guard: abort between estimate and start if the cost is too high.
try:
    job = client.jobs.run(step="extractor", document_ids=ids, prompts=[553], max_credits=500)
except CreditsLimitExceededError as e:
    print(f"would cost {e.estimate.total_credits} credits — not started")

# Get-or-create patterns for re-runnable scripts.
project = client.projects.get_or_create("Q1 Invoices")
folder = client.folders.ensure_path("2026/Q1")

# Correlate a finished job back to its documents and extraction values.
for doc in client.jobs.results(job, prompts=[553]):
    print(doc.name, doc.extractions[553].parsed_value)

# Or get values as plain dicts: {document_id: {field_name: parsed_value}}.
values = client.documents.get_values(document_ids=ids, prompts=[553])
```

## Errors

Every runtime failure — HTTP errors, transport problems, timeouts, pipeline
failures — raises a subclass of `InfersoftError`, so one
`except InfersoftError` is the safety net for things that can go wrong at run
time. Usage errors raise the standard library types instead: bad arguments are
`ValueError`, missing local files `FileNotFoundError`, download collisions
`FileExistsError` / `NotADirectoryError`. Catch specific classes when you
react programmatically:

```python
from infersoft import ConflictError, InfersoftError

try:
    project = client.projects.create("Q1 Invoices")
except ConflictError:
    # Name already exists — fetch it by exact name.
    project = client.projects.get_or_create("Q1 Invoices")
except InfersoftError as e:
    raise SystemExit(f"infersoft call failed: {e}")
```

HTTP errors (`APIError` and its subclasses) carry the parsed RFC 7807 problem
fields — `status_code`, `title`, `detail`, `type`, `instance` — plus
`request_id` (when the server returns one; include it in support requests) and
the raw `response`.

| Exception | Raised when | Key attributes |
|---|---|---|
| `InfersoftError` | Base class for everything below | — |
| `APIError` | A non-2xx problem response with no more specific class | RFC 7807 fields, `status_code`, `request_id` |
| `BadRequestError` | 400 — invalid request | as `APIError` |
| `UnauthorizedError` | 401 — bad or expired credentials at the API | as `APIError` |
| `ForbiddenError` | 403 — missing scope or permission | as `APIError` |
| `NotFoundError` | 404 | as `APIError` |
| `ConflictError` | 409 — deterministic conflicts (e.g. name already exists); transient in-flight 409s are retried automatically first | as `APIError` |
| `PreconditionFailedError` | 412 | as `APIError` |
| `PayloadTooLargeError` | 413 — e.g. a file over the API's size limit | as `APIError` |
| `UnprocessableEntityError` | 422 — e.g. an `idempotency_key` reused with different parameters | as `APIError` |
| `RateLimitError` | 429 once the retry budget is exhausted | as `APIError` |
| `ServerError` | 5xx — 500/502/503/504 after retries; others (501, …) immediately | as `APIError` |
| `AuthenticationError` | The OAuth token endpoint rejected the credentials or stayed unreachable | — |
| `APIConnectionError` | The API was unreachable after retries (resets, DNS, …) | cause chained as `__cause__` |
| `APITimeoutError` | The request timed out after retries (subclass of `APIConnectionError`) | cause chained as `__cause__` |
| `UploadTransferError` | A presigned file PUT failed after retries | `status_code`, `request_id` |
| `DownloadTransferError` | A signed file download failed after retries | `status_code`, `request_id` |
| `WaitTimeoutError` | A wait exceeded its `max_wait_seconds` | — |
| `CreditsLimitExceededError` | `jobs.run(max_credits=)` — the estimate exceeded the budget; the job was NOT started | `estimate`, `max_credits` |
| `UploadManyError` | A bulk-upload batch failed partway (earlier batches are not rolled back) | `partial`, `batches_completed` |
| `ExtractError` | The extract pipeline could not complete | `job`, `upload` |

## Idempotency

Writes send an `Idempotency-Key` (every method with an `idempotency_key=`
parameter). The server executes the request once,
records the response, and replays that same response to any retry carrying the
same key — so retried writes never double-execute. A replayed response carries
the `Idempotency-Replayed: true` header, so you can tell a cached replay from a
fresh execution. By default the SDK
generates a fresh key per call, which makes its own transient-failure retries
safe with zero configuration; every retry attempt within one call reuses the
same key.

**Exactly-once across process restarts.** Auto-generated keys die with the
process. If your script might crash between sending a write and recording its
outcome, persist the key *before* the call — re-running then replays the
original result instead of repeating the work:

```python
import json, uuid
from pathlib import Path

state = Path("import-state.json")
saved = json.loads(state.read_text()) if state.exists() else {}
key = saved.get("upload_key") or uuid.uuid4().hex
state.write_text(json.dumps({"upload_key": key}))  # persist BEFORE calling

# If a previous run died after the server committed but before we saw the
# response, this replays the SAME result — no duplicate documents.
result = client.documents.upload(paths, idempotency_key=key)
```

Recorded responses are replayable for a server-defined retention window (see
the API documentation); a fresh logical operation should always use a fresh
key.

**Slow writes.** If a retry arrives while the original request is still
executing server-side, the SDK recognizes the API's "request in progress"
reply, waits as instructed, and retries until it can replay the recorded
response. A write that outlives the whole retry budget surfaces a
`ConflictError` whose message says the request is still being processed — at
that point either retry later *with the same key*, or give slow operations a
bigger budget up front:

```python
client.with_options(max_retries=5).documents.move_to_folder(document_ids=ids, target_folder_id=folder_id)
```

**A key binds the exact request.** Reusing a key with different parameters is
an error, not a replay — the server rejects it with `UnprocessableEntityError`
so a copy-paste mistake can't silently return the wrong cached result.

**What is not keyed.** Read-only calls (`get`, `search`, `iterate`,
`download_url`, …) are retried freely because re-reading is naturally safe.
A few mutations are *naturally idempotent* — they converge on retry without a
key (`documents.delete`, and `folders.resolve_paths`/`ensure_path`, which are
get-or-create by path). [api.md](https://github.com/InferSoft-inc/infer-python/blob/main/api.md) marks each method **write** (keyed),
**read**, **write (naturally idempotent)**, or **composite** (listing the
calls it makes).

**Bulk uploads.** `upload_many` takes one base key and derives a deterministic
key per batch (`-0000`, `-0001`, …), so re-running an interrupted import with
the same key, the same files in the same order, and the same `batch_size`
dedupes batch-by-batch — see [Uploads](#uploads).

## Retries, timeouts & rate limiting

**What retries automatically.** Transient failures are retried with jittered
exponential backoff: transient statuses (408, 429, 500, 502, 503, 504),
transport errors (timeouts, connection resets), and the API's transient
"request in progress" 409 for keyed writes (see
[Idempotency](#idempotency)). A numeric `Retry-After` header is honored,
capped at 60 seconds so a misbehaving intermediary can't stall the client
(HTTP-date values fall back to the normal backoff).
Deterministic failures (validation 400s, 404s, real conflicts) are never
retried.

**Which requests are retryable.** Reads retry by nature — including the
POST-based `search` endpoints, which the SDK marks as read-only. Writes retry
safely because they carry an `Idempotency-Key`. Two budgets control attempts:
`max_retries` (API requests, default 2) and `upload_max_retries` (presigned
file transfers — the PUTs and signed downloads — defaulting to `max_retries`).
When a budget is exhausted, the last failure surfaces as the matching
exception (`RateLimitError`, `ServerError`, `APITimeoutError`, …).

**Timeouts.** The constructor takes `timeout` (API requests, default 30s) and
`upload_timeout` (presigned file transfers, default 300s — they move real
bytes). OAuth token requests keep their own internal 30s timeout.

**Per-call-site overrides.** `with_options(...)` returns a copy of the client
with different settings for the calls made through it — sharing the original's
connection pool and cached token, so it's cheap to create on the fly:

```python
# Give one heavy call a 5-minute timeout.
client.with_options(timeout=300).documents.bulk_delete(document_ids=ids)

# Fail fast inside your own polling loop (the next tick is the retry).
job = client.with_options(max_retries=0).jobs.get(job_id)

# A generous transfer budget for a big bulk upload.
client.with_options(upload_max_retries=5).documents.upload_many(paths)
```

Available overrides: `timeout`, `max_retries`, `upload_max_retries`. The
original client is unaffected, and closing a copy is a no-op (only closing the
original releases the shared pool), so copies are safe to use as context
managers.

**Rate limiting.** The API rate-limits per organization and answers excess
traffic with 429s carrying `Retry-After` (see the API documentation for
current limits). You usually don't need to do anything: the SDK backs off and
retries within its budget, and only surfaces `RateLimitError` once the budget
is exhausted — catch it to pause longer or shed load. For sustained bulk work,
prefer fewer concurrent calls plus the bulk helpers (`upload_many`,
`iterate`) over raising the budgets. The API also emits
`RateLimit-Limit`/`RateLimit-Remaining` response headers; the SDK does not
surface response headers on successful calls, so they're useful when
instrumenting at the HTTP/proxy layer rather than from SDK code.

**Retry logging.** Watch retry decisions with the stdlib logger:

```python
import logging
logging.getLogger("infersoft").setLevel(logging.DEBUG)
```

## Selectors

Search, `bulk_delete`, `move_to_folder`, `assign_documents`, and the job calls take
a `selectors` object. Build it with the `build_*_selector` helpers instead of
hand-writing dicts (raw dicts still work everywhere):

```python
from infersoft import build_selectors, build_folder_selector, build_tag_selector, build_name_selector

docs = client.documents.search(
    build_selectors(
        include=[build_folder_selector(5), build_tag_selector([10])],
        exclude=[build_name_selector("draft")],
    )
)
```

The full set: `build_file_selector`, `build_folder_selector`, `build_name_selector`,
`build_document_class_selector`, `build_tag_selector`, `build_created_at_selector`,
`build_size_selector`, `build_page_count_selector`, `build_source_document_selector`,
`build_project_selector`, `build_job_selector`, `build_is_valid_selector`,
`build_has_children_selector`, `build_has_running_workflow_selector`, plus
`build_selectors(include=…, exclude=…)`. (Also available under `infersoft.selectors`.)

When you already have a list of document IDs, skip selectors entirely — every
selector-taking method (`documents.search`/`iterate`/`get_values`/
`move_to_folder`/`bulk_delete`, `projects.assign_documents`,
`jobs.estimate`/`run`, and `client.extract`) accepts `document_ids=` as a
shortcut:

```python
job = client.jobs.run(step="extractor", document_ids=result.document_ids, prompts=[553])
```

Pass either `selectors` or `document_ids`, not both.

## API surface

The full method-by-method reference — every resource method mapped to the REST
calls it makes — lives in [api.md](https://github.com/InferSoft-inc/infer-python/blob/main/api.md). Extraction results come back inline
as typed `DocumentExtractionResultItem`s on `documents.search`/`get` when you
pass prompt IDs; there is no separate extraction endpoint.

## License

Apache License 2.0 — see [LICENSE](https://github.com/InferSoft-inc/infer-python/blob/main/LICENSE).

## Development

```bash
pip install -e ".[dev]"
ruff check .
mypy src
pytest
```
