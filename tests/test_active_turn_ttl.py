from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.core.licensing.models import WorkflowSession
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.store import InMemoryWorkflowStore


class ActiveTurnTtlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.store = InMemoryWorkflowStore(session_ttl_minutes=5)
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

    async def _persist_draft(self, sender: str) -> None:
        thread_id = self.orchestrator.thread_id(sender)
        await self.store.save(
            WorkflowSession(
                id=thread_id,
                thread_id=thread_id,
                sender=thread_id,
                capture_messages=["Microsoft 365 E3, 25 licences"],
            ),
            None,
        )

    async def _force_persisted_session_past_ttl(self, sender: str) -> None:
        thread_id = self.orchestrator.thread_id(sender)
        session, version = await self.store.get_raw(thread_id)
        assert session is not None
        await self.store.save(
            session.model_copy(
                update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
            ),
            version,
        )

    async def test_claimed_turn_reads_and_mutates_raw_state_after_ttl_boundary(self) -> None:
        sender = "919111111111"
        message_id = "wamid.long-model-turn"
        await self._persist_draft(sender)

        self.assertEqual(
            await self.orchestrator.claim_message_processing(sender, message_id),
            "claimed",
        )
        # Simulate a model/media/rendering turn whose wall-clock duration has crossed the
        # five-minute conversation TTL before its domain mutation is committed.
        await self._force_persisted_session_past_ttl(sender)

        active = await self.orchestrator.get_session(sender)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(
            active.capture_messages,
            ["Microsoft 365 E3, 25 licences"],
        )

        messages = await self.orchestrator.remember_capture_message(
            sender,
            "Power BI Pro, 10 licences",
        )
        self.assertEqual(
            messages,
            ["Microsoft 365 E3, 25 licences", "Power BI Pro, 10 licences"],
        )
        await self.orchestrator.mark_processed(sender, message_id)

        stored, _ = await self.store.get_raw(self.orchestrator.thread_id(sender))
        assert stored is not None
        self.assertEqual(stored.capture_messages, messages)
        self.assertTrue(await self.orchestrator.has_processed(sender, message_id))

    async def test_completion_restores_normal_ttl_filtering(self) -> None:
        sender = "919222222222"
        message_id = "wamid.completed-long-turn"
        await self._persist_draft(sender)
        await self.orchestrator.claim_message_processing(sender, message_id)
        await self._force_persisted_session_past_ttl(sender)

        self.assertIsNotNone(await self.orchestrator.get_session(sender))
        await self.orchestrator.mark_processed(sender, message_id)
        await self._force_persisted_session_past_ttl(sender)

        self.assertIsNone(await self.orchestrator.get_session(sender))

    async def test_finally_cleanup_drops_active_read_after_failed_turn(self) -> None:
        sender = "919333333333"
        message_id = "wamid.failed-long-turn"
        await self._persist_draft(sender)
        await self.orchestrator.claim_message_processing(sender, message_id)
        await self._force_persisted_session_past_ttl(sender)

        self.assertIsNotNone(await self.orchestrator.get_session(sender))
        # This is the non-persistent cleanup used by the webhook service's finally block;
        # the inflight replay barrier remains stored even when outbound delivery fails.
        self.orchestrator.end_message_processing_context(sender, message_id)

        self.assertIsNone(await self.orchestrator.get_session(sender))
        self.assertTrue(
            await self.orchestrator.has_inflight_message(sender, message_id)
        )


if __name__ == "__main__":
    unittest.main()
