from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import ParsedLicenseRow, ScenarioType
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.renderer import format_money
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from tests.test_adversarial_conversation_regressions import _ScriptedInterpreter
from tests.test_generic_seller_language_boundary import _BoundaryService
from tests.test_simple_pricing_workflow import (
    WORKBOOK,
    FakeWhatsAppClient,
    _agent_intent,
)


class ProposalFactAnswerGroundingTests(unittest.IsolatedAsyncioTestCase):
    """Seller-visible proposal facts must come from saved state, never model prose."""

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

    def _service(self, mapping):
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = _BoundaryService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=_ScriptedInterpreter(mapping),
        )
        return client, service

    @staticmethod
    def _text(client: FakeWhatsAppClient, *, start: int = 0) -> str:
        return "\n".join(
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages[start:]
            if getattr(message, "text", None) is not None
        )

    async def _prepare_requirement(self, sender: str, *, quantity: int = 10):
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=quantity,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=quantity,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        if estate.pending_lines:
            line = estate.pending_lines[0]
            candidate = next(
                (
                    item
                    for item in line.candidates
                    if item.sku_title.casefold() == "power bi pro"
                ),
                line.candidates[0],
            )
            estate = await self.orchestrator.confirm_matches(
                sender,
                {
                    line.line_id: (
                        candidate.product_id,
                        candidate.sku_id,
                        candidate.sku_title,
                    )
                },
            )
        await self.orchestrator.request_requirement_validation(sender)
        return estate

    async def _prepare_confirmed_renewal(self, sender: str, *, quantity: int = 10):
        await self._prepare_requirement(sender, quantity=quantity)
        await self.orchestrator.confirm_requirement(sender)
        renewal = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renewal)
        return renewal

    async def test_hostile_model_cannot_invent_current_sku_quantity_or_total(
        self,
    ) -> None:
        sender = "facts-hostile-current-summary"
        scenario = await self._prepare_confirmed_renewal(sender, quantity=10)
        question = (
            "Which SKU is in my current proposal, what is its quantity, "
            "and what is the annual total?"
        )
        hostile = (
            "Your current proposal contains 999 Invented Quantum Licence seats "
            "for INR 9,999,999.99 annually."
        )
        client, service = self._service(
            {question: _agent_intent("answer_question", response_text=hostile)}
        )

        await service._handle_text(sender, question)

        response = self._text(client)
        expected_line = next(line for line in scenario.lines if line.proposed_quantity > 0)
        self.assertNotIn("Invented Quantum Licence", response)
        self.assertNotIn("9,999,999.99", response)
        self.assertNotRegex(response, r"\b999\b")
        self.assertIn(expected_line.sku_title, response)
        self.assertRegex(response, r"\b10\b")
        self.assertIn(format_money(scenario.total_value), response)

    async def test_preconfirmation_cost_question_stays_paused(self) -> None:
        sender = "facts-preconfirmation-cost"
        await self._prepare_requirement(sender, quantity=10)
        question = "What is the current annual cost of this requirement?"
        hostile = "The confirmed annual cost is INR 8,765,432.10."
        client, service = self._service(
            {question: _agent_intent("answer_question", response_text=hostile)}
        )

        await service._handle_text(sender, question)

        response = self._text(client)
        self.assertNotIn("8,765,432.10", response)
        self.assertRegex(
            response.casefold(),
            r"(?:not (?:yet )?(?:calculated|priced)|pricing (?:is |remains )?paused)",
        )
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertFalse(session.requirement_confirmed)
        self.assertIsNone(session.confirmed_as_is)

    async def test_explicit_baseline_and_active_questions_use_their_saved_totals(
        self,
    ) -> None:
        sender = "facts-baseline-versus-active"
        baseline = await self._prepare_confirmed_renewal(sender, quantity=10)
        revised = await self.orchestrator.edit_quantity(sender, "L1", 25)
        self.assertNotEqual(baseline.total_value, revised.total_value)
        baseline_question = "What is the confirmed Renew As-Is annual total?"
        active_question = "What is the current revised annual total?"
        client, service = self._service(
            {
                baseline_question: _agent_intent(
                    "answer_question",
                    response_text="Renew As-Is is INR 111.11.",
                ),
                active_question: _agent_intent(
                    "answer_question",
                    response_text="The revised total is INR 222.22.",
                ),
            }
        )

        await service._handle_text(sender, baseline_question)
        baseline_response = self._text(client)
        split_at = len(client.messages)
        await service._handle_text(sender, active_question)
        active_response = self._text(client, start=split_at)

        self.assertNotIn("111.11", baseline_response)
        self.assertIn(format_money(baseline.total_value), baseline_response)
        self.assertNotIn("222.22", active_response)
        self.assertIn(format_money(revised.total_value), active_response)

    async def test_missing_named_scenario_is_reported_instead_of_hallucinated(
        self,
    ) -> None:
        sender = "facts-missing-named-scenario"
        await self._prepare_confirmed_renewal(sender, quantity=10)
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertNotIn(ScenarioType.ME7, session.scenarios)
        question = "What is the annual total of my saved ME7 proposal?"
        hostile = "Your saved ME7 proposal totals INR 7,777,777.77."
        client, service = self._service(
            {question: _agent_intent("answer_question", response_text=hostile)}
        )

        await service._handle_text(sender, question)

        response = self._text(client)
        self.assertNotIn("7,777,777.77", response)
        self.assertIn("ME7", response)
        self.assertRegex(
            response.casefold(),
            r"(?:not (?:yet )?(?:been )?(?:prepared|built|saved|available)|no saved)",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
