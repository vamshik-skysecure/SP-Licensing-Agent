from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
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
    FakeRequirementExtractor,
    FakeWhatsAppClient,
    SingleTurnRequirementExtractor,
    _agent_intent,
    _webhook,
)


class _BoundaryService(_QuietService):
    """Keep semantic-boundary tests independent of rendering and PDF latency."""

    async def _send_simple_revised(self, sender: str, scenario: object) -> None:
        await self._send_text(sender, "revised-proposal")

    async def _send_simple_commercial_pdf(self, sender: str) -> None:
        await self._send_text(sender, "proposal-pdf")

    async def _send_active_proposal_pdf(self, sender: str) -> None:
        await self._send_text(sender, "proposal-pdf")


class GenericSellerLanguageBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Regression contract for seller-authored intent at every state boundary.

    The language model is intentionally scripted with unsafe classifications. These tests
    therefore prove that deterministic application policy, rather than a cooperative model,
    controls every commercial mutation.
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
        client: FakeWhatsAppClient,
        interpreter: _ScriptedInterpreter | None = None,
        *,
        extractor: object | None = None,
    ) -> _BoundaryService:
        return _BoundaryService(
            client,  # type: ignore[arg-type]
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

    async def test_questions_hypotheticals_negation_and_reports_cannot_capture(self) -> None:
        blocked = {
            "What would adding 10 Power BI Pro licences cost?": _agent_intent(
                "capture_requirement",
                product_query="Power BI Pro",
                quantity=10,
            ),
            "If the customer agrees, add 10 Power BI Pro licences": _agent_intent(
                "capture_requirement",
                product_query="Power BI Pro",
                quantity=10,
            ),
            "No need for 10 Power BI Pro licences": _agent_intent(
                "capture_requirement",
                product_query="Power BI Pro",
                quantity=10,
            ),
            "Power BI Pro is not needed": _agent_intent(
                "capture_requirement",
                product_query="Power BI Pro",
                quantity=10,
            ),
            'The customer said "add 10 Power BI Pro licences"': _agent_intent(
                "capture_requirement",
                product_query="Power BI Pro",
                quantity=10,
            ),
            "I want to know what 10 Power BI Pro licences would cost": _agent_intent(
                "capture_requirement",
                product_query="Power BI Pro",
                quantity=10,
            ),
        }
        for index, (message, intent) in enumerate(blocked.items(), start=1):
            with self.subTest(message=message):
                sender = f"language-capture-block-{index}"
                client = FakeWhatsAppClient(
                    WhatsAppMedia(b"", "unused", "text/plain")
                )
                service = self._service(
                    client,
                    _ScriptedInterpreter({message: intent}),
                    extractor=SingleTurnRequirementExtractor("Power BI Pro", 10),
                )

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                self.assertTrue(
                    session is None
                    or (
                        session.estate is None
                        and not session.capture_messages
                        and session.confirmed_as_is is None
                    )
                )

    async def test_direct_and_polite_requirement_capture_still_works(self) -> None:
        accepted = (
            "Add 10 Power BI Pro licences",
            "Can you please add 10 Power BI Pro licences?",
        )
        for index, message in enumerate(accepted, start=1):
            with self.subTest(message=message):
                sender = f"language-capture-allow-{index}"
                client = FakeWhatsAppClient(
                    WhatsAppMedia(b"", "unused", "text/plain")
                )
                service = self._service(
                    client,
                    _ScriptedInterpreter(
                        {
                            message: _agent_intent(
                                "capture_requirement",
                                product_query="Power BI Pro",
                                quantity=10,
                            )
                        }
                    ),
                    extractor=SingleTurnRequirementExtractor("Power BI Pro", 10),
                )

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                self.assertIsNotNone(session)
                self.assertIsNotNone(session.estate)  # type: ignore[union-attr]
                self.assertEqual(session.estate.total_renewal_quantity, 10)  # type: ignore[union-attr]

    async def test_model_labels_cannot_mutate_without_assertive_seller_instruction(self) -> None:
        sender = "language-commercial-boundary"
        await self._prepare_confirmed_renewal(sender)
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        before = self._commercial_snapshot(session)
        blocked = {
            "Is 50 licences enough for L1?": _agent_intent(
                "set_quantity", line_id="L1", quantity=50
            ),
            "What would adding 10 Power BI Pro licences do?": _agent_intent(
                "add_sku", product_query="Power BI Pro", quantity=10
            ),
            "If the customer agrees, remove L1": _agent_intent(
                "set_disposition", line_id="L1", disposition="remove"
            ),
            "No need to replace L1 with Power BI Pro": _agent_intent(
                "replace_sku", line_id="L1", product_query="Power BI Pro"
            ),
            'The customer said "set Copilot to 25"': _agent_intent(
                "set_copilot", copilot_quantity=25
            ),
            "ME5 is expensive": _agent_intent(
                "build_scenario", scenario="me5_copilot"
            ),
            "USD is volatile": _agent_intent("set_currency", currency="USD"),
            "Annual licensing is expensive": _agent_intent(
                "set_term", term_duration="P1Y"
            ),
            "I want pricing for 10 Power BI Pro licences": _agent_intent(
                "add_sku", product_query="Power BI Pro", quantity=10
            ),
            "The customer asked to add 10 Power BI Pro licences": _agent_intent(
                "add_sku", product_query="Power BI Pro", quantity=10
            ),
        }
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(client, _ScriptedInterpreter(blocked))

        for message in blocked:
            with self.subTest(message=message):
                await service._handle_text(sender, message)
                after = await self.orchestrator.get_session(sender)
                assert after is not None
                self.assertEqual(self._commercial_snapshot(after), before)

    async def test_direct_polite_quantity_change_uses_seller_value(self) -> None:
        sender = "language-direct-quantity"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
        message = f"Could you please change {source.line_id} to 50 licences?"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "set_quantity",
                        line_id=source.line_id,
                        quantity=50,
                    )
                }
            ),
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        line = next(
            item
            for item in session.scenarios[session.active_scenario].lines
            if item.line_id == source.line_id
        )
        self.assertEqual(line.proposed_quantity, 50)

    async def test_model_cannot_invent_line_quantity_product_or_scenario(self) -> None:
        sender = "language-invented-fields"
        tests = (
            (
                "Change the quantity",
                _agent_intent("set_quantity", line_id="L2", quantity=50),
            ),
            (
                "Change L1 quantity",
                _agent_intent("set_quantity", line_id="L1", quantity=50),
            ),
            (
                "Add 10 licences",
                _agent_intent(
                    "add_sku",
                    product_query="Power BI Pro",
                    quantity=10,
                ),
            ),
        )
        for index, (message, intent) in enumerate(tests, start=1):
            with self.subTest(message=message):
                local_sender = f"{sender}-{index}"
                await self._prepare_confirmed_renewal(local_sender)
                before_session = await self.orchestrator.get_session(local_sender)
                assert before_session is not None
                before = self._commercial_snapshot(before_session)
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter({message: intent}),
                )

                await service._handle_text(local_sender, message)

                after = await self.orchestrator.get_session(local_sender)
                assert after is not None
                self.assertEqual(self._commercial_snapshot(after), before)
                self.assertIsNotNone(after.pending_dialogue)

    async def test_model_invented_scenario_does_not_block_valid_current_proposal_add(
        self,
    ) -> None:
        sender = "language-invented-scenario-valid-add"
        await self._prepare_confirmed_renewal(sender)
        message = "Add 10 Power BI Pro licences"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
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
        assert session is not None
        self.assertEqual(session.active_scenario, ScenarioType.RENEW_AS_IS)
        self.assertNotIn(ScenarioType.ME5_COPILOT, session.scenarios)
        self.assertTrue(
            any(
                line.sku_title == "Power BI Pro" and line.proposed_quantity == 10
                for line in session.scenarios[ScenarioType.RENEW_AS_IS].lines
            )
        )

    async def test_pending_quantity_accepts_bare_answer_but_not_narrative(self) -> None:
        blocked = {
            "Is 15 enough?": "question",
            "15 was the old amount": "narrative",
            "No need for 15": "negation",
        }
        for index, message in enumerate(blocked, start=1):
            with self.subTest(message=message):
                sender = f"language-pending-quantity-block-{index}"
                renewal = await self._prepare_confirmed_renewal(sender)
                source = renewal.lines[0]
                pending = PendingDialogue(
                    kind="agent_clarification",
                    question=f"What quantity should I set for {source.sku_title}?",
                    operation="set_quantity",
                    awaiting_slot="quantity",
                    scope="scenario",
                    source_line_id=source.line_id,
                )
                await self.orchestrator.set_pending_dialogue(sender, pending)
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter(
                        {
                            message: _agent_intent(
                                "set_quantity",
                                line_id=source.line_id,
                                quantity=15,
                            )
                        }
                    ),
                )
                await service._handle_text(sender, message)
                unchanged = await self.orchestrator.get_session(sender)
                assert unchanged is not None and unchanged.active_scenario is not None
                line = next(
                    item
                    for item in unchanged.scenarios[unchanged.active_scenario].lines
                    if item.line_id == source.line_id
                )
                self.assertEqual(line.proposed_quantity, source.proposed_quantity)
                self.assertIsNotNone(unchanged.pending_dialogue)
                self.assertEqual(
                    unchanged.pending_dialogue.operation,  # type: ignore[union-attr]
                    pending.operation,
                )
                self.assertEqual(
                    unchanged.pending_dialogue.source_line_id,  # type: ignore[union-attr]
                    pending.source_line_id,
                )

        sender = "language-pending-quantity-direct"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
        pending = PendingDialogue(
            kind="agent_clarification",
            question=f"What quantity should I set for {source.sku_title}?",
            operation="set_quantity",
            awaiting_slot="quantity",
            scope="scenario",
            source_line_id=source.line_id,
        )
        await self.orchestrator.set_pending_dialogue(sender, pending)
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {"15": _agent_intent("answer_question", response_text="15 noted")}
            ),
        )
        await service._handle_text(sender, "15")
        completed = await self.orchestrator.get_session(sender)
        assert completed is not None and completed.active_scenario is not None
        line = next(
            item
            for item in completed.scenarios[completed.active_scenario].lines
            if item.line_id == source.line_id
        )
        self.assertEqual(line.proposed_quantity, 15)
        self.assertIsNone(completed.pending_dialogue)

    async def test_pending_product_accepts_catalogue_answer_but_not_question(self) -> None:
        blocked = {
            "What is Planner?": _agent_intent(
                "add_sku", product_query="Planner", quantity=5
            ),
            "Planner is expensive": _agent_intent(
                "add_sku", product_query="Planner", quantity=5
            ),
        }
        for index, (message, intent) in enumerate(blocked.items(), start=1):
            with self.subTest(message=message):
                sender = f"language-pending-product-block-{index}"
                await self._prepare_confirmed_renewal(sender)
                pending = PendingDialogue(
                    kind="agent_clarification",
                    question="Which exact Microsoft product should I add?",
                    operation="add_sku",
                    awaiting_slot="product",
                    scope="scenario",
                    quantity=5,
                )
                await self.orchestrator.set_pending_dialogue(sender, pending)
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter({message: intent}),
                )
                await service._handle_text(sender, message)
                unchanged = await self.orchestrator.get_session(sender)
                assert unchanged is not None
                self.assertIsNotNone(unchanged.pending_dialogue)
                self.assertEqual(
                    unchanged.pending_dialogue.operation,  # type: ignore[union-attr]
                    pending.operation,
                )
                self.assertEqual(
                    unchanged.pending_dialogue.awaiting_slot,  # type: ignore[union-attr]
                    pending.awaiting_slot,
                )
                self.assertIsNone(unchanged.pending_sku_change)

        sender = "language-pending-product-direct"
        await self._prepare_confirmed_renewal(sender)
        pending = PendingDialogue(
            kind="agent_clarification",
            question="Which exact Microsoft product should I add?",
            operation="add_sku",
            awaiting_slot="product",
            scope="scenario",
            quantity=5,
        )
        await self.orchestrator.set_pending_dialogue(sender, pending)
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {"Planner": _agent_intent("clarify", clarification="Which product?")}
            ),
        )
        await service._handle_text(sender, "Planner")
        completed = await self.orchestrator.get_session(sender)
        assert completed is not None
        self.assertIsNone(completed.pending_dialogue)
        self.assertTrue(
            completed.pending_sku_change is not None
            or any(
                "planner" in line.sku_title.casefold()
                for scenario in completed.scenarios.values()
                for line in scenario.lines
            )
        )

    async def test_pending_scenario_accepts_only_bare_scenario_answer(self) -> None:
        sender = "language-pending-scenario"
        await self._prepare_confirmed_renewal(sender)
        pending = PendingDialogue(
            kind="agent_clarification",
            question="Which proposal should I update?",
            operation="add_sku",
            awaiting_slot="scenario",
            scope="scenario",
            product_query="Power BI Pro",
            quantity=10,
        )
        await self.orchestrator.set_pending_dialogue(sender, pending)
        messages = {
            "What is ME5?": _agent_intent(
                "answer_question", response_text="ME5 is an enterprise option."
            ),
            "ME5": _agent_intent("capture_requirement", product_query="ME5"),
        }
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(messages),
        )

        await service._handle_text(sender, "What is ME5?")
        interrupted = await self.orchestrator.get_session(sender)
        assert interrupted is not None
        self.assertEqual(
            interrupted.pending_dialogue,
            pending.model_copy(update={"failed_attempts": 1}),
        )
        self.assertNotIn(ScenarioType.ME5_COPILOT, interrupted.scenarios)

        await service._handle_text(sender, "ME5")
        completed = await self.orchestrator.get_session(sender)
        assert completed is not None
        self.assertEqual(completed.active_scenario, ScenarioType.ME5_COPILOT)
        self.assertIsNone(completed.pending_dialogue)
        self.assertTrue(
            any(
                line.sku_title == "Power BI Pro" and line.proposed_quantity == 10
                for line in completed.scenarios[ScenarioType.ME5_COPILOT].lines
            )
        )

    async def test_candidate_questions_negation_and_reports_never_confirm_requirement(self) -> None:
        blocked = (
            "What does option 2 include?",
            "Do not choose option 2",
            "The customer selected option 2",
        )
        for index, message in enumerate(blocked, start=1):
            with self.subTest(message=message):
                sender = f"language-requirement-candidate-{index}"
                estate = await self._analyze_title(sender, "Power BI")
                self.assertTrue(estate.pending_lines)
                selection = {"line_id": "L1", "candidate_number": 2}
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter(
                        {
                            message: _agent_intent(
                                "confirm_matches",
                                match_selections=[selection],
                            )
                        }
                    ),
                )

                await service._handle_text(sender, message)

                unchanged = await self.orchestrator.get_session(sender)
                assert unchanged is not None and unchanged.estate is not None
                self.assertTrue(unchanged.estate.pending_lines)
                self.assertEqual(unchanged.estate.lines, estate.lines)

    async def test_direct_candidate_answer_confirms_requirement_choice(self) -> None:
        sender = "language-requirement-candidate-direct"
        estate = await self._analyze_title(sender, "Power BI")
        self.assertTrue(estate.pending_lines)
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    "Option 2": _agent_intent(
                        "confirm_matches",
                        match_selections=[
                            {"line_id": "L1", "candidate_number": 2}
                        ],
                    )
                }
            ),
        )

        await service._handle_text(sender, "Option 2")

        confirmed = await self.orchestrator.get_session(sender)
        assert confirmed is not None and confirmed.estate is not None
        self.assertFalse(confirmed.estate.pending_lines)

    async def test_candidate_questions_negation_and_reports_never_confirm_or_cancel_edit(
        self,
    ) -> None:
        blocked = (
            (
                "What does option 2 include?",
                _agent_intent("confirm_sku", candidate_number=2),
            ),
            (
                "The customer selected option 2",
                _agent_intent("confirm_sku", candidate_number=2),
            ),
            (
                "Do not cancel that change",
                _agent_intent("cancel_sku"),
            ),
            (
                "The customer said cancel that change",
                _agent_intent("cancel_sku"),
            ),
        )
        for index, (message, intent) in enumerate(blocked, start=1):
            with self.subTest(message=message):
                sender = f"language-proposal-candidate-{index}"
                await self._prepare_confirmed_renewal(sender)
                result = await self.orchestrator.add_sku(
                    sender,
                    product_query="Power BI",
                    quantity=10,
                )
                self.assertEqual(result.state, "confirmation_required")
                before = await self.orchestrator.get_session(sender)
                assert before is not None and before.pending_sku_change is not None
                pending_id = before.pending_sku_change.id
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter({message: intent}),
                )

                await service._handle_text(sender, message)

                unchanged = await self.orchestrator.get_session(sender)
                assert unchanged is not None and unchanged.pending_sku_change is not None
                self.assertEqual(unchanged.pending_sku_change.id, pending_id)

    async def test_direct_cancel_closes_pending_edit_without_mutation(self) -> None:
        sender = "language-proposal-candidate-cancel"
        await self._prepare_confirmed_renewal(sender)
        result = await self.orchestrator.add_sku(
            sender,
            product_query="Power BI",
            quantity=10,
        )
        self.assertEqual(result.state, "confirmation_required")
        before = await self.orchestrator.get_session(sender)
        assert before is not None
        commercial = self._commercial_snapshot(before)
        message = "Please cancel that change"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter({message: _agent_intent("cancel_sku")}),
        )

        await service._handle_text(sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertIsNone(after.pending_sku_change)
        self.assertEqual(self._commercial_snapshot(after), commercial)

    async def test_reset_requires_direct_seller_instruction(self) -> None:
        blocked = {
            "What does start fresh mean?": _agent_intent("reset_requirement"),
            "Do not start fresh": _agent_intent("reset_requirement"),
            "If the customer agrees, start fresh": _agent_intent(
                "reset_requirement"
            ),
            "The customer said start fresh": _agent_intent("reset_requirement"),
        }
        for index, (message, intent) in enumerate(blocked.items(), start=1):
            with self.subTest(message=message):
                sender = f"language-reset-block-{index}"
                await self._prepare_unconfirmed_requirement(sender)
                before = await self.orchestrator.get_session(sender)
                assert before is not None and before.estate is not None
                token = before.estate.capture_token
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter({message: intent}),
                )

                await service._handle_text(sender, message)

                after = await self.orchestrator.get_session(sender)
                assert after is not None and after.estate is not None
                self.assertEqual(after.estate.capture_token, token)

        sender = "language-reset-direct"
        await self._prepare_unconfirmed_requirement(sender)
        message = "Could you please start fresh?"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter({message: _agent_intent("reset_requirement")}),
        )
        await service._handle_text(sender, message)
        reset = await self.orchestrator.get_session(sender)
        assert reset is not None
        self.assertEqual(reset.stage, WorkflowStage.AWAITING_UPLOAD)
        self.assertIsNone(reset.estate)
        self.assertFalse(reset.scenarios)

    async def test_requirement_confirmation_requires_assertive_seller_approval(self) -> None:
        blocked = (
            "Can I confirm the requirement?",
            "What happens if I confirm it?",
            "I do not confirm the requirement",
            "The customer said confirm the requirement",
        )
        for index, message in enumerate(blocked, start=1):
            with self.subTest(message=message):
                sender = f"language-validation-block-{index}"
                await self._prepare_unconfirmed_requirement(sender)
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter(
                        {message: _agent_intent("confirm_validation")}
                    ),
                )

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                assert session is not None
                self.assertEqual(
                    session.stage,
                    WorkflowStage.AWAITING_INITIAL_VALIDATION,
                )
                self.assertIsNone(session.confirmed_as_is)

        sender = "language-validation-direct"
        await self._prepare_unconfirmed_requirement(sender)
        message = "I confirm the complete requirement for pricing"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {message: _agent_intent("confirm_validation")}
            ),
        )
        await service._handle_text(sender, message)
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNotNone(session.confirmed_as_is)

    async def test_finalization_requires_direct_request_and_explicit_gate_approval(self) -> None:
        blocked = (
            "Can I finalize the proposal?",
            "What happens if I finalize it?",
            "Do not finalize the proposal",
            'The customer said "finalize the proposal"',
            "If the customer agrees, finalize it",
        )
        for index, message in enumerate(blocked, start=1):
            with self.subTest(message=message):
                sender = f"language-finalize-block-{index}"
                await self._prepare_confirmed_renewal(sender)
                service = self._service(
                    FakeWhatsAppClient(
                        WhatsAppMedia(b"", "unused", "text/plain")
                    ),
                    _ScriptedInterpreter({message: _agent_intent("finalize")}),
                )

                await service._handle_text(sender, message)

                session = await self.orchestrator.get_session(sender)
                assert session is not None
                self.assertNotEqual(
                    session.stage,
                    WorkflowStage.AWAITING_FINAL_VALIDATION,
                )
                self.assertNotEqual(session.stage, WorkflowStage.FINALIZED)

        sender = "language-finalize-direct"
        await self._prepare_confirmed_renewal(sender)
        request = "Could you please finalize the proposal?"
        unsafe_approval = "What happens if I finalize it?"
        approval = "Yes, finalize the proposal"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    request: _agent_intent("finalize"),
                    unsafe_approval: _agent_intent("finalize"),
                    approval: _agent_intent("finalize"),
                }
            ),
        )

        await service._handle_text(sender, request)
        awaiting = await self.orchestrator.get_session(sender)
        assert awaiting is not None
        self.assertEqual(awaiting.stage, WorkflowStage.AWAITING_FINAL_VALIDATION)

        await service._handle_text(sender, unsafe_approval)
        still_awaiting = await self.orchestrator.get_session(sender)
        assert still_awaiting is not None
        self.assertEqual(
            still_awaiting.stage,
            WorkflowStage.AWAITING_FINAL_VALIDATION,
        )

        await service._handle_text(sender, approval)
        finalized = await self.orchestrator.get_session(sender)
        assert finalized is not None
        self.assertEqual(finalized.stage, WorkflowStage.FINALIZED)

    async def test_duplicate_product_caption_does_not_double_count_image_or_document(
        self,
    ) -> None:
        for index, message_type in enumerate(("image", "document"), start=1):
            with self.subTest(message_type=message_type):
                sender = f"language-caption-dedupe-{message_type}"
                extractor = FakeRequirementExtractor()
                if message_type == "image":
                    media = WhatsAppMedia(b"image", "image.png", "image/png")
                    inbound = {
                        "id": f"caption-image-{index}",
                        "from": sender,
                        "type": "image",
                        "image": {
                            "id": "image-media",
                            "mime_type": "image/png",
                            "caption": "25 Power BI Pro licences",
                        },
                    }
                else:
                    media = WhatsAppMedia(b"file", "requirement.txt", "text/plain")
                    inbound = {
                        "id": f"caption-document-{index}",
                        "from": sender,
                        "type": "document",
                        "document": {
                            "id": "document-media",
                            "filename": "requirement.txt",
                            "mime_type": "text/plain",
                            "caption": "25 Power BI Pro licences",
                        },
                    }
                service = self._service(
                    FakeWhatsAppClient(media),
                    extractor=extractor,
                )

                await service.handle(_webhook(inbound))

                session = await self.orchestrator.get_session(sender)
                assert session is not None and session.estate is not None
                self.assertEqual(len(session.estate.lines), 1)
                self.assertEqual(session.estate.total_renewal_quantity, 25)

    async def test_information_interruption_preserves_exactly_one_pending_question(
        self,
    ) -> None:
        sender = "language-information-interruption"
        await self._prepare_confirmed_renewal(sender)
        pending = PendingDialogue(
            kind="agent_clarification",
            question="How many Visio Plan 2 licences should I add?",
            context_message="Add Visio Plan 2",
            operation="add_sku",
            awaiting_slot="quantity",
            scope="scenario",
            product_query="Visio Plan 2",
        )
        await self.orchestrator.set_pending_dialogue(sender, pending)
        question = "What does Visio Plan 2 include?"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    question: _agent_intent(
                        "answer_question",
                        response_text=(
                            "Visio Plan 2 is a diagramming subscription."
                        ),
                    )
                }
            ),
        )

        await service._handle_text(sender, question)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(
            session.pending_dialogue,
            pending.model_copy(update={"failed_attempts": 1}),
        )
        self.assertIsNone(session.pending_sku_change)
        bodies = _text_bodies(client)
        self.assertEqual(
            sum(pending.question.casefold() in body.casefold() for body in bodies),
            1,
        )
        self.assertEqual(
            sum("diagramming subscription" in body.casefold() for body in bodies),
            1,
        )


if __name__ == "__main__":
    unittest.main()
