from __future__ import annotations

import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from app.api.whatsapp.service import WhatsAppWebhookService
from app.core.dispatch import AzureBlobWebhookDispatcher, _dispatch_units
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppAPIError
from tests.test_production_hardening import (
    _FakeContainer,
    _FakeLease,
    _batched_webhook,
)


class _CrashAfterCommitHandler:
    def __init__(self) -> None:
        self.committed_side_effects = 0
        self.crash = True

    async def handle(self, webhook: object) -> None:
        del webhook
        self.committed_side_effects += 1
        if self.crash:
            # A process termination is intentionally outside `except Exception`.
            raise SystemExit("synthetic process termination after commit")


class _FailSecondChunkClient:
    def __init__(self) -> None:
        self.delivered: list[str] = []
        self.fail_second = True

    async def send_message(self, message: object) -> dict[str, object]:
        body = message.text.body  # type: ignore[attr-defined]
        if body == "second" and self.fail_second:
            self.fail_second = False
            raise WhatsAppAPIError("synthetic partial delivery", network_error=True)
        self.delivered.append(body)
        return {"messages": [{"id": "synthetic"}]}


class _AlwaysFailHandler:
    async def handle(self, webhook: object) -> None:
        del webhook
        raise RuntimeError("synthetic permanent failure")


class DocumentedDeliveryLimitations(unittest.IsolatedAsyncioTestCase):
    """Executable evidence for delivery guarantees that Blob alone cannot provide.

    These expected failures must not be converted to ordinary passing tests by merely
    suppressing retries. A real fix requires an atomic domain-event outbox and a provider
    idempotency/reconciliation mechanism, as documented in DELIVERY_SEMANTICS.md.
    """

    @unittest.expectedFailure
    async def test_process_crash_after_domain_commit_is_not_exactly_once(self) -> None:
        container = _FakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
            lease_factory=lambda _: _FakeLease(),
        )
        webhook = _batched_webhook()
        # Keep one seller message so the evidence isolates a single queue item.
        webhook.entry[0].changes[0].value.messages = [
            webhook.entry[0].changes[0].value.messages[0]
        ]
        await dispatcher.dispatch(b"signed", webhook, object())  # type: ignore[arg-type]
        pending_name = next(iter(container.documents))
        handler = _CrashAfterCommitHandler()
        dispatcher._handler = handler

        with self.assertRaises(SystemExit):
            await dispatcher._process(pending_name)

        # The lease expires/release occurs, while the pending Blob remains. A restarted
        # worker therefore cannot know that the external/domain side effect committed.
        handler.crash = False
        await dispatcher._process(pending_name)

        self.assertEqual(handler.committed_side_effects, 1)

    @unittest.expectedFailure
    async def test_partial_text_delivery_has_no_per_chunk_replay_ledger(self) -> None:
        client = _FailSecondChunkClient()
        service = object.__new__(WhatsAppWebhookService)
        service._whatsapp_client = client  # type: ignore[attr-defined]

        with self.assertRaises(WhatsAppAPIError):
            await service._send_text_chunks("seller", "first\n\nsecond", limit=9)

        # Retrying the full response delivers the first chunk twice because outbound
        # ordinals are not persisted independently.
        await service._send_text_chunks("seller", "first\n\nsecond", limit=9)
        self.assertEqual(client.delivered, ["first", "second"])

    async def test_meta_redelivery_after_dead_letter_is_terminally_deduplicated(
        self,
    ) -> None:
        container = _FakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
            lease_factory=lambda _: _FakeLease(),
            max_delivery_count=1,
        )
        webhook = _batched_webhook()
        webhook.entry[0].changes[0].value.messages = [
            webhook.entry[0].changes[0].value.messages[0]
        ]
        await dispatcher.dispatch(b"signed", webhook, object())  # type: ignore[arg-type]
        pending_name = next(iter(container.documents))
        dispatcher._handler = _AlwaysFailHandler()
        await dispatcher._process(pending_name)
        self.assertTrue(any("/dead-letter/" in name for name in container.documents))

        # A later Meta delivery has the same deterministic pending name. The durable
        # terminal receipt prevents that dead-lettered message from being re-enqueued.
        await dispatcher.dispatch(b"signed", webhook, object())  # type: ignore[arg-type]
        pending = [name for name in container.documents if "/pending/" in name]

        self.assertEqual(pending, [])

    async def test_meta_redelivery_after_success_is_terminally_deduplicated(self) -> None:
        container = _FakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
            lease_factory=lambda _: _FakeLease(),
        )
        webhook = _batched_webhook()
        webhook.entry[0].changes[0].value.messages = [
            webhook.entry[0].changes[0].value.messages[0]
        ]
        await dispatcher.dispatch(b"signed", webhook, object())  # type: ignore[arg-type]
        pending_name = next(iter(container.documents))
        handler = _CrashAfterCommitHandler()
        handler.crash = False
        dispatcher._handler = handler
        await dispatcher._process(pending_name)

        reordered = _batched_webhook()
        original = reordered.entry[0].changes[0].value.messages[0]
        preceding = reordered.entry[0].changes[0].value.messages[2]
        reordered.entry[0].changes[0].value.messages = [preceding, original]
        redelivered_unit = _dispatch_units(reordered)[1]
        await dispatcher.dispatch(b"signed", reordered, object())  # type: ignore[arg-type]

        self.assertEqual(handler.committed_side_effects, 1)
        self.assertFalse(
            any(
                "/pending/" in name and redelivered_unit.message_id in name
                for name in container.documents
            ),
            "A sequence-reordered redelivery must resolve to the existing terminal receipt.",
        )


class InflightReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryWorkflowStore()
        self.orchestrator = LicensingOrchestrator(
            analyzer=object(),  # type: ignore[arg-type]
            rate_cards=object(),  # type: ignore[arg-type]
            scenarios=object(),  # type: ignore[arg-type]
            store=self.store,
            default_term_duration="P1Y",
            default_billing_plan="Annual",
            default_segment="Commercial",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()

    async def test_claim_is_atomic_and_a_surviving_claim_blocks_reexecution(self) -> None:
        sender = "919111111111"
        message_id = "wamid.inflight"

        first = await self.orchestrator.claim_message_processing(sender, message_id)
        second = await self.orchestrator.claim_message_processing(sender, message_id)

        self.assertEqual(first, "claimed")
        self.assertEqual(second, "inflight")
        self.assertTrue(
            await self.orchestrator.has_inflight_message(sender, message_id)
        )
        self.assertFalse(await self.orchestrator.has_processed(sender, message_id))

    async def test_processed_receipt_atomically_closes_inflight_claim(self) -> None:
        sender = "919111111111"
        message_id = "wamid.completed"
        await self.orchestrator.claim_message_processing(sender, message_id)

        await self.orchestrator.mark_processed(sender, message_id)

        self.assertFalse(
            await self.orchestrator.has_inflight_message(sender, message_id)
        )
        self.assertTrue(await self.orchestrator.has_processed(sender, message_id))
        self.assertEqual(
            await self.orchestrator.claim_message_processing(sender, message_id),
            "processed",
        )

    async def test_concurrent_claims_have_one_owner(self) -> None:
        results = await asyncio.gather(
            self.orchestrator.claim_message_processing("seller", "wamid.concurrent"),
            self.orchestrator.claim_message_processing("seller", "wamid.concurrent"),
        )

        self.assertCountEqual(results, ["claimed", "inflight"])

    async def test_expiry_reset_preserves_inflight_replay_barrier(self) -> None:
        sender = "expiry-seller"
        message_id = "wamid.before-expiry"
        await self.orchestrator.claim_message_processing(sender, message_id)
        thread_id = self.orchestrator.thread_id(sender)
        session, version = await self.store.get_raw(thread_id)
        assert session is not None
        expired = session.model_copy(
            update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
        )
        await self.store.save(expired, version)

        reset = await self.orchestrator.reset_expired_session(sender)

        self.assertTrue(reset)
        self.assertTrue(
            await self.orchestrator.has_inflight_message(sender, message_id)
        )


if __name__ == "__main__":
    unittest.main()
