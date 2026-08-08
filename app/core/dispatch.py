from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from dataclasses import dataclass
from collections.abc import Callable
from typing import Any, Protocol

from fastapi import BackgroundTasks

from app.config import get_logger
from app.schema.whatsapp import (
    WhatsAppWebhookChange,
    WhatsAppWebhookEntry,
    WhatsAppWebhookPayload,
    WhatsAppWebhookValue,
)

logger = get_logger(__name__)


class WebhookHandler(Protocol):
    async def handle(self, webhook: WhatsAppWebhookPayload) -> None: ...


class WebhookDispatcher(Protocol):
    async def start(self, handler: WebhookHandler) -> None: ...

    async def dispatch(
        self,
        raw_body: bytes,
        webhook: WhatsAppWebhookPayload,
        background_tasks: BackgroundTasks,
    ) -> None: ...

    async def close(self) -> None: ...


class DirectWebhookDispatcher:
    async def start(self, handler: WebhookHandler) -> None:
        self._handler = handler

    async def dispatch(
        self,
        raw_body: bytes,
        webhook: WhatsAppWebhookPayload,
        background_tasks: BackgroundTasks,
    ) -> None:
        background_tasks.add_task(self._handler.handle, webhook)

    async def close(self) -> None:
        return None


class AzureBlobWebhookDispatcher:
    """Durable webhook inbox backed by the existing workflow Blob container.

    One worker processes persisted messages sequentially on the single B1 instance. A
    bounded Blob lease prevents overlapping app processes from handling the same item.
    """

    def __init__(
        self,
        *,
        container_name: str,
        prefix: str = "webhook-queue",
        account_url: str | None = None,
        connection_string: str | None = None,
        poll_seconds: float = 1.0,
        max_delivery_count: int = 5,
        container_client: Any | None = None,
        lease_factory: Callable[[Any], Any] | None = None,
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
        self._poll_seconds = poll_seconds
        self._max_delivery_count = max_delivery_count
        self._lease_factory = lease_factory
        self._handler: WebhookHandler | None = None
        self._worker: asyncio.Task[None] | None = None

    async def start(self, handler: WebhookHandler) -> None:
        await self._container.get_container_properties()
        self._handler = handler
        self._worker = asyncio.create_task(
            self._run(),
            name="whatsapp-blob-inbox-worker",
        )

    async def dispatch(
        self,
        raw_body: bytes,
        webhook: WhatsAppWebhookPayload,
        background_tasks: BackgroundTasks,
    ) -> None:
        from azure.core.exceptions import ResourceExistsError
        from azure.storage.blob import ContentSettings

        del raw_body, background_tasks
        for unit in _dispatch_units(webhook):
            blob = self._container.get_blob_client(self._pending_name(unit))
            try:
                await blob.upload_blob(
                    unit.body,
                    overwrite=False,
                    content_settings=ContentSettings(content_type="application/json"),
                    metadata={
                        "delivery_count": "0",
                        "seller_session": unit.session_id,
                    },
                )
            except ResourceExistsError:
                # Meta may redeliver the same webhook. The deterministic blob name
                # makes the persistence step idempotent before workflow processing.
                continue

    async def _run(self) -> None:
        assert self._handler is not None
        while True:
            try:
                for name in await self._pending_names():
                    await self._process(name)
                await asyncio.sleep(self._poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.error(
                    "Blob webhook inbox polling failed error_type=%s; retrying",
                    type(error).__name__,
                )
                await asyncio.sleep(self._poll_seconds)

    async def _pending_names(self) -> list[str]:
        names: list[str] = []
        prefix = f"{self._prefix}/pending/" if self._prefix else "pending/"
        async for item in self._container.list_blobs(name_starts_with=prefix):
            names.append(item.name)
            if len(names) >= 100:
                break
        return sorted(names)

    async def _process(self, name: str) -> None:
        from azure.core.exceptions import (
            HttpResponseError,
            ResourceExistsError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )
        blob = self._container.get_blob_client(name)
        if self._lease_factory is None:
            from azure.storage.blob.aio import BlobLeaseClient

            lease = BlobLeaseClient(blob)
        else:
            lease = self._lease_factory(blob)
        try:
            await lease.acquire(lease_duration=60)
        except (HttpResponseError, ResourceModifiedError, ResourceNotFoundError):
            return

        deleted = False
        content: bytes | None = None
        lease_renewer = asyncio.create_task(self._renew_lease(lease))
        try:
            stream = await blob.download_blob(lease=lease)
            content = await stream.readall()
            payload = WhatsAppWebhookPayload.model_validate_json(content)
            assert self._handler is not None
            await self._handler.handle(payload)
            await blob.delete_blob(lease=lease)
            deleted = True
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.error(
                "Persisted WhatsApp webhook processing failed error_type=%s",
                type(error).__name__,
            )
            try:
                properties = await blob.get_blob_properties(lease=lease)
                metadata = dict(properties.metadata or {})
                delivery_count = int(metadata.get("delivery_count", "0")) + 1
                metadata["delivery_count"] = str(delivery_count)
                metadata["last_error_type"] = type(error).__name__[:128]
                if delivery_count >= self._max_delivery_count and content is not None:
                    dead_letter = self._container.get_blob_client(
                        self._dead_letter_name(name)
                    )
                    try:
                        await dead_letter.upload_blob(
                            content,
                            overwrite=False,
                            metadata=metadata,
                        )
                    except ResourceExistsError:
                        pass
                    await blob.delete_blob(lease=lease)
                    deleted = True
                else:
                    await blob.set_blob_metadata(metadata, lease=lease)
            except (HttpResponseError, ResourceModifiedError, ResourceNotFoundError):
                # A concurrent deployment/restart may have changed the lease or blob.
                # The still-pending item will be retried by the next polling cycle.
                pass
        finally:
            lease_renewer.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await lease_renewer
            if not deleted:
                with suppress(HttpResponseError, ResourceNotFoundError):
                    await lease.release()

    async def _renew_lease(self, lease: Any) -> None:
        while True:
            await asyncio.sleep(30)
            await lease.renew()

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None
        if self._service is not None:
            await self._service.close()
        if self._credential is not None:
            await self._credential.close()

    def _pending_name(self, unit: "DispatchUnit") -> str:
        timestamp = unit.enqueued_at if unit.enqueued_at.isdigit() else "0000000000"
        filename = f"{timestamp.zfill(10)}-{unit.message_id}.json"
        return f"{self._prefix}/pending/{filename}" if self._prefix else f"pending/{filename}"

    def _dead_letter_name(self, pending_name: str) -> str:
        pending_segment = f"{self._prefix}/pending/" if self._prefix else "pending/"
        dead_segment = (
            f"{self._prefix}/dead-letter/" if self._prefix else "dead-letter/"
        )
        return pending_name.replace(pending_segment, dead_segment, 1)


@dataclass(frozen=True)
class DispatchUnit:
    body: bytes
    message_id: str
    session_id: str
    enqueued_at: str


def _dispatch_units(webhook: WhatsAppWebhookPayload) -> tuple[DispatchUnit, ...]:
    """Split a Meta batch into one queue message per seller message."""
    units: list[DispatchUnit] = []
    for entry in webhook.entry:
        for change in entry.changes:
            for incoming in change.value.messages:
                sender = incoming.sender.lstrip("+")
                single_message_payload = WhatsAppWebhookPayload(
                    object=webhook.object,
                    entry=[
                        WhatsAppWebhookEntry(
                            changes=[
                                WhatsAppWebhookChange(
                                    value=WhatsAppWebhookValue(messages=[incoming])
                                )
                            ]
                        )
                    ],
                )
                units.append(
                    DispatchUnit(
                        body=single_message_payload.model_dump_json(
                            by_alias=True,
                            exclude_none=True,
                        ).encode("utf-8"),
                        message_id=hashlib.sha256(
                            f"{sender}:{incoming.id}".encode("utf-8")
                        ).hexdigest(),
                        session_id=hashlib.sha256(sender.encode("utf-8")).hexdigest(),
                        enqueued_at=incoming.timestamp or "0000000000",
                    )
                )
    return tuple(units)
