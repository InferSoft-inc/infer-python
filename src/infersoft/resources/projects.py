"""Projects resource."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from .._http import AsyncHttpClient, HttpClient, new_idempotency_key
from ..errors import ConflictError
from ..models import AssignDocumentsResult, Project, ProjectPage
from ..selectors import _resolve_selectors

Selectors = dict[str, Any]


class ProjectsResource:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def create(self, name: str, *, idempotency_key: str | None = None) -> Project:
        raw = self._http.request(
            "POST",
            "/api/projects",
            json={"name": name},
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Project.model_validate(raw)

    def get(self, project_id: int) -> Project:
        raw = self._http.request("GET", f"/api/projects/{project_id}")
        return Project.model_validate(raw)

    def get_or_create(self, name: str, *, idempotency_key: str | None = None) -> Project:
        """Return the project whose name equals ``name`` exactly, creating it if absent.

        The match is exact and case-sensitive (the server's search is fuzzy, so
        results are filtered client-side). Safe to call from re-runnable
        ingestion scripts: a creation race that loses to a concurrent run is
        resolved by re-fetching the winner.
        """
        for p in self.iterate(name):
            if p.name == name:
                return p
        try:
            return self.create(name, idempotency_key=idempotency_key)
        except ConflictError:
            # Lost a creation race; the project exists now.
            for p in self.iterate(name):
                if p.name == name:
                    return p
            raise

    def search(
        self,
        q: str | None = None,
        *,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> ProjectPage:
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if q is not None:
            body["q"] = q
        # Read-only search: safe to retry on transient failures.
        raw = self._http.request("POST", "/api/projects/search", json=body, idempotent=True)
        return ProjectPage.model_validate(raw)

    def iterate(
        self,
        q: str | None = None,
        *,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> Iterator[Project]:
        """Yield every matching project, fetching pages on demand."""
        page = 1
        while True:
            result = self.search(
                q,
                page=page,
                page_size=page_size,
                order_by=order_by,
                order_dir=order_dir,
            )
            yield from result.items
            if not result.has_more:
                return
            page += 1

    def assign_documents(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        project_id: int | None = None,
        project_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> AssignDocumentsResult:
        """Assign documents (matched by ``selectors`` or ``document_ids``) to a project.

        Pass ``project_id`` to attach them to an existing project, or
        ``project_name`` to create a new project and assign them to it.
        """
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {"selectors": selectors}
        if project_id is not None:
            body["project_id"] = project_id
        if project_name is not None:
            body["project_name"] = project_name
        raw = self._http.request(
            "POST",
            "/api/projects/assign-documents",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return AssignDocumentsResult.model_validate(raw)


class AsyncProjectsResource:
    def __init__(self, http: AsyncHttpClient) -> None:
        self._http = http

    async def create(self, name: str, *, idempotency_key: str | None = None) -> Project:
        raw = await self._http.request(
            "POST",
            "/api/projects",
            json={"name": name},
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Project.model_validate(raw)

    async def get(self, project_id: int) -> Project:
        raw = await self._http.request("GET", f"/api/projects/{project_id}")
        return Project.model_validate(raw)

    async def get_or_create(self, name: str, *, idempotency_key: str | None = None) -> Project:
        """Async :meth:`ProjectsResource.get_or_create`."""
        async for p in self.iterate(name):
            if p.name == name:
                return p
        try:
            return await self.create(name, idempotency_key=idempotency_key)
        except ConflictError:
            async for p in self.iterate(name):
                if p.name == name:
                    return p
            raise

    async def search(
        self,
        q: str | None = None,
        *,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> ProjectPage:
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if q is not None:
            body["q"] = q
        raw = await self._http.request("POST", "/api/projects/search", json=body, idempotent=True)
        return ProjectPage.model_validate(raw)

    async def iterate(
        self,
        q: str | None = None,
        *,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> AsyncIterator[Project]:
        """Yield every matching project, fetching pages on demand."""
        page = 1
        while True:
            result = await self.search(
                q,
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

    async def assign_documents(
        self,
        selectors: Selectors | None = None,
        *,
        document_ids: Sequence[int] | None = None,
        project_id: int | None = None,
        project_name: str | None = None,
        idempotency_key: str | None = None,
    ) -> AssignDocumentsResult:
        """Assign documents (matched by ``selectors`` or ``document_ids``) to a project."""
        selectors = _resolve_selectors(selectors, document_ids, required=True)
        body: dict[str, Any] = {"selectors": selectors}
        if project_id is not None:
            body["project_id"] = project_id
        if project_name is not None:
            body["project_name"] = project_name
        raw = await self._http.request(
            "POST",
            "/api/projects/assign-documents",
            json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return AssignDocumentsResult.model_validate(raw)
