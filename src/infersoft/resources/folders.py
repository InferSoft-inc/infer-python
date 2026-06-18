"""Folders resource."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from .._http import AsyncHttpClient, HttpClient, new_idempotency_key
from ..errors import InfersoftError
from ..models import (
    Folder,
    FolderBulkDeleteResult,
    FolderMoveResult,
    FolderPage,
    FolderPathsResult,
)


class FoldersResource:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    def create(self, name: str, *, parent_id: int | None = None,
               idempotency_key: str | None = None) -> Folder:
        """Create a folder. Omit ``parent_id`` to create it at the root."""
        body: dict[str, Any] = {"name": name}
        if parent_id is not None:
            body["parent_id"] = parent_id
        raw = self._http.request(
            "POST", "/api/folders", json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Folder.model_validate(raw)

    def get(self, folder_id: int) -> Folder:
        raw = self._http.request("GET", f"/api/folders/{folder_id}")
        return Folder.model_validate(raw)

    def rename(self, folder_id: int, name: str, *,
               idempotency_key: str | None = None) -> Folder:
        raw = self._http.request(
            "PATCH", f"/api/folders/{folder_id}", json={"name": name},
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Folder.model_validate(raw)

    def search(
        self,
        *,
        parent_id: int | None = None,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> FolderPage:
        """List folders. Omit ``parent_id`` to list root folders."""
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        raw = self._http.request("POST", "/api/folders/search", json=body, idempotent=True)
        return FolderPage.model_validate(raw)

    def iterate(
        self,
        *,
        parent_id: int | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> Iterator[Folder]:
        """Yield every matching folder, fetching pages on demand."""
        page = 1
        while True:
            result = self.search(
                parent_id=parent_id,
                page=page,
                page_size=page_size,
                order_by=order_by,
                order_dir=order_dir,
            )
            yield from result.items
            if not result.has_more:
                return
            page += 1

    def move(self, folder_ids: Sequence[int], *, target_parent_id: int | None = None,
             idempotency_key: str | None = None) -> FolderMoveResult:
        """Move folders under a new parent. Omit ``target_parent_id`` for root."""
        body: dict[str, Any] = {"folder_ids": list(folder_ids)}
        if target_parent_id is not None:
            body["target_parent_id"] = target_parent_id
        raw = self._http.request(
            "POST", "/api/folders/move", json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return FolderMoveResult.model_validate(raw)

    def bulk_delete(self, folder_ids: Sequence[int], *,
                    idempotency_key: str | None = None) -> FolderBulkDeleteResult:
        body: dict[str, Any] = {"folder_ids": list(folder_ids)}
        raw = self._http.request(
            "POST", "/api/folders/bulk_delete", json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return FolderBulkDeleteResult.model_validate(raw)

    def resolve_paths(self, paths: Sequence[str], *,
                      parent_id: int | None = None) -> FolderPathsResult:
        """Resolve (creating if missing) each ``/``-delimited folder path.

        This is get-or-create by path and therefore naturally idempotent, so it
        is safe to retry without an idempotency key. ``results`` preserves the
        order of ``paths``. Omit ``parent_id`` to resolve from the root.
        """
        body: dict[str, Any] = {"paths": list(paths)}
        if parent_id is not None:
            body["parent_id"] = parent_id
        raw = self._http.request("POST", "/api/folders/paths", json=body, idempotent=True)
        return FolderPathsResult.model_validate(raw)

    def ensure_path(self, path: str, *, parent_id: int | None = None) -> Folder:
        """Get-or-create a single ``/``-delimited folder path; return the leaf.

        Convenience over :meth:`resolve_paths` for the common one-path case::

            folder = client.folders.ensure_path("2026/Q1 Invoices")
        """
        if not path or not path.strip("/ "):
            raise ValueError("ensure_path() requires a non-empty folder path")
        result = self.resolve_paths([path], parent_id=parent_id)
        folders = result.results[0].folders if result.results else []
        if not folders:
            raise InfersoftError(f"could not resolve folder path {path!r}")
        return folders[-1]


class AsyncFoldersResource:
    def __init__(self, http: AsyncHttpClient) -> None:
        self._http = http

    async def create(self, name: str, *, parent_id: int | None = None,
                     idempotency_key: str | None = None) -> Folder:
        """Create a folder. Omit ``parent_id`` to create it at the root."""
        body: dict[str, Any] = {"name": name}
        if parent_id is not None:
            body["parent_id"] = parent_id
        raw = await self._http.request(
            "POST", "/api/folders", json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Folder.model_validate(raw)

    async def get(self, folder_id: int) -> Folder:
        raw = await self._http.request("GET", f"/api/folders/{folder_id}")
        return Folder.model_validate(raw)

    async def rename(self, folder_id: int, name: str, *,
                     idempotency_key: str | None = None) -> Folder:
        raw = await self._http.request(
            "PATCH", f"/api/folders/{folder_id}", json={"name": name},
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return Folder.model_validate(raw)

    async def search(
        self,
        *,
        parent_id: int | None = None,
        page: int = 1,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> FolderPage:
        """List folders. Omit ``parent_id`` to list root folders."""
        body: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "order_by": order_by,
            "order_dir": order_dir,
        }
        if parent_id is not None:
            body["parent_id"] = parent_id
        raw = await self._http.request("POST", "/api/folders/search", json=body, idempotent=True)
        return FolderPage.model_validate(raw)

    async def iterate(
        self,
        *,
        parent_id: int | None = None,
        page_size: int = 50,
        order_by: str = "id",
        order_dir: str = "asc",
    ) -> AsyncIterator[Folder]:
        """Yield every matching folder, fetching pages on demand."""
        page = 1
        while True:
            result = await self.search(
                parent_id=parent_id,
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

    async def move(self, folder_ids: Sequence[int], *, target_parent_id: int | None = None,
                   idempotency_key: str | None = None) -> FolderMoveResult:
        """Move folders under a new parent. Omit ``target_parent_id`` for root."""
        body: dict[str, Any] = {"folder_ids": list(folder_ids)}
        if target_parent_id is not None:
            body["target_parent_id"] = target_parent_id
        raw = await self._http.request(
            "POST", "/api/folders/move", json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return FolderMoveResult.model_validate(raw)

    async def bulk_delete(self, folder_ids: Sequence[int], *,
                          idempotency_key: str | None = None) -> FolderBulkDeleteResult:
        body: dict[str, Any] = {"folder_ids": list(folder_ids)}
        raw = await self._http.request(
            "POST", "/api/folders/bulk_delete", json=body,
            idempotency_key=idempotency_key or new_idempotency_key(),
        )
        return FolderBulkDeleteResult.model_validate(raw)

    async def resolve_paths(self, paths: Sequence[str], *,
                            parent_id: int | None = None) -> FolderPathsResult:
        """Resolve (creating if missing) each ``/``-delimited folder path."""
        body: dict[str, Any] = {"paths": list(paths)}
        if parent_id is not None:
            body["parent_id"] = parent_id
        raw = await self._http.request("POST", "/api/folders/paths", json=body, idempotent=True)
        return FolderPathsResult.model_validate(raw)

    async def ensure_path(self, path: str, *, parent_id: int | None = None) -> Folder:
        """Async :meth:`FoldersResource.ensure_path`."""
        if not path or not path.strip("/ "):
            raise ValueError("ensure_path() requires a non-empty folder path")
        result = await self.resolve_paths([path], parent_id=parent_id)
        folders = result.results[0].folders if result.results else []
        if not folders:
            raise InfersoftError(f"could not resolve folder path {path!r}")
        return folders[-1]
