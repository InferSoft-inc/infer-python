"""Polling waiters: jobs.wait / jobs.run(wait=) and documents.wait_until_ready / upload(wait=)."""

from __future__ import annotations

import pytest

from infersoft import WaitTimeoutError


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr("infersoft._wait.time.sleep", lambda _seconds: None)


def _job(status: str, jid: int = 5) -> dict:
    return {
        "id": jid,
        "organization_id": "org",
        "organization_name": "Org",
        "total_docs": 1,
        "completed_docs": 0,
        "error_count": 0,
        "status": status,
        "createdAt": "2026-05-29T00:00:00Z",
    }


def _doc_page(status: str, did: int = 1) -> dict:
    return {
        "items": [
            {
                "id": did,
                "organization_id": "org",
                "name": "a.pdf",
                "status": status,
                "is_valid": True,
                "created_at": "2026-05-29T00:00:00Z",
                "has_active_workflow": False,
            }
        ],
        "page": 1,
        "page_size": 50,
        "has_more": False,
    }


def test_jobs_wait_polls_until_terminal(client, httpx_mock):
    for status in ("running", "creating_workflows", "completed"):
        httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job(status))

    job = client.jobs.wait(5)

    assert job.status.value == "completed"
    gets = [r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/5"]
    assert len(gets) == 3


def test_jobs_run_with_wait(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/estimate",
        json={"total_credits": 1, "page_count": 1, "id": "c1"},
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/jobs/start", json=_job("running", 9)
    )
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/jobs/9", json=_job("completed", 9)
    )

    job = client.jobs.run(step="extractor", document_ids=[1], prompts=[5], wait=True)

    assert job.id == 9 and job.status.value == "completed"


def test_jobs_wait_times_out(client, httpx_mock, monkeypatch):
    # clock: first call sets the deadline at 1.0; the next call is past it.
    # (_monotonic is the wait module's scoped deadline clock alias.)
    times = iter([0.0, 100.0, 100.0])
    monkeypatch.setattr("infersoft._wait._monotonic", lambda: next(times))
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job("running"))

    with pytest.raises(WaitTimeoutError, match="job 5"):
        client.jobs.wait(5, max_wait_seconds=1)


def test_documents_wait_until_ready(client, httpx_mock):
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_doc_page("processing")
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_doc_page("ready")
    )

    docs = client.documents.wait_until_ready([1])

    assert docs[0].status.value == "ready"
    searches = [r for r in httpx_mock.get_requests() if r.url.path == "/api/documents/search"]
    assert len(searches) == 2


def test_upload_with_wait_blocks_until_ready(client, httpx_mock, tmp_path):
    doc = tmp_path / "a.pdf"
    doc.write_bytes(b"%PDF data")
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": "a.pdf",
                    "document_id": 1,
                    "put_url": "https://s3.test/p",
                    "required_headers": {},
                }
            ]
        },
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_doc_page("ready")
    )

    result = client.documents.upload(doc, wait=True)

    assert result.document_ids == [1]
    # The readiness poll ran.
    assert any(r.url.path == "/api/documents/search" for r in httpx_mock.get_requests())


# -------- finite defaults + None-means-infinity --------


def _leap_monotonic(monkeypatch, step: float = 1000.0):
    """Make each monotonic() call advance by `step` seconds (fake wall clock)."""
    state = {"now": 0.0}

    def fake_monotonic() -> float:
        state["now"] += step
        return state["now"]

    monkeypatch.setattr("infersoft._wait._monotonic", fake_monotonic)


def test_wait_defaults_are_finite() -> None:
    import inspect

    from infersoft._client import AsyncClient as AC
    from infersoft._client import Client as C
    from infersoft.resources.documents import AsyncDocumentsResource, DocumentsResource
    from infersoft.resources.jobs import AsyncJobsResource, JobsResource

    direct = [
        (JobsResource.wait, 1800.0),
        (JobsResource.run, 1800.0),
        (AsyncJobsResource.wait, 1800.0),
        (AsyncJobsResource.run, 1800.0),
        (DocumentsResource.wait_until_ready, 600.0),
        (DocumentsResource.upload, 600.0),
        (DocumentsResource.upload_many, 600.0),
        (AsyncDocumentsResource.wait_until_ready, 600.0),
        (AsyncDocumentsResource.upload, 600.0),
        (AsyncDocumentsResource.upload_many, 600.0),
        (C.extract, 1800.0),
        (AC.extract, 1800.0),
    ]
    for fn, want in direct:
        got = inspect.signature(fn).parameters["max_wait_seconds"].default
        assert got == want, f"{fn.__qualname__}: default={got}, want {want}"


