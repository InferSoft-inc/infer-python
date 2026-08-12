"""Jobs resource: credit estimation, job start, and a combined ``run`` helper."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from typing import Any

from .._http import AsyncHttpClient, HttpClient, new_idempotency_key
from .._wait import (
    DEFAULT_JOB_WAIT_SECONDS,
    DEFAULT_POLL_INTERVAL,
    apoll_until,
    poll_until,
    warn_once_unknown_status,
)
from ..errors import CreditsLimitExceededError
from ..models import CreditsEstimate, CreditsQuote, DocumentSummary, Job, JobPage, JobStatus
from ..selectors import _resolve_selectors, build_job_selector, build_selectors
from .documents import AsyncDocumentsResource, DocumentsResource

Selectors = dict[str, Any]
Step = str  # "splitter" | "classifier" | "extractor"

#: A job is finished once it reaches one of these.
_TERMINAL_JOB_STATUSES = {
    JobStatus.completed,
    JobStatus.failed,
    JobStatus.partial_success,
}

#: Known status VALUES (pseudo-members from unknown server statuses are
#: instances of JobStatus too, so membership is checked by value).
_KNOWN_JOB_STATUS_VALUES = frozenset(m.value for m in JobStatus)


def _terminal_check(seen_unknown: set[str]) -> Callable[[Job], bool]:
    """Build the wait predicate: terminal-status check + warn-once on unknowns."""

    def check(j: Job) -> bool:
        warn_once_unknown_status(seen_unknown, j.status.value, _KNOWN_JOB_STATUS_VALUES, "job")
        return j.status in _TERMINAL_JOB_STATUSES

    return check


class JobsResource:
    def __init__(self, http: HttpClient) -> None:
        self._http = http
        # Private documents twin used by results(); stateless beyond http.
        self._documents = DocumentsResource(http)

    def estimate(
        self,
        *,
        step: Step,
        selectors: Selectors | None = None,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        synchronous: bool = False,
        idempotency_key: str | None = None,
    ) -> CreditsEstimate:
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {
            "steps": [step],
            "selectors": selectors,
            "synchronous": synchronous,
        }
        if prompts:
            body["prompts"] = list(prompts)
        raw = self._http.request(
            "POST",
            "/api/jobs/credits/estimate",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return CreditsEstimate.model_validate(raw)

    def quote(
        self,
        *,
        step: Step,
        selectors: Selectors | None = None,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        synchronous: bool = False,
    ) -> CreditsQuote:
        """Price a workflow without reserving anything.

        Same request shape and formula as :meth:`estimate`, but read-only: no
        documents are reserved and no ``id`` is returned, so the result cannot
        be passed to :meth:`start`. Use it while composing a job; call
        :meth:`estimate` when ready to start.
        """
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {
            "steps": [step],
            "selectors": selectors,
            "synchronous": synchronous,
        }
        if prompts:
            body["prompts"] = list(prompts)
        raw = self._http.request("POST", "/api/jobs/credits/quote", json=body, idempotent=True)
        return CreditsQuote.model_validate(raw)

    def start(
        self,
        credits_id: str,
        *,
        project_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        body: dict[str, Any] = {"credits_id": credits_id}
        if project_id is not None:
            body["project_id"] = project_id
        raw = self._http.request(
            "POST",
            "/api/jobs/start",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Job.model_validate(raw)

    def run(
        self,
        *,
        step: Step,
        selectors: Selectors | None = None,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        synchronous: bool = False,
        project_id: int | None = None,
        max_credits: int | None = None,
        wait: bool = False,
        max_wait_seconds: float | None = DEFAULT_JOB_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Job:
        """Estimate credits then immediately start the job from that estimate.

        ``max_credits`` guards the budget: if the estimate exceeds it, a
        ``CreditsLimitExceededError`` carrying the estimate is raised and the
        job is **not** started. (The gate is on the estimate, not a server-side
        cap on actual spend.)

        With ``wait=True`` the call blocks via :meth:`wait` until the job reaches
        a terminal status and returns the finished job.
        """
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        estimate = self.estimate(
            step=step, selectors=selectors, prompts=prompts, synchronous=synchronous
        )
        if max_credits is not None and estimate.total_credits > max_credits:
            raise CreditsLimitExceededError(estimate, max_credits)
        job = self.start(estimate.id, project_id=project_id)
        if wait:
            return self.wait(job, max_wait_seconds=max_wait_seconds, poll_interval=poll_interval)
        return job

    def get(self, job_id: int) -> Job:
        raw = self._http.request("GET", f"/api/jobs/{job_id}")
        return Job.model_validate(raw)

    def wait(
        self,
        job: Job | int,
        *,
        max_wait_seconds: float | None = DEFAULT_JOB_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Job:
        """Poll a job until it reaches a terminal status and return it.

        Terminal means ``completed``, ``failed``, or ``partial_success`` — note a
        finished job is returned even when it failed; inspect ``.status``.
        ``max_wait_seconds`` defaults to 30 minutes, after which a
        ``WaitTimeoutError`` is raised — so a stuck job (or a future terminal
        status this SDK build does not know) fails loudly instead of hanging.
        Pass ``max_wait_seconds=None`` to wait without a deadline.
        """
        job_id = job.id if isinstance(job, Job) else job
        return poll_until(
            lambda: self.get(job_id),
            _terminal_check(set()),
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
            timeout_message=(
                f"job {job_id} did not reach a terminal status within {max_wait_seconds}s"
            ),
        )

    def results(
        self,
        job: Job | int,
        *,
        prompts: Sequence[int] | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> Iterator[DocumentSummary]:
        """Yield the documents a job touched, fetching pages on demand.

        Pass ``prompts`` to get each document's ``extraction_results`` (and the
        ``.extractions`` accessor) populated — the typical post-job step. Note
        the prompts are not recorded on the job, so pass the same IDs the job ran.
        """
        job_id = job.id if isinstance(job, Job) else job
        selectors = build_selectors(include=[build_job_selector([job_id])])
        yield from self._documents.iterate(
            selectors,
            prompts=prompts,
            page_size=page_size,
            order_by=order_by,
            order_dir=order_dir,
        )

    def search(
        self,
        *,
        statuses: Sequence[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "desc",
    ) -> JobPage:
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if statuses:
            body["statuses"] = list(statuses)
        if created_from is not None:
            body["created_from"] = created_from
        if created_to is not None:
            body["created_to"] = created_to
        # Read-only search: safe to retry on transient failures.
        raw = self._http.request("POST", "/api/jobs/search", json=body, idempotent=True)
        return JobPage.model_validate(raw)

    def iterate(
        self,
        *,
        statuses: Sequence[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "desc",
    ) -> Iterator[Job]:
        """Yield every matching job, fetching pages on demand."""
        page = 1
        while True:
            result = self.search(
                statuses=statuses,
                created_from=created_from,
                created_to=created_to,
                page=page,
                page_size=page_size,
                order_by=order_by,
                order_dir=order_dir,
            )
            yield from result.items
            if not result.has_more:
                return
            page += 1


class AsyncJobsResource:
    def __init__(self, http: AsyncHttpClient) -> None:
        self._http = http
        # Private documents twin used by results(); stateless beyond http.
        self._documents = AsyncDocumentsResource(http)

    async def estimate(
        self,
        *,
        step: Step,
        selectors: Selectors | None = None,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        synchronous: bool = False,
        idempotency_key: str | None = None,
    ) -> CreditsEstimate:
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {
            "steps": [step],
            "selectors": selectors,
            "synchronous": synchronous,
        }
        if prompts:
            body["prompts"] = list(prompts)
        raw = await self._http.request(
            "POST",
            "/api/jobs/credits/estimate",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return CreditsEstimate.model_validate(raw)

    async def quote(
        self,
        *,
        step: Step,
        selectors: Selectors | None = None,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        synchronous: bool = False,
    ) -> CreditsQuote:
        """Async :meth:`JobsResource.quote` (read-only, nothing reserved)."""
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {
            "steps": [step],
            "selectors": selectors,
            "synchronous": synchronous,
        }
        if prompts:
            body["prompts"] = list(prompts)
        raw = await self._http.request(
            "POST", "/api/jobs/credits/quote", json=body, idempotent=True
        )
        return CreditsQuote.model_validate(raw)

    async def start(
        self,
        credits_id: str,
        *,
        project_id: int | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        body: dict[str, Any] = {"credits_id": credits_id}
        if project_id is not None:
            body["project_id"] = project_id
        raw = await self._http.request(
            "POST",
            "/api/jobs/start",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Job.model_validate(raw)

    async def run(
        self,
        *,
        step: Step,
        selectors: Selectors | None = None,
        document_ids: Sequence[int] | None = None,
        prompts: Sequence[int] | None = None,
        synchronous: bool = False,
        project_id: int | None = None,
        max_credits: int | None = None,
        wait: bool = False,
        max_wait_seconds: float | None = DEFAULT_JOB_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Job:
        """Estimate credits then immediately start the job from that estimate.

        See :meth:`JobsResource.run` for the ``max_credits`` and ``wait`` semantics.
        """
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        estimate = await self.estimate(
            step=step, selectors=selectors, prompts=prompts, synchronous=synchronous
        )
        if max_credits is not None and estimate.total_credits > max_credits:
            raise CreditsLimitExceededError(estimate, max_credits)
        job = await self.start(estimate.id, project_id=project_id)
        if wait:
            return await self.wait(
                job, max_wait_seconds=max_wait_seconds, poll_interval=poll_interval
            )
        return job

    async def get(self, job_id: int) -> Job:
        raw = await self._http.request("GET", f"/api/jobs/{job_id}")
        return Job.model_validate(raw)

    async def wait(
        self,
        job: Job | int,
        *,
        max_wait_seconds: float | None = DEFAULT_JOB_WAIT_SECONDS,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
    ) -> Job:
        """Async :meth:`JobsResource.wait` (default 30 min; ``None`` = no deadline)."""
        job_id = job.id if isinstance(job, Job) else job
        return await apoll_until(
            lambda: self.get(job_id),
            _terminal_check(set()),
            max_wait_seconds=max_wait_seconds,
            poll_interval=poll_interval,
            timeout_message=(
                f"job {job_id} did not reach a terminal status within {max_wait_seconds}s"
            ),
        )

    async def results(
        self,
        job: Job | int,
        *,
        prompts: Sequence[int] | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> AsyncIterator[DocumentSummary]:
        """Async :meth:`JobsResource.results`."""
        job_id = job.id if isinstance(job, Job) else job
        selectors = build_selectors(include=[build_job_selector([job_id])])
        async for doc in self._documents.iterate(
            selectors,
            prompts=prompts,
            page_size=page_size,
            order_by=order_by,
            order_dir=order_dir,
        ):
            yield doc

    async def search(
        self,
        *,
        statuses: Sequence[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "desc",
    ) -> JobPage:
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if statuses:
            body["statuses"] = list(statuses)
        if created_from is not None:
            body["created_from"] = created_from
        if created_to is not None:
            body["created_to"] = created_to
        raw = await self._http.request("POST", "/api/jobs/search", json=body, idempotent=True)
        return JobPage.model_validate(raw)

    async def iterate(
        self,
        *,
        statuses: Sequence[str] | None = None,
        created_from: str | None = None,
        created_to: str | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "desc",
    ) -> AsyncIterator[Job]:
        """Yield every matching job, fetching pages on demand."""
        page = 1
        while True:
            result = await self.search(
                statuses=statuses,
                created_from=created_from,
                created_to=created_to,
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
