"""Top-level Infersoft API clients (sync ``Client`` and ``AsyncClient``)."""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Literal

from ._auth import ClientCredentialsAuth
from ._http import AsyncHttpClient, HttpClient
from ._wait import DEFAULT_JOB_WAIT_SECONDS, DEFAULT_POLL_INTERVAL
from .errors import ExtractError, InfersoftError
from .models import ExtractResult, JobStatus, UploadResult
from .resources import (
    AsyncDocumentsResource,
    AsyncFoldersResource,
    AsyncJobsResource,
    AsyncProjectsResource,
    AsyncPromptsResource,
    DocumentsResource,
    FoldersResource,
    JobsResource,
    ProjectsResource,
    PromptsResource,
)
from .resources.documents import PathLike, Selectors, _flatten_extractions

DEFAULT_BASE_URL = "https://api.infersoft.com"
DEFAULT_AUDIENCE = "https://api.infersoft.com"
DEFAULT_TOKEN_URL = "https://dev-noabnisxxguu0jp0.us.auth0.com/oauth/token"


def _resolve_settings(
    client_id: str | None,
    client_secret: str | None,
    base_url: str | None,
    token_url: str | None,
    audience: str | None,
) -> tuple[str, str, str, str, str]:
    """Resolve credentials/endpoints from args, falling back to env vars."""
    client_id = client_id or os.environ.get("INFERSOFT_CLIENT_ID")
    client_secret = client_secret or os.environ.get("INFERSOFT_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise InfersoftError(
            "client_id and client_secret are required (pass them directly or set "
            "INFERSOFT_CLIENT_ID / INFERSOFT_CLIENT_SECRET)"
        )
    base_url = base_url or os.environ.get("INFERSOFT_BASE_URL", DEFAULT_BASE_URL)
    token_url = token_url or os.environ.get("INFERSOFT_TOKEN_URL", DEFAULT_TOKEN_URL)
    audience = audience or os.environ.get("INFERSOFT_AUDIENCE", DEFAULT_AUDIENCE)
    return client_id, client_secret, base_url, token_url, audience


