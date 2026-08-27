from __future__ import annotations

import asyncio
import hashlib
import re
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
    @property
    def is_running(self) -> bool: ...

    async def start(self, handler: WebhookHandler) -> None: ...

    async def dispatch(
        self,
        raw_body: bytes,
        webhook: WhatsAppWebhookPayload,
        background_tasks: BackgroundTasks,
    ) -> None: ...

    async def close(self) -> None: ...


class DirectWebhookDispatcher:
    def __init__(self) -> None:
        self._handler: WebhookHandler | None = None

    @property
    def is_running(self) -> bool:
        return self._handler is not None

    async def start(self, handler: WebhookHandler) -> None:
        self._handler = handler

    async def dispatch(
        self,
        raw_body: bytes,
        webhook: WhatsAppWebhookPayload,
        background_tasks: BackgroundTasks,
    ) -> None:
        if self._handler is None:
            raise RuntimeError("The webhook dispatcher has not started.")
        background_tasks.add_task(self._handler.handle, webhook)

    async def close(self) -> None:
        self._handler = None


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
        self._poll_healthy = False

    @property
    def is_running(self) -> bool:
        return (
            self._handler is not None
            and self._worker is not None
            and not self._worker.done()
            and self._poll_healthy
        )

    async def start(self, handler: WebhookHandler) -> None:
        await self._container.get_container_properties()
        self._handler = handler
        self._poll_healthy = True
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
            pending_name = self._pending_name(unit)
            terminal = self._container.get_blob_client(
                self._terminal_name(pending_name)
            )
            if await terminal.exists():
                # A success/dead-letter terminal receipt outlives the deleted pending
                # item, so a later Meta redelivery cannot silently restart processing.
                continue
            blob = self._container.get_blob_client(pending_name)
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
                self._poll_healthy = True
                await asyncio.sleep(self._poll_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._poll_healthy = False
                logger.error(
                    "Blob webhook inbox polling failed error_type=%s; retrying",
                    type(error).__name__,
                )
                await asyncio.sleep(self._poll_seconds)

    async def _pending_names(self) -> list[str]:
        first_per_seller: dict[str, tuple[int, float, int, str]] = {}
        prefix = f"{self._prefix}/pending/" if self._prefix else "pending/"
        async for item in self._container.list_blobs(name_starts_with=prefix):
            relative = item.name[len(prefix) :]
            segments = relative.split("/", 1)
            # New queue entries are partitioned by the opaque seller-session digest.
            # Legacy flat entries did not expose a safe seller partition, so serialize
            # them through one bounded rollout partition instead of retaining one memory
            # entry per legacy message or processing them concurrently.
            session_id = segments[0] if len(segments) == 2 else "legacy"
            created = getattr(item, "creation_time", None)
            created_order = (
                float(created.timestamp())
                if created is not None and hasattr(created, "timestamp")
                else 0.0
            )
            filename = segments[1] if len(segments) == 2 else segments[0]
            order_match = re.match(r"^(\d{10,})-(?:(\d{4})-)?", filename)
            event_order = int(order_match.group(1)) if order_match else 0
            sequence_order = (
                int(order_match.group(2))
                if order_match is not None and order_match.group(2) is not None
                else 0
            )
            # Meta's event timestamp is the authoritative seller-message order. Blob
            # creation time is only a tie-breaker because concurrent webhook requests can
            # finish uploading in the opposite order. Sequence preserves order inside a
            # single Meta batch when timestamps and creation times are equal.
            candidate = (event_order, created_order, sequence_order, item.name)
            current = first_per_seller.get(session_id)
            if current is None or candidate < current:
                first_per_seller[session_id] = candidate
        # Retain only one candidate per seller while streaming the listing. Memory now
        # grows with active seller partitions rather than the number of queued messages,
        # so one seller's large backlog cannot starve others or fill the worker's memory.
        # Every app instance attempts the same globally oldest seller heads; competing
        # instances contend for those Blob leases rather than processing a seller's later
        # messages concurrently.
        ordered = sorted(first_per_seller.values())
        return [name for _event, _created, _sequence, name in ordered[:100]]

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
            terminal_name = self._terminal_name(name)
            terminal = self._container.get_blob_client(terminal_name)
            if await terminal.exists():
                # Recover the narrow crash window between writing a terminal receipt and
                # deleting the pending item without invoking the domain handler again.
                await blob.delete_blob(lease=lease)
                deleted = True
                return
            stream = await blob.download_blob(lease=lease)
            content = await stream.readall()
            payload = WhatsAppWebhookPayload.model_validate_json(content)
            assert self._handler is not None
            await self._handler.handle(payload)
            await self._write_terminal_receipt(
                terminal_name,
                state="completed",
            )
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
                    await self._write_terminal_receipt(
                        self._terminal_name(name),
                        state="dead_letter",
                    )
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
        self._handler = None
        self._poll_healthy = False
        if self._service is not None:
            await self._service.close()
        if self._credential is not None:
            await self._credential.close()

    def _pending_name(self, unit: "DispatchUnit") -> str:
        timestamp = unit.enqueued_at if unit.enqueued_at.isdigit() else "0000000000"
        filename = f"{timestamp.zfill(10)}-{unit.sequence:04d}-{unit.message_id}.json"
        base = f"{self._prefix}/pending" if self._prefix else "pending"
        return f"{base}/{unit.session_id}/{filename}"

    def _dead_letter_name(self, pending_name: str) -> str:
        pending_segment = f"{self._prefix}/pending/" if self._prefix else "pending/"
        dead_segment = (
            f"{self._prefix}/dead-letter/" if self._prefix else "dead-letter/"
        )
        return pending_name.replace(pending_segment, dead_segment, 1)

    def _terminal_name(self, pending_name: str) -> str:
        pending_segment = f"{self._prefix}/pending/" if self._prefix else "pending/"
        relative = pending_name.removeprefix(pending_segment)
        segments = relative.split("/", 1)
        if len(segments) == 2:
            session_id, filename = segments
        else:
            # Legacy flat queue entries did not carry a seller-session partition. Keep
            # them isolated under an opaque legacy partition during rolling upgrades.
            session_id, filename = "legacy", segments[0]
        if not re.fullmatch(r"[0-9a-f]{64}", session_id):
            session_id = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        match = re.search(r"([0-9a-f]{64})\.json$", filename)
        message_id = (
            match.group(1)
            if match is not None
            else hashlib.sha256(relative.encode("utf-8")).hexdigest()
        )
        base = f"{self._prefix}/terminal" if self._prefix else "terminal"
        # Timestamp and batch sequence are deliberately excluded. Meta may redeliver the
        # same message in a differently ordered batch; seller + opaque message digest is
        # the stable terminal identity.
        return f"{base}/{session_id}/{message_id}.json"

    async def _write_terminal_receipt(self, name: str, *, state: str) -> None:
        from azure.core.exceptions import ResourceExistsError

        terminal = self._container.get_blob_client(name)
        try:
            await terminal.upload_blob(
                b"",
                overwrite=False,
                metadata={"state": state},
            )
        except ResourceExistsError:
            # The receipt is immutable. A competing/recovered worker that observes the
            # same name must not replace its terminal decision.
            pass


@dataclass(frozen=True)
class DispatchUnit:
    body: bytes
    message_id: str
    session_id: str
    enqueued_at: str
    sequence: int = 0


def _dispatch_units(webhook: WhatsAppWebhookPayload) -> tuple[DispatchUnit, ...]:
    """Split a Meta batch into one queue message per seller message."""
    units: list[DispatchUnit] = []
    sequence = 0
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
                                    value=WhatsAppWebhookValue(
                                        metadata=change.value.metadata,
                                        messages=[incoming],
                                    )
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
                        sequence=sequence,
                    )
                )
                sequence += 1
    return tuple(units)
