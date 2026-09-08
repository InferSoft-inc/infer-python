"""jobs.quote(): read-only credit calculation, nothing reserved."""

from __future__ import annotations

import json

import pytest

from infersoft import Client, CreditsQuote

_QUOTE = {"total_credits": 17, "page_count": 9, "document_count": 4}
_FILE_SEL = {"include": [{"type": "fileSelector", "files": [1, 2]}], "exclude": []}


def test_quote_sends_estimate_shaped_body_without_idempotency_key(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/quote",
        json=_QUOTE,
    )

    quote = client.jobs.quote(step="extractor", document_ids=[1, 2], prompts=[5], synchronous=True)

    assert quote.total_credits == 17
    assert quote.page_count == 9
    assert quote.document_count == 4
    # Nothing was reserved, so there is no credits id to hand to jobs.start().
    # Asserted against the declared fields, not the instance: _Model allows
    # extras, so an `id` in the payload would be carried on the object anyway.
    assert "id" not in CreditsQuote.model_fields

    post = [r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/credits/quote"][0]
    body = json.loads(post.content)
    assert body == {
        "steps": ["extractor"],
        "selectors": _FILE_SEL,
        "synchronous": True,
        "prompts": [5],
    }
    # Read-only: no idempotency machinery.
    assert "Idempotency-Key" not in post.headers


def test_quote_omits_prompts_when_not_given(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/quote",
        json=_QUOTE,
    )

    client.jobs.quote(step="splitter", document_ids=[1, 2])

    post = [r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/credits/quote"][0]
    body = json.loads(post.content)
    assert "prompts" not in body
    assert body["synchronous"] is False


def test_quote_is_retried_on_transient_failure(client, httpx_mock):
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/quote",
        status_code=503,
        json={"title": "unavailable"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/quote",
        json=_QUOTE,
    )

    quote = client.jobs.quote(step="splitter", document_ids=[1])

    assert quote.total_credits == 17
    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/credits/quote"]
    assert len(posts) == 2


def test_quote_requires_selectors_or_document_ids():
    # No httpx_mock: the call raises before any network request.
    bare = Client(
        client_id="id",
        client_secret="secret",
        base_url="https://api.test",
        token_url="https://auth.test/oauth/token",
        audience="https://api.test",
    )
    with pytest.raises(ValueError):
        bare.jobs.quote(step="splitter")


async def test_async_quote_reimplements_the_sync_contract(async_client, httpx_mock):
    # AsyncJobsResource.quote is a second implementation, not a wrapper delegating
    # to the sync one, so every part of the contract is re-pinned against that
    # copy: path, body, unkeyed headers, and retry-on-transient. Mutating any of
    # them in the async method leaves the rest of the suite green — this is the
    # only test that fails.
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/quote",
        status_code=503,
        json={"title": "unavailable"},
    )
    httpx_mock.add_response(
        method="POST",
        url="https://api.test/api/jobs/credits/quote",
        json=_QUOTE,
    )

    quote = await async_client.jobs.quote(
        step="extractor", document_ids=[1, 2], prompts=[5], synchronous=True
    )

    assert isinstance(quote, CreditsQuote)
    assert quote.total_credits == 17

    posts = [r for r in httpx_mock.get_requests() if r.url.path == "/api/jobs/credits/quote"]
    # Read-only and unkeyed, but still opted into retries via `idempotent=True`.
    assert len(posts) == 2
    # Asserted over every attempt rather than the first: a replay that altered the
    # body, or grew an Idempotency-Key on the way, would otherwise pass unnoticed.
    for post in posts:
        assert json.loads(post.content) == {
            "steps": ["extractor"],
            "selectors": _FILE_SEL,
            "synchronous": True,
            "prompts": [5],
        }
        assert "Idempotency-Key" not in post.headers