class Client:
    """Synchronous client for the Infersoft REST API.

    Credentials and endpoints fall back to environment variables when not
    passed explicitly:

    - ``INFERSOFT_CLIENT_ID`` / ``INFERSOFT_CLIENT_SECRET``
    - ``INFERSOFT_BASE_URL``, ``INFERSOFT_TOKEN_URL``, ``INFERSOFT_AUDIENCE``
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        token_url: str | None = None,
        audience: str | None = None,
        scope: str | None = None,
        timeout: float = 30.0,
        upload_timeout: float = 300.0,
        max_retries: int = 2,
        upload_max_retries: int | None = None,
    ) -> None:
        client_id, client_secret, base_url, token_url, audience = _resolve_settings(
            client_id, client_secret, base_url, token_url, audience
        )
        auth = ClientCredentialsAuth(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            audience=audience,
            scope=scope,
            max_retries=max_retries,
        )
        self._http = HttpClient(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
            max_retries=max_retries,
            upload_timeout=upload_timeout,
            upload_max_retries=upload_max_retries,
        )

        self.documents = DocumentsResource(self._http)
        self.folders = FoldersResource(self._http)
        self.projects = ProjectsResource(self._http)
        self.prompts = PromptsResource(self._http)
        self.jobs = JobsResource(self._http)

    @classmethod
    def _from_http(cls, http: HttpClient) -> Client:
        new = cls.__new__(cls)
        new._http = http
        new.documents = DocumentsResource(http)
        new.folders = FoldersResource(http)
        new.projects = ProjectsResource(http)
        new.prompts = PromptsResource(http)
        new.jobs = JobsResource(http)
        return new

    def with_options(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        upload_max_retries: int | None = None,
    ) -> Client:
        """Return a copy of this client with per-request setting overrides.

        The copy shares this client's connection pool and token cache (no new
        connections or extra token fetches) and applies the given ``timeout`` /
        ``max_retries`` to its requests::

            client.with_options(timeout=300).documents.bulk_delete(...)
            client.with_options(max_retries=0).jobs.get(job_id)

        Closing a copy is a no-op (only closing the original releases the shared
        pool), so copies are safe to use as context managers. The ``timeout``
        override applies to API requests; OAuth token acquisition keeps its own
        30s timeout.
        """
        return Client._from_http(self._http.copy_with(
            timeout=timeout, max_retries=max_retries, upload_max_retries=upload_max_retries
        ))

    def extract(
        self,
        files: PathLike | Sequence[PathLike] | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        selectors: Selectors | None = None,
        prompts: Sequence[int],
        project_id: int | None = None,
        project_name: str | None = None,
        flatten: bool = True,
        max_credits: int | None = None,
        key_by: Literal["name", "prompt_id"] = "name",
        raise_on_failure: bool = True,
        max_wait_seconds: float | None = DEFAULT_JOB_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> ExtractResult:
        """Run the whole extraction pipeline in one call.

        Pass exactly one of ``files`` (uploaded first and waited until ready),
        ``document_ids``, or ``selectors``. The extractor job is then run with
        ``prompts`` and waited to completion, and the result carries the
        documents plus their values flattened to ``{document_id: {field: value}}``
        (see ``documents.get_values`` for ``key_by``).

        Upload-phase semantics (``files=...``): files that fail to upload do
        **not** raise — the pipeline proceeds with the files that succeeded,
        and ``result.values`` covers only those. Check ``result.upload.failed``
        (per-file outcomes, the same vocabulary as ``upload``/``upload_many``)
        before treating the result as complete. Only when **every** file fails
        is an ``ExtractError`` raised, since there is nothing left to run.

        Failure semantics: a ``failed`` job raises ``ExtractError`` (with the
        ``.job`` and any ``.upload`` attached for manual resumption) unless
        ``raise_on_failure=False``; ``partial_success`` returns normally —
        inspect ``result.job.error_count`` and per-document statuses. A
        ``CreditsLimitExceededError`` from ``max_credits`` and a
        ``WaitTimeoutError`` from the waits propagate as-is; documents uploaded
        before either are not rolled back. ``max_wait_seconds`` applies to each
        waiting phase (upload readiness, job completion) separately — it is a
        per-phase budget, not a total; it defaults to 30 minutes per phase, and
        ``None`` disables the deadline for both phases.
        """
        provided = sum(x is not None for x in (files, document_ids, selectors))
        if provided != 1:
            raise ValueError(
                "extract() requires exactly one of files, document_ids, or selectors"
            )
        if files is None and project_name is not None:
            raise ValueError(
                "project_name requires files (the upload creates the project); "
                "pass project_id instead"
            )

        upload: UploadResult | None = None
        if files is not None:
            upload = self.documents.upload(
                files,
                flatten=flatten,
                project_id=project_id,
                project_name=project_name,
                wait=True,
                max_wait_seconds=max_wait_seconds,
                poll_interval=poll_interval,
            )
            document_ids = upload.document_ids
            if not document_ids:
                raise ExtractError(
                    "extract() aborted: no documents to process "
                    "(every file failed to upload)",
                    upload=upload,
                )

        job = self.jobs.run(
            step="extractor",
            selectors=selectors,
            document_ids=document_ids,
            prompts=prompts,
            project_id=None if files is not None else project_id,
            max_credits=max_credits,
            wait=True,
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
        )
        if raise_on_failure and job.status == JobStatus.failed:
            raise ExtractError(f"extraction job {job.id} failed", job=job, upload=upload)

        documents = list(self.jobs.results(job, prompts=prompts))
        values = {doc.id: _flatten_extractions(doc, key_by) for doc in documents}
        return ExtractResult(job=job, upload=upload, documents=documents, values=values)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> Client:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class AsyncClient:
    """Asynchronous client for the Infersoft REST API.

    Mirrors :class:`Client` (same constructor and env-var fallbacks); every
    resource method is a coroutine and ``iterate(...)`` yields with ``async for``.
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        client_secret: str | None = None,
        base_url: str | None = None,
        token_url: str | None = None,
        audience: str | None = None,
        scope: str | None = None,
        timeout: float = 30.0,
        upload_timeout: float = 300.0,
        max_retries: int = 2,
        upload_max_retries: int | None = None,
    ) -> None:
        client_id, client_secret, base_url, token_url, audience = _resolve_settings(
            client_id, client_secret, base_url, token_url, audience
        )
        auth = ClientCredentialsAuth(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
            audience=audience,
            scope=scope,
            max_retries=max_retries,
        )
        self._http = AsyncHttpClient(
            base_url=base_url,
            auth=auth,
            timeout=timeout,
            max_retries=max_retries,
            upload_timeout=upload_timeout,
            upload_max_retries=upload_max_retries,
        )

        self.documents = AsyncDocumentsResource(self._http)
        self.folders = AsyncFoldersResource(self._http)
        self.projects = AsyncProjectsResource(self._http)
        self.prompts = AsyncPromptsResource(self._http)
        self.jobs = AsyncJobsResource(self._http)

    @classmethod
    def _from_http(cls, http: AsyncHttpClient) -> AsyncClient:
        new = cls.__new__(cls)
        new._http = http
        new.documents = AsyncDocumentsResource(http)
        new.folders = AsyncFoldersResource(http)
        new.projects = AsyncProjectsResource(http)
        new.prompts = AsyncPromptsResource(http)
        new.jobs = AsyncJobsResource(http)
        return new

    def with_options(
        self,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
        upload_max_retries: int | None = None,
    ) -> AsyncClient:
        """Return a copy of this client with per-request setting overrides.

        The copy shares this client's connection pool and token cache (no new
        connections or extra token fetches) and applies the given ``timeout`` /
        ``max_retries`` to its requests. Closing a copy is a no-op (only closing
        the original releases the shared pool), so copies are safe to use as
        context managers. The ``timeout`` override applies to API requests;
        OAuth token acquisition keeps its own 30s timeout.
        """
        return AsyncClient._from_http(
            self._http.copy_with(
            timeout=timeout, max_retries=max_retries, upload_max_retries=upload_max_retries
        )
        )

    async def extract(
        self,
        files: PathLike | Sequence[PathLike] | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        selectors: Selectors | None = None,
        prompts: Sequence[int],
        project_id: int | None = None,
        project_name: str | None = None,
        flatten: bool = True,
        max_credits: int | None = None,
        key_by: Literal["name", "prompt_id"] = "name",
        raise_on_failure: bool = True,
        max_wait_seconds: float | None = DEFAULT_JOB_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> ExtractResult:
        """Async :meth:`Client.extract` (same inputs and failure semantics)."""
        provided = sum(x is not None for x in (files, document_ids, selectors))
        if provided != 1:
            raise ValueError(
                "extract() requires exactly one of files, document_ids, or selectors"
            )
        if files is None and project_name is not None:
            raise ValueError(
                "project_name requires files (the upload creates the project); "
                "pass project_id instead"
            )

        upload: UploadResult | None = None
        if files is not None:
            upload = await self.documents.upload(
                files,
                flatten=flatten,
                project_id=project_id,
                project_name=project_name,
                wait=True,
                max_wait_seconds=max_wait_seconds,
                poll_interval=poll_interval,
            )
            document_ids = upload.document_ids
            if not document_ids:
                raise ExtractError(
                    "extract() aborted: no documents to process "
                    "(every file failed to upload)",
                    upload=upload,
                )

        job = await self.jobs.run(
            step="extractor",
            selectors=selectors,
            document_ids=document_ids,
            prompts=prompts,
            project_id=None if files is not None else project_id,
            max_credits=max_credits,
            wait=True,
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
        )
        if raise_on_failure and job.status == JobStatus.failed:
            raise ExtractError(f"extraction job {job.id} failed", job=job, upload=upload)

        documents = [doc async for doc in self.jobs.results(job, prompts=prompts)]
        values = {doc.id: _flatten_extractions(doc, key_by) for doc in documents}
        return ExtractResult(job=job, upload=upload, documents=documents, values=values)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> AsyncClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
