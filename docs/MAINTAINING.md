# Maintaining infer-python

`infer-python` is the Python client for the InferSoft public REST API ([infer-rest](https://github.com/InferSoft-inc/infer-rest)), published to PyPI as [`infersoft`](https://pypi.org/project/infersoft/). It is hand-written: there is no code generator, and every model, resource method and selector builder was written against infer-rest's OpenAPI spec (`rest-api/openapi.yaml` in infer-rest). The .NET SDK, [infer-dotnet](https://github.com/InferSoft-inc/infer-dotnet), exposes the same surface, so an API change normally lands in both SDKs.

The customer-facing docs are [README.md](../README.md) (behavior and examples), [api.md](../api.md) (every public method and the REST calls it makes) and [CHANGELOG.md](../CHANGELOG.md). Keep all three accurate in the same PR as a code change; `pyproject.toml` ships README.md as the PyPI project description, which is why its links are absolute GitHub URLs.

## What the SDK talks to

```mermaid
flowchart LR
    app["Customer code<br/>Client / AsyncClient"]
    auth0["Auth0 token endpoint<br/>dev-noabnisxxguu0jp0.us.auth0.com/oauth/token"]
    rest["infer-rest public API<br/>https://api.infersoft.com /api/*"]
    s3["CloudFront-signed file URLs<br/>(signed by infer-facade, returned by infer-rest,<br/>served from S3)"]
    app -->|"POST client_credentials grant"| auth0
    app -->|"Bearer token, JSON"| rest
    app -->|"PUT file bytes (upload)<br/>GET file bytes (download)"| s3
```

| Dependency | How the SDK reaches it | Code |
|---|---|---|
| Auth0 token endpoint | `POST` with `grant_type=client_credentials`, `client_id`, `client_secret`, `audience` (and `scope` if set). Token cached until 60 s before `expires_in`, refreshed single-flight, refreshed once more on a 401. 30 s timeout of its own. | [`_auth.py`](../src/infersoft/_auth.py) |
| infer-rest | One pooled `httpx` client with `base_url`, the auth flow above, `User-Agent: infersoft-python/<version>`. | [`_http.py`](../src/infersoft/_http.py) |
| CloudFront-signed file URLs | A second pooled `httpx` client without auth (a bearer token would break the signature) and with `upload_timeout`. Used for upload `PUT`s and download `GET`s. | [`_http.py`](../src/infersoft/_http.py) `put_presigned` / `get_presigned` |

The defaults (`https://api.infersoft.com` for base URL and audience, the `dev-noabnisxxguu0jp0` Auth0 tenant for the token URL) are the production values: infer-rest's `ProdInferRESTStack` is deployed with `domainName: 'api.infersoft.com'` and `auth0Domain: 'dev-noabnisxxguu0jp0.us.auth0.com'`, and the API validates the token audience against `https://<domainName>`.

### Pointing the SDK at the dev environment

infer-rest's `DevInferRESTStack` runs on a different domain and Auth0 tenant. Override all three endpoints and use credentials from the dev tenant:

| Environment variable | Dev value |
|---|---|
| `INFERSOFT_BASE_URL` | `https://dev-api.infersoft.com` |
| `INFERSOFT_TOKEN_URL` | `https://dev-infersoft.us.auth0.com/oauth/token` |
| `INFERSOFT_AUDIENCE` | `https://dev-api.infersoft.com` |

The interactive API docs and `/openapi.yaml` are only served by the production API (`mountDocs` runs only when `ENVIRONMENT=production`): [api.infersoft.com/documentation](https://api.infersoft.com/documentation/).

## Local development

Work in a virtualenv (`.venv/` is gitignored). The last four commands are the ones CI runs:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
ruff check .
mypy src
pytest -q
```

`mypy` runs in strict mode (`[tool.mypy]` in `pyproject.toml`); `ruff` selects `E,F,I,B,UP` with a 100-character line length.

## CI

[`.github/workflows/ci.yml`](../.github/workflows/ci.yml) runs on every pull request and on pushes to `main`: install, `ruff check .`, `mypy src`, `pytest -q`, on Python 3.10, 3.11, 3.12, 3.13 and 3.14. It needs no secrets. Actions are pinned to commit SHAs.

`main` is the default branch and the only release branch. [CODEOWNERS](../.github/CODEOWNERS) assigns every path to the `InferSoft-inc/infersoft-it` team.

## Releasing to PyPI

Versions follow Semantic Versioning. The version lives only in [`src/infersoft/_version.py`](../src/infersoft/_version.py) (hatchling reads it through `[tool.hatch.version]`); it is also sent in the `User-Agent` header.

```mermaid
sequenceDiagram
    participant Dev as Maintainer
    participant GH as GitHub (main)
    participant Build as release.yml build job
    participant Pub as release.yml publish job (env pypi)
    participant PyPI
    Dev->>GH: PR bumps _version.py and CHANGELOG, merge to main
    Dev->>GH: push tag vX.Y.Z on the merge commit
    GH->>Build: on push tags v*
    Build->>Build: tag must equal __version__
    Build->>Build: ruff, mypy, pytest on the tagged commit
    Build->>Build: python -m build (sdist + wheel), upload artifact dist
    Build->>Pub: needs build
    Pub->>PyPI: pypa/gh-action-pypi-publish via OIDC trusted publishing (skip-existing)
```

1. In a PR to `main`, set `__version__` in `_version.py` and move the `## [Unreleased]` notes in CHANGELOG.md under `## [X.Y.Z] - <date>`. Merge it.
2. Tag the merge commit by its SHA (a bare `git tag` would tag whatever is checked out locally) and push the tag:
   ```bash
   git fetch origin
   git tag vX.Y.Z <merge commit SHA>
   git push origin vX.Y.Z
   ```
3. [`.github/workflows/release.yml`](../.github/workflows/release.yml) builds and publishes:
   - **build & verify**: fails unless the tag without its `v` equals `__version__`, reruns `ruff`, `mypy` and `pytest`, builds the sdist and wheel, uploads them as the `dist` artifact.
   - **publish to PyPI**: runs in the GitHub environment `pypi`, which only accepts refs matching the tag pattern `v*`. It has `id-token: write` and publishes through PyPI Trusted Publishing (GitHub OpenID Connect, OIDC), so there is no PyPI token or repository secret. The PyPI trusted publisher must stay registered for this repo, `release.yml` and the `pypi` environment. `skip-existing: true` lets a failed publish be re-run without burning the version.

A tag pushed on a commit whose `_version.py` does not match fails the version check and publishes nothing.

Published versions and their dates are on [PyPI](https://pypi.org/project/infersoft/#history) and in [CHANGELOG.md](../CHANGELOG.md).

## Code layout

| Path | What it holds |
|---|---|
| [`src/infersoft/__init__.py`](../src/infersoft/__init__.py) | Public exports (`__all__`): clients, errors, models, selector builders. |
| [`src/infersoft/_client.py`](../src/infersoft/_client.py) | `Client` and `AsyncClient`: settings resolution from arguments and `INFERSOFT_*` environment variables, `with_options`, the `extract` pipeline. |
| [`src/infersoft/_auth.py`](../src/infersoft/_auth.py) | OAuth2 client-credentials `httpx.Auth` (sync and async flows). |
| [`src/infersoft/_http.py`](../src/infersoft/_http.py) | Request sending, retry policy, `Idempotency-Key` header, request-in-progress 409 handling, presigned transfers. |
| [`src/infersoft/_wait.py`](../src/infersoft/_wait.py) | Polling helper and the default wait budgets (30 minutes for jobs, 10 minutes for documents, 2 s interval). |
| [`src/infersoft/errors.py`](../src/infersoft/errors.py) | Exception hierarchy and the status code to exception mapping. |
| [`src/infersoft/models.py`](../src/infersoft/models.py) | Pydantic v2 response models (`extra="allow"`, so unknown server fields survive in `model_extra`) and extensible string enums. |
| [`src/infersoft/selectors.py`](../src/infersoft/selectors.py) | `build_*_selector` helpers, one per selector type in the spec. |
| [`src/infersoft/resources/`](../src/infersoft/resources/) | One module per resource (`documents`, `folders`, `projects`, `prompts`, `jobs`), each with a sync and an async class. |
| [`src/infersoft/_version.py`](../src/infersoft/_version.py) | `__version__`, the single version source. |
| [`tests/`](../tests/) | pytest suite; HTTP is mocked with `pytest-httpx`, so no credentials or network are needed. [`tests/test_examples.py`](../tests/test_examples.py) compiles every script in `examples/`. |
| [`examples/`](../examples/) | Runnable scripts; they need real credentials in `INFERSOFT_CLIENT_ID` / `INFERSOFT_CLIENT_SECRET`. |

Sync and async classes are written side by side and must stay identical in signatures and behavior (api.md promises one-to-one parity).

## How the SDK maps to infer-rest

infer-rest registers its routes in `rest-api/internal/server/server.go`. Routes wrapped in `idempotent(...)` record and replay responses per `Idempotency-Key`; routes wrapped in `validated(...)` ignore the header. The SDK follows the same split: it sends a key (auto-generated UUID4 hex, or the caller's `idempotency_key`) only to the recorded routes, and marks read-style `POST`s `idempotent=True` so they are retried without a key.

| Route | infer-rest wrapper | SDK methods | SDK sends `Idempotency-Key` |
|---|---|---|---|
| `POST /api/uploads` | idempotent | `documents.upload`, `upload_many` (key `<base>-NNNN` per batch) | yes |
| `POST /api/documents/search` | validated | `documents.search`, `iterate`, `get_values`, `wait_until_ready`, `jobs.results` | no |
| `GET /api/documents/{id}` | validated | `documents.get`, `download` (name lookup) | no |
| `GET /api/documents/{id}/download` | validated | `documents.download_url`, `download` | no |
| `DELETE /api/documents/{id}` | validated | `documents.delete`, `upload(on_duplicate="block")` | no |
| `POST /api/documents/move-to-folder` | idempotent | `documents.move_to_folder` | yes |
| `POST /api/documents/bulk_delete` | idempotent | `documents.bulk_delete` | yes |
| `POST /api/folders` | idempotent | `folders.create` | yes |
| `GET /api/folders/{id}` | validated | `folders.get` | no |
| `PATCH /api/folders/{id}` | idempotent | `folders.rename` | yes |
| `POST /api/folders/search` | validated | `folders.search`, `iterate` | no |
| `POST /api/folders/move` | idempotent | `folders.move` | yes |
| `POST /api/folders/paths` | validated | `folders.resolve_paths`, `ensure_path` | no |
| `POST /api/folders/bulk_delete` | idempotent | `folders.bulk_delete` | yes |
| `POST /api/projects` | idempotent | `projects.create`, `get_or_create` | yes |
| `GET /api/projects/{id}` | validated | `projects.get` | no |
| `POST /api/projects/search` | validated | `projects.search`, `iterate`, `get_or_create` | no |
| `POST /api/projects/assign-documents` | idempotent | `projects.assign_documents` | yes |
| `POST /api/prompts/search` | validated | `prompts.search`, `iterate` | no |
| `POST /api/jobs/credits/estimate` | idempotent | `jobs.estimate`, `run` | yes |
| `POST /api/jobs/credits/quote` | validated | `jobs.quote` | no |
| `POST /api/jobs/start` | idempotent | `jobs.start`, `run` | yes |
| `GET /api/jobs/{id}` | validated | `jobs.get`, `wait`, `run(wait=True)` | no |
| `POST /api/jobs/search` | validated | `jobs.search`, `iterate` | no |

Routes in the spec that the SDK deliberately does not wrap: `POST /api/documents/{id}/resplit` and `POST /api/documents/{id}/split` (marked deprecated and internal-only in the spec) and `POST /api/documents/extraction_results/search` (deprecated; extraction values come from `documents.search` with `prompts`).

### Server contracts copied by hand

These values live in both codebases. Change them together.

| SDK | infer-rest |
|---|---|
| `_PROBLEM_TYPE_REQUEST_IN_PROGRESS` in [`_http.py`](../src/infersoft/_http.py) | `ProblemTypeRequestInProgress` in `rest-api/internal/idempotency/middleware.go` (the transient 409 the SDK retries, honoring `Retry-After`) |
| `MAX_BATCH_FILES = 100` in [`resources/documents.py`](../src/infersoft/resources/documents.py) | `BatchUploadRequest.files` `maxItems: 100` |
| `build_*_selector` set in [`selectors.py`](../src/infersoft/selectors.py) | `Selector` union in the spec |
| `JobStatus`, `DocumentStatus`, `DataTypeName` in [`models.py`](../src/infersoft/models.py) | `JobStatus`, `DocumentStatus`, `DataTypeName` enums in the spec |
| Terminal job statuses (`completed`, `failed`, `partial_success`) in [`resources/jobs.py`](../src/infersoft/resources/jobs.py) | `JobStatus` enum (`running` and `creating_workflows` are non-terminal) |
| Default endpoints in [`_client.py`](../src/infersoft/_client.py) | `ProdInferRESTStack` props in `bin/infer-rest.ts` |

Enums are extensible: an unknown value parses, is treated as non-terminal by the waiters, and each wait call logs one warning per unknown value. A new terminal status therefore needs an SDK release, or waits run until `max_wait_seconds`.

### Keeping in sync with the spec

1. Find what changed in infer-rest's spec since the last SDK release (production runs infer-rest `main`):
   ```bash
   git -C ../infer-rest log -p origin/main -- rest-api/openapi.yaml
   ```
2. Update the models, resources and selector builders here and add tests that mock the new wire shape with `pytest-httpx`.
3. Update api.md and README.md, and add an entry under `## [Unreleased]` in CHANGELOG.md.
4. Make the same change in infer-dotnet.

## Dependencies

- Runtime: `httpx>=0.27,<1` and `pydantic>=2.7,<3`, declared as ranges with no lock file because this is a library.
- [Dependabot](../.github/dependabot.yml) opens one grouped weekly PR for GitHub Actions SHA pins. There is no `pip` ecosystem entry on purpose: raising the dependency floors would raise them for every consumer.

## Security reports

[SECURITY.md](../SECURITY.md): reports go through GitHub private vulnerability reporting; only the latest release is supported.

## Related repos

- [infer-rest](https://github.com/InferSoft-inc/infer-rest): the REST API and its OpenAPI spec.
- [infer-dotnet](https://github.com/InferSoft-inc/infer-dotnet): the .NET SDK with the same surface.
