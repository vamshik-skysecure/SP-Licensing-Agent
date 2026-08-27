from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import (
    ParsedLicenseRow,
    PendingDialogue,
    ScenarioType,
    WorkflowStage,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from tests.test_adversarial_conversation_regressions import (
    _QuietService,
    _ScriptedInterpreter,
    _text_bodies,
)
from tests.test_simple_pricing_workflow import (
    CUSTOMER,
    WORKBOOK,
    FakeWhatsAppClient,
    _agent_intent,
)


class NaturalPendingLanguageRegressionTests(unittest.IsolatedAsyncioTestCase):
    """Seller-language contracts for pending and suspended workflow state.

    The model is deliberately scripted with plausible but unsafe structured values in
    several tests.  Seller-authored wording must remain authoritative at the state-machine
    boundary, regardless of how a model classifies the same turn.
    """

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
            seller_allowlist=frozenset(),
            max_document_bytes=10 * 1024 * 1024,
            allow_all_sellers=True,
            workflow_mode="simple_pricing",
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    def _service(
        self,
        interpreter: _ScriptedInterpreter | None = None,
    ) -> tuple[FakeWhatsAppClient, _QuietService]:
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        return client, _QuietService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=interpreter,
        )

    @staticmethod
    def _commercial_snapshot(session: object) -> dict:
        return session.model_dump(  # type: ignore[union-attr]
            mode="json",
            exclude={
                "pending_dialogue",
                "pending_sku_change",
                "pending_match_prompt_suspended",
                "updated_at",
                "processed_message_ids",
                "inflight_message_ids",
                "failure_notified_message_ids",
            },
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

    async def _prepare_ambiguous_requirement(self, sender: str):
        return await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )

    async def test_natural_cancellation_closes_pending_change_without_mutation(self) -> None:
        for index, message in enumerate(
            (
                "I do not want to make this change",
                "Forget this replacement",
            ),
            start=1,
        ):
            with self.subTest(message=message):
                sender = f"natural-pending-cancel-{index}"
                await self._prepare_confirmed_renewal(sender)
                result = await self.orchestrator.add_sku(
                    sender,
                    product_query="Power BI",
                    quantity=10,
                )
                self.assertEqual(result.state, "confirmation_required")
                before = await self.orchestrator.get_session(sender)
                assert before is not None and before.pending_sku_change is not None
                snapshot = self._commercial_snapshot(before)
                _, service = self._service()

                await service._handle_text(sender, message)

                after = await self.orchestrator.get_session(sender)
                assert after is not None
                self.assertIsNone(after.pending_sku_change)
                self.assertEqual(self._commercial_snapshot(after), snapshot)

    async def test_natural_resume_and_fresh_start_resolve_saved_draft_gate(self) -> None:
        resume_sender = "natural-resume-saved-draft"
        await self._prepare_unconfirmed_requirement(resume_sender)
        before_resume = await self.orchestrator.get_session(resume_sender)
        assert before_resume is not None and before_resume.estate is not None
        capture_token = before_resume.estate.capture_token
        await self.orchestrator.set_pending_dialogue(
            resume_sender,
            PendingDialogue(
                kind="resume_session",
                question="Would you like to resume this saved draft, or start fresh?",
                awaiting_slot="resume_choice",
            ),
        )
        _, resume_service = self._service()

        await resume_service._handle_text(
            resume_sender,
            "Please continue with my saved requirement",
        )

        resumed = await self.orchestrator.get_session(resume_sender)
        assert resumed is not None and resumed.estate is not None
        self.assertIsNone(resumed.pending_dialogue)
        self.assertEqual(resumed.estate.capture_token, capture_token)

        fresh_sender = "natural-fresh-saved-draft"
        await self._prepare_unconfirmed_requirement(fresh_sender)
        await self.orchestrator.set_pending_dialogue(
            fresh_sender,
            PendingDialogue(
                kind="resume_session",
                question="Would you like to resume this saved draft, or start fresh?",
                awaiting_slot="resume_choice",
            ),
        )
        fresh_message = "I want to discard the old draft and begin a new requirement"
        _, fresh_service = self._service(
            _ScriptedInterpreter(
                {fresh_message: _agent_intent("reset_requirement")}
            )
        )

        await fresh_service._handle_text(fresh_sender, fresh_message)

        fresh = await self.orchestrator.get_session(fresh_sender)
        assert fresh is not None
        self.assertEqual(fresh.stage, WorkflowStage.AWAITING_UPLOAD)
        self.assertIsNone(fresh.estate)
        self.assertIsNone(fresh.pending_dialogue)

    async def test_natural_option_phrases_confirm_pending_sku_choice(self) -> None:
        for index, message in enumerate(
            (
                "I choose option 2",
                "I want option 2",
                "Go with option 2",
            ),
            start=1,
        ):
            with self.subTest(message=message):
                sender = f"natural-sku-option-{index}"
                await self._prepare_confirmed_renewal(sender)
                result = await self.orchestrator.add_sku(
                    sender,
                    product_query="Power BI",
                    quantity=10,
                )
                self.assertEqual(result.state, "confirmation_required")
                before = await self.orchestrator.get_session(sender)
                assert before is not None and before.pending_sku_change is not None
                expected_title = before.pending_sku_change.candidates[1].sku_title
                _, service = self._service(
                    _ScriptedInterpreter(
                        {message: _agent_intent("confirm_sku", candidate_number=2)}
                    )
                )

                await service._handle_text(sender, message)

                after = await self.orchestrator.get_session(sender)
                assert after is not None and after.active_scenario is not None
                self.assertIsNone(after.pending_sku_change)
                self.assertTrue(
                    any(
                        line.sku_title == expected_title
                        for line in after.scenarios[after.active_scenario].lines
                    )
                )

    async def test_natural_option_phrases_confirm_requirement_match(self) -> None:
        for index, message in enumerate(
            (
                "I choose option 2",
                "I want option 2",
                "Go with option 2",
            ),
            start=1,
        ):
            with self.subTest(message=message):
                sender = f"natural-requirement-option-{index}"
                estate = await self._prepare_ambiguous_requirement(sender)
                self.assertTrue(estate.pending_lines)
                expected_title = estate.pending_lines[0].candidates[1].sku_title
                _, service = self._service(
                    _ScriptedInterpreter(
                        {
                            message: _agent_intent(
                                "confirm_matches",
                                match_selections=[
                                    {"line_id": "L1", "candidate_number": 2}
                                ],
                            )
                        }
                    )
                )

                await service._handle_text(sender, message)

                after = await self.orchestrator.get_session(sender)
                assert after is not None and after.estate is not None
                self.assertFalse(after.estate.pending_lines)
                self.assertEqual(after.estate.lines[0].sku_title, expected_title)

    async def test_plan_number_is_not_consumed_as_a_quantity_answer(self) -> None:
        sender = "plan-number-is-not-quantity"
        await self._prepare_confirmed_renewal(sender)
        pending = PendingDialogue(
            kind="agent_clarification",
            question="How many Microsoft Defender for Endpoint licences should I add?",
            operation="add_sku",
            awaiting_slot="quantity",
            scope="scenario",
            product_query="Microsoft Defender for Endpoint",
            quantity=-1,
        )
        await self.orchestrator.set_pending_dialogue(sender, pending)
        before = await self.orchestrator.get_session(sender)
        assert before is not None
        snapshot = self._commercial_snapshot(before)
        message = "Plan 2"
        _, service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "add_sku",
                        product_query="Microsoft Defender for Endpoint Plan 2",
                        quantity=2,
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertEqual(self._commercial_snapshot(after), snapshot)
        self.assertIsNone(after.pending_sku_change)
        self.assertIsNotNone(after.pending_dialogue)
        assert after.pending_dialogue is not None
        self.assertEqual(after.pending_dialogue.awaiting_slot, "quantity")
        self.assertNotEqual(after.pending_dialogue.quantity, 2)

    async def test_source_line_answer_is_not_reused_as_replacement_product(self) -> None:
        sender = "source-line-not-replacement-product"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
        pending = PendingDialogue(
            kind="agent_clarification",
            question="Which licence would you like to replace?",
            operation="replace_sku",
            awaiting_slot="line",
            scope="scenario",
            scenario_type=ScenarioType.RENEW_AS_IS,
            product_query="",
            quantity=-1,
        )
        await self.orchestrator.set_pending_dialogue(sender, pending)
        before = await self.orchestrator.get_session(sender)
        assert before is not None
        snapshot = self._commercial_snapshot(before)
        message = source.line_id
        _, service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "replace_sku",
                        line_id=source.line_id,
                        product_query=source.sku_title,
                        quantity=source.proposed_quantity,
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.pending_dialogue is not None
        self.assertEqual(self._commercial_snapshot(after), snapshot)
        self.assertIsNone(after.pending_sku_change)
        self.assertEqual(after.pending_dialogue.operation, "replace_sku")
        self.assertEqual(after.pending_dialogue.awaiting_slot, "product")
        self.assertEqual(after.pending_dialogue.source_line_id, source.line_id)
        self.assertEqual(after.pending_dialogue.product_query, "")

    async def test_seller_can_reopen_a_suspended_requirement_sku_menu(self) -> None:
        sender = "reopen-suspended-requirement-menu"
        estate = await self._prepare_ambiguous_requirement(sender)
        self.assertTrue(estate.pending_lines)
        await self.orchestrator.set_pending_match_prompt_suspended(sender, True)
        message = "Please show the product options again"
        client, service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "answer_question",
                        response_text="I can show the product options again.",
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.estate is not None
        self.assertFalse(after.pending_match_prompt_suspended)
        self.assertTrue(after.estate.pending_lines)
        response = "\n".join(_text_bodies(client)).casefold()
        self.assertIn("1. power bi", response)
        self.assertIn("power bi", response)


if __name__ == "__main__":
    unittest.main()
