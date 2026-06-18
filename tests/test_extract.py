"""The client.extract() end-to-end pipeline."""

from __future__ import annotations

import json

import pytest

from infersoft import Client, ExtractError

_ESTIMATE = {"total_credits": 10, "page_count": 3, "id": "cred-1"}


def _job(status: str, jid: int = 9) -> dict:
    return {
        "id": jid,
        "organization_id": "org",
        "organization_name": "Org",
        "total_docs": 1,
        "completed_docs": 1,
        "error_count": 0 if status == "completed" else 1,
        "status": status,
        "createdAt": "2026-05-29T00:00:00Z",
    }


def _doc(did: int, *, status: str = "ready", extractions: list[dict] | None = None) -> dict:
    return {
        "id": did,
        "organization_id": "org",
        "name": f"doc{did}.pdf",
        "status": status,
        "is_valid": True,
        "created_at": "2026-05-29T00:00:00Z",
        "has_active_workflow": False,
        "extraction_results": extractions or [],
    }


def _page(items: list[dict]) -> dict:
    return {"items": items, "page": 1, "page_size": 50, "has_more": False}


def _mock_job_flow(httpx_mock, status: str = "completed", jid: int = 9) -> None:
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/jobs/credits/estimate", json=_ESTIMATE
    )
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/jobs/start", json=_job("running", jid)
    )
    httpx_mock.add_response(
        method="GET", url=f"https://api.test/api/jobs/{jid}", json=_job(status, jid)
    )


def _bare_client() -> Client:
    return Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )


def test_extract_full_pipeline_from_files(client, httpx_mock, tmp_path):
    doc_file = tmp_path / "invoice.pdf"
    doc_file.write_bytes(b"%PDF data")
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": "invoice.pdf",
                    "document_id": 42,
                    "put_url": "https://s3.test/p",
                    "required_headers": {},
                }
            ]
        },
    )
    httpx_mock.add_response(method="PUT", url="https://s3.test/p", status_code=200)
    # 1st documents/search: the upload readiness poll. 2nd: jobs.results.
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([_doc(42)])
    )
    _mock_job_flow(httpx_mock)
    extracted = _doc(42, extractions=[{"prompt_id": 5, "value": {"name": "Total",
                                                                 "parsed_value": 1234.5}}])
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([extracted])
    )

    result = client.extract(doc_file, prompts=[5])

    assert result.job.status.value == "completed"
    assert result.upload is not None and result.upload.document_ids == [42]
    assert result.values == {42: {"Total": 1234.5}}
    assert result.documents[0].extractions[5].parsed_value == 1234.5

    searches = [
        json.loads(r.content)
        for r in httpx_mock.get_requests()
        if r.url.path == "/api/documents/search"
    ]
    assert searches[0]["selectors"]["include"] == [{"type": "fileSelector", "files": [42]}]
    assert searches[1]["selectors"]["include"] == [{"type": "jobSelector", "jobs": [9]}]


def test_extract_from_existing_document_ids(client, httpx_mock):
    _mock_job_flow(httpx_mock)
    extracted = _doc(1, extractions=[{"prompt_id": 5, "value": {"name": "Total",
                                                                "parsed_value": 9.0}}])
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([extracted])
    )

    result = client.extract(document_ids=[1], prompts=[5])

    assert result.upload is None
    assert result.values == {1: {"Total": 9.0}}
    est = next(
        r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/credits/estimate"
    )
    assert json.loads(est.content)["selectors"]["include"] == [
        {"type": "fileSelector", "files": [1]}
    ]


def test_extract_failed_job_raises_with_partial_state(client, httpx_mock):
    _mock_job_flow(httpx_mock, status="failed")

    with pytest.raises(ExtractError) as exc_info:
        client.extract(document_ids=[1], prompts=[5])

    err = exc_info.value
    assert err.job is not None and err.job.id == 9 and err.job.status.value == "failed"
    assert err.upload is None


def test_extract_partial_success_returns_normally(client, httpx_mock):
    _mock_job_flow(httpx_mock, status="partial_success")
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([_doc(1)])
    )

    result = client.extract(document_ids=[1], prompts=[5])

    assert result.job.status.value == "partial_success"
    assert result.job.error_count == 1  # caller inspects rather than catching


def test_extract_failed_job_with_raise_on_failure_false(client, httpx_mock):
    _mock_job_flow(httpx_mock, status="failed")
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([])
    )

    result = client.extract(document_ids=[1], prompts=[5], raise_on_failure=False)
    assert result.job.status.value == "failed"


def test_extract_aborts_when_every_upload_fails(client, httpx_mock, tmp_path):
    doc_file = tmp_path / "bad.pdf"
    doc_file.write_bytes(b"x")
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/uploads",
        json={
            "items": [
                {
                    "client_file_name": "bad.pdf",
                    "error": {"code": "FILE_TOO_LARGE", "message": "too big"},
                }
            ]
        },
    )

    with pytest.raises(ExtractError) as exc_info:
        client.extract(doc_file, prompts=[5])

    err = exc_info.value
    assert err.job is None and err.upload is not None
    assert err.upload.failed[0].error.code == "FILE_TOO_LARGE"
    # The pipeline stopped before estimating/starting a job.
    assert all(r.url.path != "/api/jobs/credits/estimate" for r in httpx_mock.get_requests())


def test_extract_input_validation():
    bare = _bare_client()
    with pytest.raises(ValueError, match="exactly one"):
        bare.extract(prompts=[5])
    with pytest.raises(ValueError, match="exactly one"):
        bare.extract("a.pdf", document_ids=[1], prompts=[5])
    with pytest.raises(ValueError, match="project_name requires files"):
        bare.extract(document_ids=[1], prompts=[5], project_name="P")


async def test_async_extract_from_document_ids(async_client, httpx_mock):
    _mock_job_flow(httpx_mock)
    extracted = _doc(1, extractions=[{"prompt_id": 5, "value": {"name": "Total",
                                                                "parsed_value": 7.5}}])
    httpx_mock.add_response(
        method="POST", url="https://api.test/api/documents/search", json=_page([extracted])
    )

    result = await async_client.extract(document_ids=[1], prompts=[5])

    assert result.values == {1: {"Total": 7.5}}
    assert result.job.status.value == "completed"