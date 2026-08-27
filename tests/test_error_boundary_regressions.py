from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.agent import AgentIntent
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import ScenarioType
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore, WorkflowConflictError
from app.core.whatsapp import WhatsAppAPIError, WhatsAppMedia
from tests.test_simple_pricing_workflow import (
    CUSTOMER,
    WORKBOOK,
    FakeWhatsAppClient,
    _agent_intent,
    _webhook,
)


class _AddDefenderInterpreter:
    async def interpret(self, *_: object) -> AgentIntent:
        return _agent_intent(
            "add_sku",
            product_query="Microsoft Defender for Endpoint P2",
            quantity=10,
        )


class _AcknowledgementInterpreter:
    async def interpret(self, *_: object) -> AgentIntent:
        return _agent_intent("acknowledge", response_text="Thank you.")


class _ConversationalQuantityInterpreter:
    async def interpret(self, *_: object) -> AgentIntent:
        # Reproduce a plausible model classification error for a short slot answer.
        return _agent_intent("answer_question", response_text="I noted 51.")


class _CountingAcknowledgementInterpreter:
    def __init__(self) -> None:
        self.calls = 0

    async def interpret(self, *_: object) -> AgentIntent:
        self.calls += 1
        return _agent_intent("acknowledge", response_text="Thank you.")


class _AddCommentInterpreter:
    async def interpret(self, *_: object) -> AgentIntent:
        return _agent_intent(
            "add_comment",
            comment="Customer approval pending.",
        )


class _PostCommitDeliveryFailureService(WhatsAppWebhookService):
    async def _send_scenario(self, *_: object) -> None:
        # The orchestrator has already committed the edit when this delivery starts.
        raise RuntimeError("synthetic outbound delivery failure")


class _HardCrashAfterCommitService(WhatsAppWebhookService):
    async def _send_scenario(self, *_: object) -> None:
        # SystemExit models process termination and deliberately bypasses
        # `except Exception` after the orchestrator has committed the edit.
        raise SystemExit("synthetic process termination after commit")


class _FailSecondImageClient:
    def __init__(self) -> None:
        self.images: list[bytes] = []
        self.messages: list[object] = []

    async def send_image(self, *, content: bytes, **_: object) -> dict[str, object]:
        if len(self.images) == 1:
            raise WhatsAppAPIError("synthetic image delivery failure", network_error=True)
        self.images.append(content)
        return {}

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}


class ErrorBoundaryRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.provider = RateCardProvider(
            LocalRateCardSource(WORKBOOK),
            sheet_name="Final Output Sheet",
            refresh_seconds=3600,
        )
        self.store = InMemoryWorkflowStore()
        self.orchestrator = LicensingOrchestrator(
            analyzer=LicenseAnalyzer(self.provider),
            rate_cards=self.provider,
            scenarios=ScenarioEngine(
                apply_bundle_rules=False,
                price_basis="marketplace",
            ),
            store=self.store,
            default_term_duration="P1Y",
            default_billing_plan="Annual",
            default_segment="Commercial",
        )
        self.configuration = ServiceConfiguration(
            frozenset(),
            10 * 1024 * 1024,
            allow_all_sellers=True,
            workflow_mode="simple_pricing",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    async def _prepare_confirmed_renewal(self, sender: str) -> None:
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renewal = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renewal)

    async def test_retry_after_post_commit_delivery_failure_does_not_duplicate_edit(
        self,
    ) -> None:
        sender = "post-commit-delivery-retry"
        await self._prepare_confirmed_renewal(sender)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = _PostCommitDeliveryFailureService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=_AddDefenderInterpreter(),
        )
        webhook = _webhook(
            {
                "id": "wamid.post-commit-retry",
                "from": sender,
                "type": "text",
                "text": {"body": "Add 10 Microsoft Defender for Endpoint P2 licences"},
            }
        )

        # The edit is committed before rendering fails.  The service must persist a
        # replay barrier, send a safe recovery notice, and consume a delivery retry
        # without executing the same commercial mutation again.
        await service.handle(webhook)
        self.assertTrue(
            await self.orchestrator.has_processed(sender, "wamid.post-commit-retry")
        )
        self.assertTrue(
            any(
                "stopped automatic replay" in message.text.body.lower()
                or "prevent a duplicate proposal change" in message.text.body.lower()
                for message in client.messages
                if hasattr(message, "text")
            ),
            "The seller must receive a safe recovery/stop-replay notice.",
        )

        await service.handle(webhook)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        matching = [
            line
            for line in scenario.lines
            if line.sku_title == "Microsoft Defender for Endpoint P2"
            and line.proposed_quantity == 10
        ]
        self.assertEqual(
            len(matching),
            1,
            "Retrying one WhatsApp message must not commit the same edit twice.",
        )

    async def test_process_restart_after_commit_uses_inflight_replay_barrier(self) -> None:
        sender = "hard-crash-post-commit"
        await self._prepare_confirmed_renewal(sender)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = _HardCrashAfterCommitService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=_AddDefenderInterpreter(),
        )
        webhook = _webhook(
            {
                "id": "wamid.hard-crash-post-commit",
                "from": sender,
                "type": "text",
                "text": {"body": "Add 10 Microsoft Defender for Endpoint P2 licences"},
            }
        )

        with self.assertRaises(SystemExit):
            await service.handle(webhook)
        self.assertTrue(
            await self.orchestrator.has_inflight_message(
                sender,
                "wamid.hard-crash-post-commit",
            )
        )

        # A restarted delivery of the same Meta message receives a safe uncertainty
        # response and never executes the committed commercial instruction again.
        await service.handle(webhook)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        matching = [
            line
            for line in scenario.lines
            if line.sku_title == "Microsoft Defender for Endpoint P2"
            and line.proposed_quantity == 10
        ]
        self.assertEqual(len(matching), 1)
        self.assertTrue(
            await self.orchestrator.has_processed(
                sender,
                "wamid.hard-crash-post-commit",
            )
        )
        self.assertTrue(
            any(
                "will not repeat it automatically" in message.text.body.lower()
                for message in client.messages
                if hasattr(message, "text")
            )
        )

    async def test_receipt_conflict_after_committed_mutation_never_replays_business_action(
        self,
    ) -> None:
        sender = "receipt-conflict-post-commit"
        message_id = "wamid.receipt-conflict-post-commit"
        await self._prepare_confirmed_renewal(sender)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=_AddCommentInterpreter(),
        )
        webhook = _webhook(
            {
                "id": message_id,
                "from": sender,
                "type": "text",
                "text": {"body": "Add customer approval pending as a comment"},
            }
        )

        # The business mutation commits successfully; only the completion receipt
        # conflicts.  The handler must retain a replay barrier rather than execute the
        # seller instruction a second time on Meta redelivery.
        with patch.object(
            self.orchestrator,
            "mark_processed",
            AsyncMock(side_effect=WorkflowConflictError("synthetic receipt conflict")),
        ):
            await service.handle(webhook)

        after_first = await self.orchestrator.get_session(sender)
        assert after_first is not None and after_first.active_scenario is not None
        first_scenario = after_first.scenarios[after_first.active_scenario]
        self.assertEqual(first_scenario.comments, ["Customer approval pending."])
        self.assertTrue(
            await self.orchestrator.has_failure_notification(sender, message_id)
        )

        await service.handle(webhook)

        after_retry = await self.orchestrator.get_session(sender)
        assert after_retry is not None and after_retry.active_scenario is not None
        retry_scenario = after_retry.scenarios[after_retry.active_scenario]
        self.assertEqual(retry_scenario.comments, ["Customer approval pending."])
        self.assertTrue(await self.orchestrator.has_processed(sender, message_id))

    async def test_standalone_acknowledgement_closes_pending_sku_choice(self) -> None:
        sender = "stale-choice-acknowledgement"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        result = await self.orchestrator.replace_requirement_sku(
            sender,
            "L1",
            "Microsoft 365 E7",
            25,
        )
        self.assertEqual(result.state, "confirmation_required")
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=_AcknowledgementInterpreter(),
        )

        await service._handle_text(sender, "thanks")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(
            session.pending_sku_change,
            "A courtesy reply must not leave an old product choice armed.",
        )

    async def test_numeric_pending_slot_survives_conversational_misclassification(
        self,
    ) -> None:
        sender = "numeric-slot-conversational-misclassification"
        await self._prepare_confirmed_renewal(sender)
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=_ConversationalQuantityInterpreter(),
        )
        await service._execute_agent_intent(
            sender,
            _agent_intent(
                "add_sku",
                product_query="Microsoft Defender for Endpoint P2",
            ),
            original_message="Add Microsoft Defender for Endpoint P2",
        )

        await service._handle_text(sender, "51")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        self.assertTrue(
            any(
                line.sku_title == "Microsoft Defender for Endpoint P2"
                and line.proposed_quantity == 51
                for line in scenario.lines
            ),
            "A numeric answer must complete the saved add operation even if the model "
            "misclassifies the turn as conversational.",
        )
        self.assertIsNone(session.pending_dialogue)

    async def test_expired_session_duplicate_message_id_is_not_reprocessed(self) -> None:
        sender = "expired-session-duplicate"
        message_id = "wamid.expired-duplicate"
        await self.orchestrator.mark_processed(sender, message_id)
        thread_id = self.orchestrator.thread_id(sender)
        session, version = await self.store.get(thread_id)
        assert session is not None
        await self.store.save(
            session.model_copy(
                update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
            ),
            version,
        )
        interpreter = _CountingAcknowledgementInterpreter()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=interpreter,
        )

        await service.handle(
            _webhook(
                {
                    "id": message_id,
                    "from": sender,
                    "type": "text",
                    "text": {"body": "Thank you"},
                }
            )
        )

        self.assertEqual(
            interpreter.calls,
            0,
            "A delayed duplicate must remain deduplicated after workflow expiry.",
        )

    async def test_expired_failure_replay_preserves_every_delivery_ledger_entry(self) -> None:
        sender = "expired-failure-ledgers"
        current_failure = "wamid.failure-current"
        old_processed = "wamid.processed-other"
        old_inflight = "wamid.inflight-other"
        old_failure = "wamid.failure-other"
        await self.orchestrator.mark_processed(sender, old_processed)
        self.assertEqual(
            await self.orchestrator.claim_message_processing(sender, old_inflight),
            "claimed",
        )
        await self.orchestrator.mark_failure_notified(sender, old_failure)
        await self.orchestrator.mark_failure_notified(sender, current_failure)
        thread_id = self.orchestrator.thread_id(sender)
        session, version = await self.store.get(thread_id)
        assert session is not None
        await self.store.save(
            session.model_copy(
                update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
            ),
            version,
        )
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=_CountingAcknowledgementInterpreter(),
        )

        await service.handle(
            _webhook(
                {
                    "id": current_failure,
                    "from": sender,
                    "type": "text",
                    "text": {"body": "Apply the earlier change"},
                }
            )
        )

        self.assertTrue(await self.orchestrator.has_processed(sender, current_failure))
        self.assertTrue(await self.orchestrator.has_processed(sender, old_processed))
        self.assertTrue(
            await self.orchestrator.has_inflight_message(sender, old_inflight)
        )
        self.assertTrue(
            await self.orchestrator.has_failure_notification(sender, old_failure)
        )

    @patch(
        "app.api.whatsapp.service.render_information_table_images",
        return_value=[b"page-one", b"page-two"],
    )
    async def test_partial_image_delivery_does_not_resend_full_text_fallback(
        self,
        _render: object,
    ) -> None:
        client = _FailSecondImageClient()
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
        )

        with self.assertRaises(WhatsAppAPIError):
            await service._send_information_table(
                "partial-image-delivery",
                title="Guidance",
                headers=["Product", "Purpose"],
                rows=[["Microsoft 365 E3", "Core productivity"]],
            )

        self.assertEqual(client.images, [b"page-one"])
        self.assertEqual(
            client.messages,
            [],
            "A partial image delivery must not be followed by a duplicate full-text table.",
        )


if __name__ == "__main__":
    unittest.main()
