from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.agent import (
    AgentIntent,
    IntentInterpretationError,
    OfficialProductAnswer,
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


class SemanticActionHelperTests(unittest.TestCase):
    def test_requirement_confirmation_requires_an_assertion(self) -> None:
        accepted = (
            "I confirm the requirement",
            "Yes, I approve the details",
            "Proceed with pricing",
            "The requirement is correct",
        )
        rejected = (
            "Can you confirm the requirement?",
            "What happens if I confirm?",
            "I do not confirm the requirement",
            "The requirement is not confirmed",
            "If I confirm, will you price it?",
        )
        for message in accepted:
            with self.subTest(message=message):
                self.assertTrue(
                    WhatsAppWebhookService._is_explicit_requirement_confirmation(
                        message
                    )
                )
        for message in rejected:
            with self.subTest(message=message):
                self.assertFalse(
                    WhatsAppWebhookService._is_requirement_confirmation_reply(message)
                )

    def test_keyword_shortcuts_respect_negation_and_informational_questions(self) -> None:
        self.assertFalse(
            WhatsAppWebhookService._requests_fresh_start("do not start fresh")
        )
        self.assertFalse(
            WhatsAppWebhookService._requests_fresh_start("what does start fresh mean")
        )
        self.assertTrue(
            WhatsAppWebhookService._requests_fresh_start("can you start fresh")
        )

        self.assertFalse(
            WhatsAppWebhookService._requests_enterprise_comparison(
                "do not compare all four"
            )
        )
        self.assertFalse(
            WhatsAppWebhookService._requests_enterprise_comparison(
                "why compare all four"
            )
        )
        self.assertTrue(
            WhatsAppWebhookService._requests_enterprise_comparison(
                "can you compare all four"
            )
        )

        self.assertFalse(
            WhatsAppWebhookService._requests_pending_change_cancel(
                "do not cancel that change"
            )
        )
        self.assertFalse(
            WhatsAppWebhookService._requests_pending_change_cancel(
                "what happens if I cancel the change"
            )
        )
        self.assertTrue(
            WhatsAppWebhookService._requests_pending_change_cancel(
                "do not replace it"
            )
        )

    def test_only_a_bare_scenario_reply_fills_a_pending_scenario_slot(self) -> None:
        self.assertEqual(
            WhatsAppWebhookService._scenario_from_bare_reply("ME5"),
            ScenarioType.ME5_COPILOT,
        )
        self.assertEqual(
            WhatsAppWebhookService._scenario_from_bare_reply("the ME7 option please"),
            ScenarioType.ME7,
        )
        self.assertIsNone(
            WhatsAppWebhookService._scenario_from_bare_reply(
                "Add 10 Power BI Pro licences to ME5"
            )
        )
        self.assertIsNone(
            WhatsAppWebhookService._scenario_from_bare_reply("Build ME5 instead")
        )

    def test_table_only_official_answer_still_requires_official_sources(self) -> None:
        answer = OfficialProductAnswer(
            answer="",
            clarification_question="",
            table_title="E3 and E5",
            table_headers=["Product", "Capability"],
            table_rows=[["Microsoft 365 E3", "Core productivity"]],
            source_urls=[],
        )
        with self.assertRaises(IntentInterpretationError):
            answer.validated()

    def test_finalization_and_rejection_require_assertive_seller_wording(self) -> None:
        for message in (
            "Finalize the proposal",
            "Please finalize it",
            "Can you finalize the proposal?",
            "Could you please finalize it?",
        ):
            with self.subTest(accepted_finalize=message):
                self.assertTrue(
                    WhatsAppWebhookService._is_assertive_finalization_request(message)
                )
        for message in (
            "Can I finalize the proposal?",
            "What happens if I finalize?",
            "Do not finalize this proposal",
            'The customer said "finalize the proposal"',
            "If the customer agrees, finalize it",
        ):
            with self.subTest(rejected_finalize=message):
                self.assertFalse(
                    WhatsAppWebhookService._is_assertive_finalization_request(message)
                )

        for message in (
            "No",
            "This proposal is not correct",
            "I do not approve this proposal",
            "Please reject the validation",
        ):
            with self.subTest(accepted_rejection=message):
                self.assertTrue(
                    WhatsAppWebhookService._is_explicit_validation_rejection(message)
                )
        for message in (
            "Can I reject the validation?",
            "What happens if I reject it?",
            "Do not reject the validation",
        ):
            with self.subTest(rejected_rejection=message):
                self.assertFalse(
                    WhatsAppWebhookService._is_explicit_validation_rejection(message)
                )

    def test_mutation_assertion_boundary_rejects_questions_hypotheticals_and_reports(self) -> None:
        blocked = (
            ("Is 50 enough for L1?", _agent_intent("set_quantity", line_id="L1", quantity=50)),
            (
                "What happens if I remove L2?",
                _agent_intent("set_disposition", line_id="L2", disposition="remove"),
            ),
            (
                "If the customer agrees, add 10 Power BI Pro licences",
                _agent_intent("add_sku", product_query="Power BI Pro", quantity=10),
            ),
            (
                'The customer said "replace L1 with Power BI Pro"',
                _agent_intent("replace_sku", line_id="L1", product_query="Power BI Pro"),
            ),
            ("What is ME5?", _agent_intent("build_scenario", scenario="me5_copilot")),
            (
                "What does customer name mean?",
                _agent_intent(
                    "set_requirement_detail",
                    detail_label="customer_name",
                    detail_value="customer name",
                ),
            ),
            (
                "I want to know what 10 Power BI Pro would cost",
                _agent_intent("add_sku", product_query="Power BI Pro", quantity=10),
            ),
        )
        for message, intent in blocked:
            with self.subTest(blocked=message):
                self.assertFalse(
                    WhatsAppWebhookService._is_assertive_commercial_instruction(
                        message,
                        intent,
                        None,
                        pending_slot_completion=False,
                    )
                )

        allowed = (
            (
                "Can you change L1 to 50?",
                _agent_intent("set_quantity", line_id="L1", quantity=50),
            ),
            (
                "Add 10 Power BI Pro licences",
                _agent_intent("add_sku", product_query="Power BI Pro", quantity=10),
            ),
            ("ME5", _agent_intent("build_scenario", scenario="me5_copilot")),
            ("What do you recommend?", _agent_intent("request_recommendation")),
            (
                "Customer name is Contoso",
                _agent_intent(
                    "set_requirement_detail",
                    detail_label="customer_name",
                    detail_value="Contoso",
                ),
            ),
        )
        for message, intent in allowed:
            with self.subTest(allowed=message):
                self.assertTrue(
                    WhatsAppWebhookService._is_assertive_commercial_instruction(
                        message,
                        intent,
                        None,
                        pending_slot_completion=False,
                    )
                )


class _ClarifyingOfficialAdvisor:
    async def answer_product_question(self, **_: object) -> OfficialProductAnswer:
        return OfficialProductAnswer(
            answer="",
            clarification_question="Which security capability must the plan include?",
            table_title="",
            table_headers=[],
            table_rows=[],
            source_urls=["https://learn.microsoft.com/en-us/microsoft-365/"],
        ).validated()


class _TwoTurnOfficialAdvisor:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def answer_product_question(self, **kwargs: object) -> OfficialProductAnswer:
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return OfficialProductAnswer(
                answer="",
                clarification_question="Which security capability must the plan include?",
                table_title="",
                table_headers=[],
                table_rows=[],
                source_urls=["https://learn.microsoft.com/en-us/microsoft-365/"],
            ).validated()
        return OfficialProductAnswer(
            answer="Microsoft 365 E5 includes the requested endpoint security capability.",
            clarification_question="",
            table_title="",
            table_headers=[],
            table_rows=[],
            source_urls=["https://learn.microsoft.com/en-us/microsoft-365/"],
        ).validated()


class SemanticActionIntegrationTests(unittest.IsolatedAsyncioTestCase):
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

    def _service(
        self,
        client: FakeWhatsAppClient,
        interpreter: _ScriptedInterpreter,
        *,
        advisor: object | None = None,
    ) -> _QuietService:
        return _QuietService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=interpreter,
            recommendation_advisor=advisor,  # type: ignore[arg-type]
        )

    async def test_model_misclassification_cannot_price_a_confirmation_question(self) -> None:
        sender = "semantic-confirm-question"
        await self._prepare_unconfirmed_requirement(sender)
        message = "Can you confirm the complete requirement?"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter({message: _agent_intent("confirm_validation")}),
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertIsNone(session.confirmed_as_is)
        self.assertFalse(session.scenarios)

    async def test_complete_me5_action_supersedes_stale_scenario_slot(self) -> None:
        sender = "semantic-stale-scenario"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which proposal should I update?",
                context_message="Add 5 Visio Plan 2 licences",
                operation="add_sku",
                awaiting_slot="scenario",
                scope="scenario",
                product_query="Visio Plan 2",
                quantity=5,
            ),
        )
        message = "Add 10 Power BI Pro licences to ME5"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "add_sku",
                        scenario="me5_copilot",
                        product_query="Power BI Pro",
                        quantity=10,
                    )
                }
            ),
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        self.assertEqual(session.active_scenario, ScenarioType.ME5_COPILOT)
        active = session.scenarios[session.active_scenario]
        self.assertTrue(
            any(
                line.sku_title == "Power BI Pro" and line.proposed_quantity == 10
                for line in active.lines
            )
        )
        self.assertFalse(any("Visio Plan 2" in line.sku_title for line in active.lines))
        self.assertIsNone(session.pending_dialogue)

    async def test_new_official_clarification_replaces_older_pending_question(self) -> None:
        sender = "semantic-new-official-question"
        await self._prepare_confirmed_renewal(sender)
        old = PendingDialogue(
            kind="agent_clarification",
            question="How many Visio Plan 2 licences should I add?",
            context_message="Add Visio Plan 2",
            operation="add_sku",
            awaiting_slot="quantity",
            scope="scenario",
            product_query="Visio Plan 2",
        )
        await self.orchestrator.set_pending_dialogue(sender, old)
        message = "What security capability does Microsoft 365 E5 include?"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "answer_question",
                        detail_label="official_product_question",
                        detail_value=message,
                        product_query="Microsoft 365 E5",
                    )
                }
            ),
            advisor=_ClarifyingOfficialAdvisor(),
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(
            session.pending_dialogue.question,
            "Which security capability must the plan include?",
        )
        self.assertNotEqual(session.pending_dialogue, old)

    async def test_official_clarification_preserves_pending_sku_and_receives_next_reply(
        self,
    ) -> None:
        sender = "semantic-official-question-with-pending-sku"
        await self._prepare_confirmed_renewal(sender)
        sku_result = await self.orchestrator.add_sku(
            sender,
            product_query="Power BI",
            quantity=10,
        )
        self.assertEqual(sku_result.state, "confirmation_required")
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.pending_sku_change is not None
        pending_id = before.pending_sku_change.id
        question = "Does Microsoft 365 E5 include the security capability I need?"
        clarification = "Endpoint protection"
        advisor = _TwoTurnOfficialAdvisor()
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    question: _agent_intent(
                        "answer_question",
                        detail_label="official_product_question",
                        detail_value=question,
                        product_query="Microsoft 365 E5",
                    ),
                    clarification: _agent_intent(
                        "answer_question",
                        detail_label="official_product_question",
                        detail_value=clarification,
                        product_query="Microsoft 365 E5",
                    ),
                }
            ),
            advisor=advisor,
        )

        await service._handle_text(sender, question)

        awaiting_clarification = await self.orchestrator.get_session(sender)
        assert awaiting_clarification is not None
        assert awaiting_clarification.pending_sku_change is not None
        assert awaiting_clarification.pending_dialogue is not None
        self.assertEqual(awaiting_clarification.pending_sku_change.id, pending_id)
        self.assertEqual(
            awaiting_clarification.pending_dialogue.question,
            "Which security capability must the plan include?",
        )

        await service._handle_text(sender, clarification)

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.pending_sku_change is not None
        self.assertEqual(after.pending_sku_change.id, pending_id)
        self.assertIsNone(after.pending_dialogue)
        self.assertEqual(len(advisor.calls), 2)
        self.assertIn(
            "Seller clarification: Endpoint protection",
            str(advisor.calls[1]["seller_question"]),
        )
        response = "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        )
        self.assertIn("includes the requested endpoint security capability", response)


if __name__ == "__main__":
    unittest.main()
