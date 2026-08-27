from __future__ import annotations

import unittest

from app.api.whatsapp.service import (
    RESET_REQUESTS,
    ServiceConfiguration,
)
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import PendingDialogue, ScenarioType, WorkflowStage
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from tests.test_adversarial_conversation_regressions import (
    _QuietService,
    _ScriptedInterpreter,
)
from tests.test_simple_pricing_workflow import (
    CUSTOMER,
    WORKBOOK,
    FakeWhatsAppClient,
    _agent_intent,
)


class PendingRouteAdversarialTests(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    def _service(
        self,
        *,
        workflow_mode: str = "simple_pricing",
        interpreter: _ScriptedInterpreter | None = None,
    ) -> _QuietService:
        configuration = ServiceConfiguration(
            seller_allowlist=frozenset(),
            max_document_bytes=10 * 1024 * 1024,
            allow_all_sellers=True,
            workflow_mode=workflow_mode,  # type: ignore[arg-type]
        )
        return _QuietService(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),  # type: ignore[arg-type]
            self.orchestrator,
            configuration,
            intent_interpreter=interpreter,
        )

    async def _prepare_unconfirmed_requirement(self, sender: str) -> None:
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)

    async def _prepare_confirmed_renewal(self, sender: str):
        await self._prepare_unconfirmed_requirement(sender)
        await self.orchestrator.confirm_requirement(sender)
        renewal = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renewal)
        return renewal

    @staticmethod
    def _commercial_snapshot(session: object) -> dict:
        return session.model_dump(  # type: ignore[union-attr]
            mode="json",
            exclude={
                "pending_dialogue",
                "pending_sku_change",
                "updated_at",
                "processed_message_ids",
                "inflight_message_ids",
                "failure_notified_message_ids",
            },
        )

    async def test_cancel_the_question_preserves_older_pending_sku_choice(self) -> None:
        sender = "pending-route-dual-state-cancel"
        await self._prepare_confirmed_renewal(sender)
        result = await self.orchestrator.add_sku(
            sender,
            product_query="Power BI",
            quantity=10,
        )
        self.assertEqual(result.state, "confirmation_required")
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.pending_sku_change is not None
        pending_sku_id = before.pending_sku_change.id
        before_commercial = self._commercial_snapshot(before)

        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which security capability must the plan include?",
                context_message="Does Microsoft 365 E5 include the capability I need?",
                detail_value="official_product_clarification",
            ),
        )

        await self._service()._handle_text(sender, "cancel the question")

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertIsNone(after.pending_dialogue)
        self.assertIsNotNone(after.pending_sku_change)
        self.assertEqual(after.pending_sku_change.id, pending_sku_id)  # type: ignore[union-attr]
        self.assertEqual(self._commercial_snapshot(after), before_commercial)

    async def test_short_refusals_never_become_comments_or_segments(self) -> None:
        replies = ("yes", "no", "I don't know", "cancel please")
        operations = (
            ("add_comment", "comment", "What seller comment should I add?"),
            ("set_segment", "segment", "Which customer segment applies?"),
        )

        for operation_index, (operation, slot, question) in enumerate(operations):
            for reply_index, reply in enumerate(replies):
                with self.subTest(operation=operation, reply=reply):
                    sender = f"pending-route-free-text-{operation_index}-{reply_index}"
                    await self._prepare_confirmed_renewal(sender)
                    before = await self.orchestrator.get_session(sender)
                    assert before is not None and before.active_scenario is not None
                    before_scenario = before.scenarios[before.active_scenario]

                    await self.orchestrator.set_pending_dialogue(
                        sender,
                        PendingDialogue(
                            kind="agent_clarification",
                            question=question,
                            operation=operation,  # type: ignore[arg-type]
                            awaiting_slot=slot,  # type: ignore[arg-type]
                            scope="scenario",
                        ),
                    )

                    # No interpreter deliberately exercises the deterministic fallback path.
                    await self._service()._handle_text(sender, reply)

                    after = await self.orchestrator.get_session(sender)
                    assert after is not None and after.active_scenario is not None
                    after_scenario = after.scenarios[after.active_scenario]
                    self.assertEqual(after_scenario.comments, before_scenario.comments)
                    self.assertEqual(after_scenario.segment, before_scenario.segment)

    async def test_cancel_closes_resume_gate_without_changing_saved_draft(self) -> None:
        sender = "pending-route-resume-cancel"
        await self._prepare_unconfirmed_requirement(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="resume_session",
                question="Would you like to resume this saved draft, or start fresh?",
                awaiting_slot="resume_choice",
            ),
        )
        before = await self.orchestrator.get_session(sender)
        assert before is not None
        before_commercial = self._commercial_snapshot(before)

        await self._service()._handle_text(sender, "cancel")

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertIsNone(after.pending_dialogue)
        self.assertEqual(self._commercial_snapshot(after), before_commercial)

    async def test_every_declared_reset_request_starts_a_fresh_requirement(self) -> None:
        for index, reply in enumerate(sorted(RESET_REQUESTS)):
            with self.subTest(reply=reply):
                sender = f"pending-route-reset-{index}"
                await self._prepare_unconfirmed_requirement(sender)
                await self.orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="resume_session",
                        question="Would you like to resume this saved draft, or start fresh?",
                        awaiting_slot="resume_choice",
                    ),
                )

                await self._service()._handle_text(sender, reply)

                after = await self.orchestrator.get_session(sender)
                assert after is not None
                self.assertEqual(after.stage, WorkflowStage.AWAITING_UPLOAD)
                self.assertIsNone(after.estate)
                self.assertIsNone(after.confirmed_as_is)
                self.assertIsNone(after.pending_dialogue)
                self.assertFalse(after.scenarios)

    async def test_pending_quantity_short_reply_works_in_every_workflow_mode(self) -> None:
        workflow_modes = (
            "simple_pricing",
            "renewal_only",
            "upgrade_comparison",
            "scenario_comparison",
        )
        for index, workflow_mode in enumerate(workflow_modes):
            with self.subTest(workflow_mode=workflow_mode):
                sender = f"pending-route-mode-{index}"
                renewal = await self._prepare_confirmed_renewal(sender)
                source = renewal.lines[0]
                target_quantity = source.proposed_quantity + 7
                reply = str(target_quantity)
                await self.orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="agent_clarification",
                        question=f"What quantity should I set for {source.sku_title}?",
                        operation="set_quantity",
                        awaiting_slot="quantity",
                        scope="scenario",
                        source_line_id=source.line_id,
                    ),
                )
                service = self._service(
                    workflow_mode=workflow_mode,
                    interpreter=_ScriptedInterpreter(
                        {
                            reply: _agent_intent(
                                "answer_question",
                                response_text="Quantity noted.",
                            )
                        }
                    ),
                )

                await service._handle_text(sender, reply)

                after = await self.orchestrator.get_session(sender)
                assert after is not None and after.active_scenario is not None
                changed = next(
                    line
                    for line in after.scenarios[after.active_scenario].lines
                    if line.line_id == source.line_id
                )
                self.assertEqual(changed.proposed_quantity, target_quantity)
                self.assertIsNone(after.pending_dialogue)


if __name__ == "__main__":
    unittest.main()