def test_default_deadline_fires(client, httpx_mock, monkeypatch):
    # With the (finite) default, a job that never terminates raises
    # WaitTimeoutError instead of polling forever. Each fake poll advances the
    # clock by 1000s, so the 1800s default trips on the second check.
    _leap_monotonic(monkeypatch)
    # deadline = 1000 (first leap) + 1800; poll 1 checks at 2000, poll 2 trips
    # at 3000 -> exactly two fetches.
    for _ in range(2):
        httpx_mock.add_response(
            method="GET", url="https://api.test/api/jobs/5", json=_job("running")
        )
    with pytest.raises(WaitTimeoutError):
        client.jobs.wait(5)


def test_none_disables_the_deadline(client, httpx_mock, monkeypatch):
    # max_wait_seconds=None is the explicit infinity opt-in: the same fake
    # clock leaps far past the default, and the wait must survive until the
    # job completes.
    _leap_monotonic(monkeypatch)
    for _ in range(10):  # 10 polls x 1000s leap >> 1800s default
        httpx_mock.add_response(
            method="GET", url="https://api.test/api/jobs/5", json=_job("running")
        )
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job("completed"))

    job = client.jobs.wait(5, max_wait_seconds=None)
    assert job.status == "completed"


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
def test_extract_passes_none_through(client, monkeypatch):
    # Composites must forward a user-supplied None verbatim, not substitute
    # their default. Capture what reaches the underlying waits.
    captured: dict[str, object] = {}

    def fake_upload(*args, **kwargs):
        captured["upload_wait"] = kwargs.get("max_wait_seconds", "missing")

        class _R:
            failed: list = []
            outcomes: list = []

            @property
            def document_ids(self):
                return [1]

        r = _R()
        return r

    monkeypatch.setattr(client.documents, "upload", fake_upload)

    def fake_run(*args, **kwargs):
        captured["job_wait"] = kwargs.get("max_wait_seconds", "missing")
        raise RuntimeError("stop here")  # we only care about the forwarded kwargs

    monkeypatch.setattr(client.jobs, "run", fake_run)

    with pytest.raises(RuntimeError):
        client.extract(files=["/tmp/x.pdf"], prompts=[1], max_wait_seconds=None)

    assert captured["job_wait"] is None


def test_unknown_status_warns_once_and_times_out(client, httpx_mock, monkeypatch, caplog):
    # A future status this build doesn't know ("queued") must not hang forever
    # (finite default) and must be diagnosable: exactly one warning per value.
    _leap_monotonic(monkeypatch)
    for _ in range(2):  # two polls before the default deadline trips
        httpx_mock.add_response(
            method="GET", url="https://api.test/api/jobs/5", json=_job("queued")
        )

    with caplog.at_level("WARNING", logger="infersoft"):
        with pytest.raises(WaitTimeoutError):
            client.jobs.wait(5)

    warnings = [r for r in caplog.records if "queued" in r.getMessage()]
    assert len(warnings) == 1, [r.getMessage() for r in caplog.records]


async def test_async_default_deadline_and_none(async_client, httpx_mock, monkeypatch):
    async def _nap(_s):
        return None

    monkeypatch.setattr("infersoft._wait.asyncio.sleep", _nap)
    _leap_monotonic(monkeypatch)

    # Default fires (two polls before the deadline trips).
    for _ in range(2):
        httpx_mock.add_response(
            method="GET", url="https://api.test/api/jobs/5", json=_job("running")
        )
    with pytest.raises(WaitTimeoutError):
        await async_client.jobs.wait(5)

    # None survives.
    for _ in range(10):
        httpx_mock.add_response(
            method="GET", url="https://api.test/api/jobs/6", json=_job("running", jid=6)
        )
    httpx_mock.add_response(
        method="GET", url="https://api.test/api/jobs/6", json=_job("completed", jid=6)
    )
    job = await async_client.jobs.wait(6, max_wait_seconds=None)
    assert job.status == "completed"


def test_known_terminal_status_short_circuits(client, httpx_mock):
    # A terminal status on the FIRST poll returns immediately (no extra polls).
    httpx_mock.add_response(method="GET", url="https://api.test/api/jobs/5", json=_job("failed"))
    job = client.jobs.wait(5)
    assert job.status == "failed"
