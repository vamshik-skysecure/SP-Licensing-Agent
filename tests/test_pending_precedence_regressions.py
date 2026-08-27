from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import LicenseAnalyzer, ParsedLicenseRow
from app.core.licensing.models import PendingDialogue, ScenarioType
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


def _text_bodies(client: FakeWhatsAppClient) -> list[str]:
    return [
        message.text.body
        for message in client.messages
        if getattr(message, "text", None) is not None
    ]


class _PrecedenceService(_QuietService):
    """Keep scenario rendering cheap while exercising the real SKU-choice presentation."""

    async def _send_sku_change_result(self, sender: str, result: object) -> None:
        await WhatsAppWebhookService._send_sku_change_result(
            self,
            sender,
            result,  # type: ignore[arg-type]
        )


class PendingPrecedenceRegressionTests(unittest.IsolatedAsyncioTestCase):
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
    ) -> tuple[FakeWhatsAppClient, _PrecedenceService]:
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        return client, _PrecedenceService(
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

    async def _prepare_confirmed_renewal(self, sender: str) -> None:
        await self._prepare_unconfirmed_requirement(sender)
        await self.orchestrator.confirm_requirement(sender)
        renewal = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renewal)

    async def _prepare_ambiguous_requirement(self, sender: str) -> None:
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E1",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        self.assertTrue(estate.pending_lines)

    async def _prepare_dual_pending_state(self, sender: str):
        await self._prepare_confirmed_renewal(sender)
        result = await self.orchestrator.add_sku(
            sender,
            product_query="Power BI",
            quantity=10,
        )
        self.assertEqual(result.state, "confirmation_required")
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.pending_sku_change is not None
        pending_sku = before.pending_sku_change
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which security capability must the plan include?",
                context_message="Does Microsoft 365 E5 include the capability I need?",
                detail_value="official_product_clarification",
            ),
        )
        return pending_sku, self._commercial_snapshot(before)

    async def test_cancel_question_does_not_cancel_pending_sku_or_match(self) -> None:
        sku_sender = "precedence-cancel-question-sku"
        await self._prepare_confirmed_renewal(sku_sender)
        result = await self.orchestrator.add_sku(
            sku_sender,
            product_query="Power BI",
            quantity=10,
        )
        self.assertEqual(result.state, "confirmation_required")
        before_sku = await self.orchestrator.get_session(sku_sender)
        assert before_sku is not None and before_sku.pending_sku_change is not None
        pending_id = before_sku.pending_sku_change.id
        before_commercial = self._commercial_snapshot(before_sku)

        _, service = self._service()
        await service._handle_text(sku_sender, "cancel?")

        after_sku = await self.orchestrator.get_session(sku_sender)
        assert after_sku is not None and after_sku.pending_sku_change is not None
        self.assertEqual(after_sku.pending_sku_change.id, pending_id)
        self.assertEqual(self._commercial_snapshot(after_sku), before_commercial)

        match_sender = "precedence-cancel-question-match"
        await self._prepare_ambiguous_requirement(match_sender)
        before_match = await self.orchestrator.get_session(match_sender)
        assert before_match is not None
        before_match_commercial = self._commercial_snapshot(before_match)

        _, service = self._service()
        await service._handle_text(match_sender, "cancel?")

        after_match = await self.orchestrator.get_session(match_sender)
        assert after_match is not None
        self.assertFalse(after_match.pending_match_prompt_suspended)
        self.assertEqual(self._commercial_snapshot(after_match), before_match_commercial)

    async def test_resume_question_and_bare_scenario_do_not_consume_resume_gate(self) -> None:
        resume_sender = "precedence-resume-question"
        await self._prepare_unconfirmed_requirement(resume_sender)
        await self.orchestrator.set_pending_dialogue(
            resume_sender,
            PendingDialogue(
                kind="resume_session",
                question="Would you like to resume this saved draft, or start fresh?",
                awaiting_slot="resume_choice",
            ),
        )
        before_resume = await self.orchestrator.get_session(resume_sender)
        assert before_resume is not None
        before_resume_commercial = self._commercial_snapshot(before_resume)

        _, service = self._service()
        await service._handle_text(resume_sender, "resume?")

        after_resume = await self.orchestrator.get_session(resume_sender)
        assert after_resume is not None and after_resume.pending_dialogue is not None
        self.assertEqual(after_resume.pending_dialogue.kind, "resume_session")
        self.assertEqual(self._commercial_snapshot(after_resume), before_resume_commercial)

        scenario_sender = "precedence-bare-scenario-resume"
        await self._prepare_unconfirmed_requirement(scenario_sender)
        await self.orchestrator.set_pending_dialogue(
            scenario_sender,
            PendingDialogue(
                kind="resume_session",
                question="Would you like to resume this saved draft, or start fresh?",
                awaiting_slot="resume_choice",
            ),
        )
        before_scenario = await self.orchestrator.get_session(scenario_sender)
        assert before_scenario is not None
        before_scenario_commercial = self._commercial_snapshot(before_scenario)
        _, service = self._service(
            _ScriptedInterpreter(
                {
                    "ME5": _agent_intent(
                        "build_scenario",
                        scenario=ScenarioType.ME5_COPILOT.value,
                    )
                }
            )
        )

        await service._handle_text(scenario_sender, "ME5")

        after_scenario = await self.orchestrator.get_session(scenario_sender)
        assert after_scenario is not None and after_scenario.pending_dialogue is not None
        self.assertEqual(after_scenario.pending_dialogue.kind, "resume_session")
        self.assertEqual(self._commercial_snapshot(after_scenario), before_scenario_commercial)

    async def test_complete_quantity_change_may_implicitly_resume(self) -> None:
        sender = "precedence-complete-edit-resume"
        await self._prepare_unconfirmed_requirement(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="resume_session",
                question="Would you like to resume this saved draft, or start fresh?",
                awaiting_slot="resume_choice",
            ),
        )
        message = "Change L1 to 25 licences"
        _, service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "set_quantity",
                        line_id="L1",
                        quantity=25,
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        changed = next(line for line in session.estate.lines if line.line_id == "L1")
        self.assertEqual(changed.renewal_quantity, 25)
        self.assertIsNone(session.pending_dialogue)

    async def test_newer_clarification_owns_ambiguous_candidate_reply(self) -> None:
        sender = "precedence-dual-option"
        pending_sku, before_commercial = await self._prepare_dual_pending_state(sender)
        message = "option 2"
        _, service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "confirm_sku",
                        candidate_number=2,
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_sku_change is not None
        self.assertEqual(session.pending_sku_change.id, pending_sku.id)
        self.assertEqual(self._commercial_snapshot(session), before_commercial)

    async def test_completed_or_cancelled_clarification_visibly_restates_sku_choice(self) -> None:
        cases = (
            ("cancel", "cancel the question", None),
            (
                "resolved",
                "Endpoint protection for the selected users",
                _agent_intent(
                    "answer_question",
                    response_text="I will check that capability.",
                ),
            ),
        )
        for label, message, intent in cases:
            with self.subTest(case=label):
                sender = f"precedence-dual-restatement-{label}"
                pending_sku, _ = await self._prepare_dual_pending_state(sender)
                interpreter = (
                    _ScriptedInterpreter({message: intent}) if intent is not None else None
                )
                client, service = self._service(interpreter)

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                assert session is not None and session.pending_sku_change is not None
                self.assertEqual(session.pending_sku_change.id, pending_sku.id)
                bodies = "\n".join(_text_bodies(client)).casefold()
                self.assertIn("power bi", bodies)
                self.assertIn("option", bodies)


if __name__ == "__main__":
    unittest.main()
