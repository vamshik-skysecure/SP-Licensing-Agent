from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import (
    MigrationDisposition,
    ParsedLicenseRow,
    PendingDialogue,
    ScenarioType,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from tests.test_adversarial_conversation_regressions import (
    _ScriptedInterpreter,
)
from tests.test_generic_seller_language_boundary import _BoundaryService
from tests.test_simple_pricing_workflow import (
    CUSTOMER,
    WORKBOOK,
    FakeWhatsAppClient,
    SingleTurnRequirementExtractor,
    _agent_intent,
)


class PendingSlotSellerGroundingTests(unittest.IsolatedAsyncioTestCase):
    """The seller's words, not a model field, own every commercial slot."""

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
        *,
        extractor: object | None = None,
    ) -> _BoundaryService:
        return _BoundaryService(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            self.orchestrator,
            self.configuration,
            intent_interpreter=interpreter,
            requirement_extractor=extractor,  # type: ignore[arg-type]
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

    async def _analyze_title(self, sender: str, title: str, quantity: int = 10):
        return await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title=title,
                    total_licenses=quantity,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=quantity,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )

    @staticmethod
    def _scenario_line(session, line_id: str):
        assert session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        return next(line for line in scenario.lines if line.line_id == line_id)

    async def test_pending_product_slot_rejects_model_product_mismatch(self) -> None:
        sender = "slot-ground-product"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which exact Microsoft product should I add?",
                operation="add_sku",
                awaiting_slot="product",
                scope="scenario",
                quantity=5,
            ),
        )
        message = "Power BI Pro"
        service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "add_sku",
                        product_query="Microsoft 365 E5 without Audio Conferencing",
                        quantity=5,
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        titles = {
            line.sku_title.casefold()
            for line in session.scenarios[session.active_scenario].lines
        }
        self.assertNotIn(
            "microsoft 365 e5 without audio conferencing",
            titles,
        )
        if session.pending_sku_change is not None:
            self.assertNotEqual(
                session.pending_sku_change.product_query.casefold(),
                "microsoft 365 e5 without audio conferencing",
            )

    async def test_pending_line_slot_prefers_explicit_seller_line(self) -> None:
        sender = "slot-ground-line"
        renewal = await self._prepare_confirmed_renewal(sender)
        original_l1 = next(line for line in renewal.lines if line.line_id == "L1")
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which licence should I replace?",
                operation="replace_sku",
                awaiting_slot="line",
                scope="scenario",
                product_query="Microsoft 365 E5 without Audio Conferencing",
            ),
        )
        message = "L2"
        service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "replace_sku",
                        line_id="L1",
                        product_query="",
                        quantity=-1,
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(
            self._scenario_line(session, "L1").sku_title,
            original_l1.sku_title,
        )
        if session.pending_sku_change is not None:
            self.assertEqual(session.pending_sku_change.source_line_id, "L2")

    async def test_pending_disposition_slot_uses_seller_disposition(self) -> None:
        sender = "slot-ground-disposition"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Should I retain or remove L1?",
                operation="set_disposition",
                awaiting_slot="disposition",
                scope="scenario",
                source_line_id="L1",
            ),
        )
        message = "Retain"
        service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "set_disposition",
                        line_id="L1",
                        disposition="remove",
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(
            self._scenario_line(session, "L1").disposition,
            MigrationDisposition.RETAIN,
        )

    async def test_pending_free_text_slot_uses_seller_value(self) -> None:
        sender = "slot-ground-segment"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which customer segment applies?",
                operation="set_segment",
                awaiting_slot="segment",
                scope="scenario",
            ),
        )
        message = "Commercial"
        service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "set_segment",
                        segment="Enterprise",
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        self.assertEqual(
            session.scenarios[session.active_scenario].segment,
            "Commercial",
        )

    async def test_superseding_requirement_keeps_office_365_identity(self) -> None:
        sender = "slot-ground-supersede-suite"
        estate = await self._analyze_title(sender, "Power BI")
        self.assertTrue(estate.pending_lines)
        message = "Add 10 Office 365 E5 licences"
        service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "capture_requirement",
                        product_query="Microsoft 365 E5 without Audio Conferencing",
                        quantity=10,
                    )
                }
            ),
            extractor=SingleTurnRequirementExtractor(
                "Office 365 E5 without Audio Conferencing",
                10,
            ),
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        estate_titles = {
            (line.sku_title or line.source_product_title).casefold()
            for line in session.estate.lines
        }
        self.assertNotIn(
            "microsoft 365 e5 without audio conferencing",
            estate_titles,
        )
        if session.pending_sku_change is not None:
            self.assertNotEqual(
                session.pending_sku_change.product_query.casefold(),
                "microsoft 365 e5 without audio conferencing",
            )

    async def test_line_and_plan_digits_cannot_ground_model_quantity(self) -> None:
        cases = (
            (
                "line-id",
                "Replace L2 with Microsoft 365 E5 without Audio Conferencing",
                _agent_intent(
                    "replace_sku",
                    line_id="L2",
                    product_query="Microsoft 365 E5 without Audio Conferencing",
                    quantity=2,
                ),
                "L2",
                2,
            ),
            (
                "plan-code",
                "Change L1 to E5",
                _agent_intent("set_quantity", line_id="L1", quantity=5),
                "L1",
                5,
            ),
            (
                "zero",
                "Change L1",
                _agent_intent("set_quantity", line_id="L1", quantity=0),
                "L1",
                0,
            ),
        )
        for suffix, message, intent, line_id, unsafe_quantity in cases:
            with self.subTest(case=suffix):
                sender = f"slot-ground-number-{suffix}"
                renewal = await self._prepare_confirmed_renewal(sender)
                original_line_ids = {line.line_id for line in renewal.lines}
                original = next(
                    line for line in renewal.lines if line.line_id == line_id
                )
                service = self._service(
                    _ScriptedInterpreter({message: intent})
                )

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                assert session is not None
                if session.pending_sku_change is not None:
                    self.assertNotEqual(
                        session.pending_sku_change.quantity,
                        unsafe_quantity,
                    )
                    if suffix == "line-id":
                        self.assertEqual(
                            session.pending_sku_change.quantity,
                            original.proposed_quantity,
                        )
                else:
                    scenario = session.scenarios[session.active_scenario]
                    if suffix == "line-id":
                        replacement_lines = [
                            line
                            for line in scenario.lines
                            if line.line_id not in original_line_ids
                        ]
                        self.assertEqual(len(replacement_lines), 1)
                        self.assertEqual(
                            replacement_lines[0].proposed_quantity,
                            original.proposed_quantity,
                        )
                    else:
                        self.assertEqual(
                            self._scenario_line(session, line_id).proposed_quantity,
                            original.proposed_quantity,
                        )

    async def test_negated_and_reported_titles_do_not_confirm_requirement(self) -> None:
        for suffix, prefix in (
            ("negated", "Do not use"),
            ("reported", "The customer selected"),
        ):
            with self.subTest(case=suffix):
                sender = f"slot-ground-requirement-title-{suffix}"
                estate = await self._analyze_title(sender, "Power BI")
                self.assertTrue(estate.pending_lines)
                candidate = estate.pending_lines[0].candidates[0]
                message = f"{prefix} {candidate.sku_title}"
                service = self._service()

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                assert session is not None and session.estate is not None
                self.assertTrue(session.estate.pending_lines)

    async def test_negated_and_reported_titles_do_not_confirm_sku_change(self) -> None:
        for suffix, prefix in (
            ("negated", "Do not use"),
            ("reported", "The customer selected"),
        ):
            with self.subTest(case=suffix):
                sender = f"slot-ground-change-title-{suffix}"
                await self._prepare_confirmed_renewal(sender)
                result = await self.orchestrator.add_sku(sender, "Power BI", 10)
                self.assertEqual(result.state, "confirmation_required")
                session = await self.orchestrator.get_session(sender)
                assert session is not None and session.pending_sku_change is not None
                candidate = session.pending_sku_change.candidates[0]
                message = f"{prefix} {candidate.sku_title}"
                service = self._service()

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                assert session is not None
                self.assertIsNotNone(session.pending_sku_change)

    async def test_requirement_detail_label_must_be_seller_grounded(self) -> None:
        sender = "slot-ground-detail-label"
        await self._prepare_unconfirmed_requirement(sender)
        message = "Customer name is Contoso"
        service = self._service(
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "set_requirement_detail",
                        detail_label="tenant_id",
                        detail_value="Contoso",
                    )
                }
            )
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        labels = {
            detail.label.casefold().replace("_", " ")
            for detail in session.estate.seller_details
        }
        self.assertNotIn("tenant id", labels)

    async def test_repeated_informational_interruptions_release_pending_prompt(self) -> None:
        sender = "slot-ground-bounded-interruptions"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
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
        first = "What does this product include?"
        second = "Can you explain the product again?"
        service = self._service(
            _ScriptedInterpreter(
                {
                    first: _agent_intent(
                        "answer_question",
                        response_text="It is a Microsoft licensing product.",
                    ),
                    second: _agent_intent(
                        "answer_question",
                        response_text="The proposal remains unchanged.",
                    ),
                }
            )
        )

        await service._handle_text(sender, first)
        after_first = await self.orchestrator.get_session(sender)
        assert after_first is not None
        self.assertIsNotNone(after_first.pending_dialogue)

        await service._handle_text(sender, second)
        after_second = await self.orchestrator.get_session(sender)
        assert after_second is not None
        self.assertIsNone(after_second.pending_dialogue)
        self.assertEqual(
            self._scenario_line(after_second, source.line_id).proposed_quantity,
            source.proposed_quantity,
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
