from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .models import WorkflowSession


class WorkflowConflictError(RuntimeError):
    pass


class WorkflowStore(Protocol):
    async def get(self, thread_id: str) -> tuple[WorkflowSession | None, str | None]: ...

    async def save(
        self,
        session: WorkflowSession,
        expected_version: str | None,
    ) -> str: ...

    async def close(self) -> None: ...


class InMemoryWorkflowStore:
    def __init__(self) -> None:
        self._documents: dict[str, tuple[WorkflowSession, int]] = {}
        self._lock = asyncio.Lock()

    async def get(self, thread_id: str) -> tuple[WorkflowSession | None, str | None]:
        async with self._lock:
            stored = self._documents.get(thread_id)
            if stored is None:
                return None, None
            session, version = stored
            return session.model_copy(deep=True), str(version)

    async def save(
        self,
        session: WorkflowSession,
        expected_version: str | None,
    ) -> str:
        async with self._lock:
            current = self._documents.get(session.thread_id)
            current_version = str(current[1]) if current else None
            if current_version != expected_version:
                raise WorkflowConflictError("Workflow state changed concurrently.")
            next_version = (current[1] + 1) if current else 1
            self._documents[session.thread_id] = (
                session.model_copy(deep=True),
                next_version,
            )
            return str(next_version)

    async def close(self) -> None:
        self._documents.clear()


class AzureBlobWorkflowStore:
    """One JSON blob per workflow with ETag-based optimistic concurrency."""

    def __init__(
        self,
        *,
        container_name: str,
        prefix: str = "sessions",
        account_url: str | None = None,
        connection_string: str | None = None,
        session_ttl_hours: int = 24,
        container_client: Any | None = None,
    ) -> None:
        self._credential = None
        self._service = None
        if container_client is not None:
            self._container = container_client
        else:
            if not (connection_string or account_url):
                raise ValueError("An account URL or connection string is required.")
            from azure.storage.blob.aio import BlobServiceClient

            if connection_string:
                self._service = BlobServiceClient.from_connection_string(
                    connection_string
                )
            else:
                from azure.identity.aio import DefaultAzureCredential

                self._credential = DefaultAzureCredential()
                self._service = BlobServiceClient(
                    account_url=account_url,
                    credential=self._credential,
                )
            self._container = self._service.get_container_client(container_name)
        self._prefix = prefix.strip("/")
        self._session_ttl = timedelta(hours=session_ttl_hours)

    async def connect(self) -> None:
        await self._container.get_container_properties()

    async def get(self, thread_id: str) -> tuple[WorkflowSession | None, str | None]:
        from azure.core.exceptions import ResourceNotFoundError

        blob = self._blob(thread_id)
        try:
            stream = await blob.download_blob()
            content = await stream.readall()
        except ResourceNotFoundError:
            return None, None
        session = WorkflowSession.model_validate_json(content)
        etag = getattr(getattr(stream, "properties", None), "etag", None)
        version = str(etag) if etag else None
        if session.updated_at + self._session_ttl <= datetime.now(UTC):
            return None, version
        return session, version

    async def save(
        self,
        session: WorkflowSession,
        expected_version: str | None,
    ) -> str:
        from azure.core import MatchConditions
        from azure.core.exceptions import (
            ResourceExistsError,
            ResourceModifiedError,
        )
        from azure.storage.blob import ContentSettings

        blob = self._blob(session.thread_id)
        content = session.model_dump_json().encode("utf-8")
        options: dict[str, object] = {
            "content_settings": ContentSettings(content_type="application/json"),
            "metadata": {"workflow": "ssp-licensing"},
        }
        try:
            if expected_version is None:
                result = await blob.upload_blob(content, overwrite=False, **options)
            else:
                result = await blob.upload_blob(
                    content,
                    overwrite=True,
                    etag=expected_version,
                    match_condition=MatchConditions.IfNotModified,
                    **options,
                )
            return str(result.get("etag", ""))
        except (ResourceExistsError, ResourceModifiedError) as error:
            raise WorkflowConflictError("Workflow state changed concurrently.") from error

    async def close(self) -> None:
        if self._service is not None:
            await self._service.close()
        if self._credential is not None:
            await self._credential.close()

    def _blob(self, thread_id: str) -> Any:
        name = f"{self._prefix}/{thread_id}.json" if self._prefix else f"{thread_id}.json"
        return self._container.get_blob_client(name)
