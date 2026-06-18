"""Shared fixtures for the test suite."""

from __future__ import annotations

import pytest

from infersoft import AsyncClient, Client

_KWARGS = {
    "client_id": "id",
    "client_secret": "secret",
    "base_url": "https://api.test",
    "token_url": "https://auth.test/oauth/token",
    "audience": "https://api.test",
}
_TOKEN = {"access_token": "test-token", "expires_in": 3600, "token_type": "Bearer"}


@pytest.fixture
def client(httpx_mock):
    # Token fetch (Auth0 client-credentials); consumed on the first authed call.
    httpx_mock.add_response(url="https://auth.test/oauth/token", json=_TOKEN)
    return Client(**_KWARGS)


@pytest.fixture
def async_client(httpx_mock):
    httpx_mock.add_response(url="https://auth.test/oauth/token", json=_TOKEN)
    return AsyncClient(**_KWARGS)
