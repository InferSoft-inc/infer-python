"""Prompts resource (read-only)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

from .._http import AsyncHttpClient, HttpClient
from ..models import PromptMeta, PromptPage


class PromptsResource:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def search(
        self,
        q: str | None = None,
        *,
        document_class: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "name",
        order_dir: str = "asc",
    ) -> PromptPage:
        """Search extractor prompts. ``q`` is a fuzzy match on the prompt name."""
        body: dict[str, Any] = {
            "include_deleted": include_deleted,
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if q is not None:
            body["q"] = q
        if document_class is not None:
            body["document_class"] = document_class
        raw = self._http.request("POST", "/api/prompts/search", json=body, idempotent=True)
        return PromptPage.model_validate(raw)

    def iterate(
        self,
        q: str | None = None,
        *,
        document_class: str | None = None,
        include_deleted: bool = False,
        page_size: int = 50,
        order_by: str = "name",
        order_dir: str = "asc",
    ) -> Iterator[PromptMeta]:
        """Yield every matching prompt, fetching pages on demand."""
        page = 1
        while True:
            result = self.search(
                q,
                document_class=document_class,
                include_deleted=include_deleted,
                page=page,
                page_size=page_size,
                order_by=order_by,
                order_dir=order_dir,
            )
            yield from result.items
            if not result.has_more:
                return
            page += 1


class AsyncPromptsResource:
    def __init__(self, http: AsyncHttpClient) -> None:
        self._http = http

    async def search(
        self,
        q: str | None = None,
        *,
        document_class: str | None = None,
        include_deleted: bool = False,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "name",
        order_dir: str = "asc",
    ) -> PromptPage:
        """Search extractor prompts. ``q`` is a fuzzy match on the prompt name."""
        body: dict[str, Any] = {
            "include_deleted": include_deleted,
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if q is not None:
            body["q"] = q
        if document_class is not None:
            body["document_class"] = document_class
        raw = await self._http.request("POST", "/api/prompts/search", json=body, idempotent=True)
        return PromptPage.model_validate(raw)

    async def iterate(
        self,
        q: str | None = None,
        *,
        document_class: str | None = None,
        include_deleted: bool = False,
        page_size: int = 50,
        order_by: str = "name",
        order_dir: str = "asc",
    ) -> AsyncIterator[PromptMeta]:
        """Yield every matching prompt, fetching pages on demand."""
        page = 1
        while True:
            result = await self.search(
                q,
                document_class=document_class,
                include_deleted=include_deleted,
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
