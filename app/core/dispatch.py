from __future__ import annotations

import asyncio
import hashlib
from contextlib import suppress
from typing import Protocol

from fastapi import BackgroundTasks

from app.config import get_logger
from app.schema.whatsapp import WhatsAppWebhookPayload

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


class ServiceBusWebhookDispatcher:
    """Durable webhook ingress with competing consumers and retry/DLQ behavior."""

    def __init__(
        self,
        *,
        queue_name: str,
        fully_qualified_namespace: str | None = None,
        connection_string: str | None = None,
    ) -> None:
        from azure.servicebus.aio import ServiceBusClient

        self._credential = None
        if connection_string:
            self._client = ServiceBusClient.from_connection_string(connection_string)
        else:
            if not fully_qualified_namespace:
                raise ValueError("Service Bus namespace is required.")
            from azure.identity.aio import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
            self._client = ServiceBusClient(
                fully_qualified_namespace,
                credential=self._credential,
            )
        self._queue_name = queue_name
        self._sender = self._client.get_queue_sender(queue_name)
        self._handler: WebhookHandler | None = None
        self._worker: asyncio.Task[None] | None = None

    async def start(self, handler: WebhookHandler) -> None:
        self._handler = handler
        self._worker = asyncio.create_task(self._run(), name="whatsapp-service-bus-worker")

    async def dispatch(
        self,
        raw_body: bytes,
        webhook: WhatsAppWebhookPayload,
        background_tasks: BackgroundTasks,
    ) -> None:
        from azure.servicebus import ServiceBusMessage

        message_id = hashlib.sha256(raw_body).hexdigest()
        message = ServiceBusMessage(
            raw_body,
            message_id=message_id,
            content_type="application/json",
            subject="whatsapp-webhook",
        )
        await self._sender.send_messages(message)

    async def _run(self) -> None:
        assert self._handler is not None
        receiver = self._client.get_queue_receiver(
            self._queue_name,
            max_wait_time=10,
        )
        async with receiver:
            while True:
                messages = await receiver.receive_messages(
                    max_message_count=5,
                    max_wait_time=10,
                )
                for message in messages:
                    try:
                        payload = WhatsAppWebhookPayload.model_validate_json(
                            _message_bytes(message)
                        )
                        await self._handler.handle(payload)
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        logger.exception(
                            "Queued WhatsApp webhook processing failed delivery_count=%s",
                            message.delivery_count,
                        )
                        if (message.delivery_count or 0) >= 5:
                            await receiver.dead_letter_message(
                                message,
                                reason=type(error).__name__,
                                error_description=str(error)[:512],
                            )
                        else:
                            await receiver.abandon_message(message)
                    else:
                        await receiver.complete_message(message)

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        await self._sender.close()
        await self._client.close()
        if self._credential is not None:
            await self._credential.close()


def _message_bytes(message: object) -> bytes:
    body = getattr(message, "body", None)
    if isinstance(body, bytes):
        return body
    if body is None:
        raise ValueError("Service Bus message has no body.")
    return b"".join(bytes(part) for part in body)
