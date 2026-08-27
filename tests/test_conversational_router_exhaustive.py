from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.agent import AgentIntent
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import (
    MigrationDisposition,
    PendingDialogue,
    ScenarioType,
    WorkflowStage,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from tests.test_simple_pricing_workflow import (
    CUSTOMER,
    WORKBOOK,
    FakeWhatsAppClient,
    SingleTurnRequirementExtractor,
    _agent_intent,
    _webhook,
)


class _ScriptedInterpreter:
    def __init__(self, mapping: dict[str, AgentIntent]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    async def interpret(self, message: str, *_: object) -> AgentIntent:
        self.calls.append(message)
        try:
            return self.mapping[message]
        except KeyError as error:  # pragma: no cover - fixture contract failure
            raise AssertionError(f"No scripted intent for {message!r}") from error


class _StateOnlyService(WhatsAppWebhookService):
    """Exercise production routing and state without rendering commercial files."""

    async def _send_scenario(self, sender: str, scenario: object) -> None:
        await self._send_text(sender, "scenario-updated")

    async def _send_updated_requirement(self, sender: str, estate: object) -> None:
        await self._send_text(sender, "requirement-updated")

    async def _send_sku_change_result(self, sender: str, result: object) -> None:
        await self._send_text(sender, str(getattr(result, "state", "updated")))

    async def _send_estate_table(self, sender: str, estate: object) -> None:
        return None

    async def _send_estate_report(self, sender: str, estate: object) -> None:
        return None

    async def _send_simple_as_is(self, sender: str, scenario: object) -> None:
        await self._send_text(sender, "as-is-priced")

    async def _send_recommendation_prompt(self, sender: str) -> None:
        await self._send_text(sender, "recommendation-prompt")

    async def _send_simple_revised(self, sender: str, scenario: object) -> None:
        await self._send_text(sender, "finalized-output")

    async def _send_simple_commercial_pdf(self, sender: str) -> None:
        await self._send_text(sender, "finalized-pdf")


def _text_bodies(client: FakeWhatsAppClient) -> list[str]:
    return [
        message.text.body
        for message in client.messages
        if getattr(message, "text", None) is not None
    ]


class ConversationalRouterExhaustiveTests(unittest.IsolatedAsyncioTestCase):
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
        self.message_number = 0

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    def _service(
        self,
        client: FakeWhatsAppClient,
        interpreter: _ScriptedInterpreter,
        *,
        extractor: object | None = None,
    ) -> _StateOnlyService:
        return _StateOnlyService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=interpreter,
            requirement_extractor=extractor,  # type: ignore[arg-type]
        )

    async def _send(
        self,
        service: _StateOnlyService,
        sender: str,
        text: str,
    ) -> None:
        self.message_number += 1
        await service.handle(
            _webhook(
                {
                    "id": f"router-exhaustive-{self.message_number}",
                    "from": sender,
                    "type": "text",
                    "text": {"body": text},
                }
            )
        )

    async def _prepare_confirmed_renewal(self, sender: str):
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
        return renewal

    async def test_out_of_scope_turn_never_fills_any_free_text_commercial_slot(self) -> None:
        cases = (
            ("add_comment", "comment", "What seller comment should I add?"),
            ("set_segment", "segment", "Which customer segment applies?"),
            ("set_term", "term", "What annual contract term should I apply?"),
            ("set_billing", "billing", "Which annual billing plan should I apply?"),
            ("set_currency", "currency", "Which currency are you asking about?"),
        )
        message = "Who is Virat Kohli?"
        for operation, slot, question in cases:
            with self.subTest(operation=operation):
                sender = f"router-out-of-scope-{operation}"
                before = await self._prepare_confirmed_renewal(sender)
                await self.orchestrator.set_pending_dialogue(
                    sender,
                    PendingDialogue(
                        kind="agent_clarification",
                        question=question,
                        context_message=f"Pending {operation}",
                        operation=operation,  # type: ignore[arg-type]
                        awaiting_slot=slot,  # type: ignore[arg-type]
                        scope="scenario",
                    ),
                )
                client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
                service = self._service(
                    client,
                    _ScriptedInterpreter(
                        {
                            message: _agent_intent(
                                "out_of_scope",
                                response_text=(
                                    "That is outside this licensing review; I can help with "
                                    "Microsoft licensing requirements."
                                ),
                            )
                        }
                    ),
                )

                await self._send(service, sender, message)

                session = await self.orchestrator.get_session(sender)
                assert session is not None and session.active_scenario is not None
                after = session.scenarios[session.active_scenario]
                self.assertEqual(after, before)
                self.assertIsNone(session.pending_dialogue)
                self.assertTrue(
                    any("outside" in body.casefold() for body in _text_bodies(client))
                )

    async def test_relevant_question_does_not_become_a_pending_seller_comment(self) -> None:
        sender = "router-question-not-comment"
        before = await self._prepare_confirmed_renewal(sender)
        pending = PendingDialogue(
            kind="agent_clarification",
            question="What seller comment should I add?",
            context_message="Add a seller comment",
            operation="add_comment",
            awaiting_slot="comment",
            scope="scenario",
        )
        await self.orchestrator.set_pending_dialogue(sender, pending)
        message = "What does Microsoft 365 E5 include?"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "answer_question",
                        response_text="It is a Microsoft enterprise plan.",
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        self.assertEqual(session.scenarios[session.active_scenario], before)
        self.assertEqual(
            session.pending_dialogue,
            pending.model_copy(update={"failed_attempts": 1}),
        )
        self.assertIn(
            "It is a Microsoft enterprise plan.",
            _text_bodies(client),
        )

    async def test_new_remove_instruction_supersedes_older_quantity_target_question(self) -> None:
        sender = "router-remove-old-qty"
        renewal = await self._prepare_confirmed_renewal(sender)
        target = renewal.lines[1]
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which licence should I change to 33?",
                context_message="Change a quantity to 33",
                operation="set_quantity",
                awaiting_slot="line",
                scope="scenario",
                quantity=33,
            ),
        )
        message = f"Remove {target.sku_title}"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "set_disposition",
                        line_id=target.line_id,
                        disposition="remove",
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        changed = next(
            line
            for line in session.scenarios[session.active_scenario].lines
            if line.line_id == target.line_id
        )
        self.assertEqual(changed.disposition, MigrationDisposition.REMOVE)
        self.assertNotEqual(changed.proposed_quantity, 33)
        self.assertIsNone(session.pending_dialogue)

    async def test_new_add_to_me5_supersedes_older_scenario_target_question(self) -> None:
        sender = "router-new-add-over-old-scenario"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.build_scenario(sender, ScenarioType.ME5_COPILOT)
        await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which proposal should receive 7 Copilot licences?",
                context_message="Set Copilot to 7",
                operation="set_copilot",
                awaiting_slot="scenario",
                scope="scenario",
                copilot_quantity=7,
            ),
        )
        message = "Add 15 Visio Plan 2 licences to ME5"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "add_sku",
                        scenario="me5_copilot",
                        product_query="Visio Plan 2",
                        quantity=15,
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        pending = session.pending_sku_change
        applied = any(
            "visio plan 2" in line.sku_title.casefold()
            and line.proposed_quantity == 15
            for line in session.scenarios[ScenarioType.ME5_COPILOT].lines
        )
        awaiting_confirmation = bool(
            pending is not None
            and pending.action == "add"
            and pending.product_query == "Visio Plan 2"
            and pending.quantity == 15
        )
        self.assertTrue(applied or awaiting_confirmation)

    async def test_unique_one_word_product_reference_resolves_without_line_id(self) -> None:
        sender = "router-unique-one-word-line"
        renewal = await self._prepare_confirmed_renewal(sender)
        target = next(line for line in renewal.lines if "Residency" in line.sku_title)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Which licence should I change to 12?",
                context_message="Change a quantity to 12",
                operation="set_quantity",
                awaiting_slot="line",
                scope="scenario",
                quantity=12,
            ),
        )
        message = "Residency"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {message: _agent_intent("clarify", clarification="Which licence?")}
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        changed = next(
            line
            for line in session.scenarios[session.active_scenario].lines
            if line.line_id == target.line_id
        )
        self.assertEqual(changed.proposed_quantity, 12)
        self.assertIsNone(session.pending_dialogue)

    async def test_choose_sku_without_source_first_asks_which_current_product(self) -> None:
        sender = "router-choose-sku-no-source"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question=(
                    "What would you like to change in the current proposal: quantity, "
                    "SKU, or disposition?"
                ),
                context_message="Change the current proposal",
                operation="choose_change",
                awaiting_slot="change_dimension",
                scope="scenario",
            ),
        )
        message = "SKU"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "clarify",
                        clarification="Which product should replace it?",
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(session.pending_dialogue.operation, "replace_sku")
        self.assertEqual(session.pending_dialogue.awaiting_slot, "line")
        self.assertEqual(session.pending_dialogue.source_line_id, "")
        latest = _text_bodies(client)[-1].casefold()
        self.assertIn("which licence", latest)
        self.assertNotIn("what product", latest)

    async def test_information_question_does_not_replace_saved_draft_resume_gate(self) -> None:
        sender = "router-resume-gate-information"
        await self._prepare_confirmed_renewal(sender)
        resume = PendingDialogue(
            kind="resume_session",
            question="Would you like to resume this saved draft, or start fresh?",
        )
        await self.orchestrator.set_pending_dialogue(sender, resume)
        message = "What is Microsoft 365 E5?"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "answer_question",
                        response_text=(
                            "Microsoft 365 E5 is an enterprise licensing plan. "
                            "Which capability are you evaluating?"
                        ),
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(session.pending_dialogue.kind, "resume_session")
        self.assertEqual(session.pending_dialogue.question, resume.question)

    async def test_new_requirement_does_not_merge_prior_information_question(self) -> None:
        sender = "router-info-not-capture"
        question = "What is Microsoft 365 E5?"
        requirement = "We need 10 Microsoft 365 E3 licences"
        extractor = SingleTurnRequirementExtractor("Microsoft 365 E3", 10)
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    question: _agent_intent(
                        "answer_question",
                        response_text=(
                            "Microsoft 365 E5 is an enterprise plan. "
                            "Would you like to compare its capabilities?"
                        ),
                    ),
                    requirement: _agent_intent(
                        "capture_requirement",
                        product_query="Microsoft 365 E3",
                        quantity=10,
                    ),
                }
            ),
            extractor=extractor,
        )

        await self._send(service, sender, question)
        await self._send(service, sender, requirement)

        self.assertEqual(extractor.inputs, [requirement])

    async def test_repeated_finalize_at_final_gate_is_explicit_final_approval(self) -> None:
        sender = "router-repeat-finalize"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.request_finalization(sender)
        message = "Finalize"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter({message: _agent_intent("finalize")}),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.FINALIZED)

    async def test_complete_product_correction_keeps_pending_replace_semantics(self) -> None:
        sender = "router-replace-correction"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
        pending = await self.orchestrator.replace_sku(
            sender,
            source.line_id,
            "Copilot",
            5,
        )
        self.assertEqual(pending.state, "confirmation_required")
        message = "Microsoft 365 E7 for one licence"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "capture_requirement",
                        product_query="Microsoft 365 E7",
                        quantity=1,
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        if session.pending_sku_change is not None:
            self.assertEqual(session.pending_sku_change.action, "replace")
            self.assertEqual(session.pending_sku_change.source_line_id, source.line_id)
            self.assertEqual(session.pending_sku_change.quantity, 1)
        else:
            assert session.active_scenario is not None
            revised_source = next(
                line
                for line in session.scenarios[session.active_scenario].lines
                if line.line_id == source.line_id
            )
            self.assertEqual(revised_source.proposed_quantity, 0)
            self.assertTrue(
                any(
                    "microsoft 365 e7" in line.sku_title.casefold()
                    and line.proposed_quantity == 1
                    for line in session.scenarios[session.active_scenario].lines
                )
            )

    async def test_information_answer_preserves_pending_sku_selection(self) -> None:
        sender = "router-sku-info-preserved"
        renewal = await self._prepare_confirmed_renewal(sender)
        requested = await self.orchestrator.replace_sku(
            sender,
            renewal.lines[0].line_id,
            "Copilot",
            5,
        )
        self.assertEqual(requested.state, "confirmation_required")
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.pending_sku_change is not None
        pending_id = before.pending_sku_change.id
        message = "What is the difference between these products?"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "answer_question",
                        response_text=(
                            "They are different Microsoft Copilot products. Which business "
                            "capability do you need?"
                        ),
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.pending_sku_change is not None
        self.assertEqual(after.pending_sku_change.id, pending_id)
        self.assertIsNone(after.pending_dialogue)

    async def test_safe_affirmative_resolves_unstructured_information_follow_up(self) -> None:
        sender = "router-safe-follow-up-yes"
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="Would you like guidance on selecting a licence?",
                context_message="Seller asked for licensing guidance.",
            ),
        )
        message = "Yes"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "answer_question",
                        response_text=(
                            "I can help compare Microsoft licence capabilities after you "
                            "share the user group and business need."
                        ),
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_dialogue)
        self.assertIn("business need", _text_bodies(client)[-1].casefold())

    def test_shared_verb_multi_edits_are_detected_before_partial_execution(self) -> None:
        unsafe = (
            "Add E3 and Copilot",
            "Remove L1 and L2",
            "Change L1 and L2 to 50 licences",
            "Add Microsoft 365 E3 and 10 Copilot licences",
            "Remove Power BI and Copilot",
        )
        for message in unsafe:
            with self.subTest(message=message):
                self.assertTrue(
                    WhatsAppWebhookService._contains_multiple_mutation_clauses(message)
                )
        self.assertFalse(
            WhatsAppWebhookService._contains_multiple_mutation_clauses(
                "Replace L1 with Microsoft 365 E5 and set the quantity to 50"
            )
        )
        safe_single_edits = (
            "Add Microsoft 365 E3 and set the quantity to 20",
            "Add E3 and make it 20 licences",
            "Change L1 to Microsoft 365 E3 and set the quantity to 20",
        )
        for message in safe_single_edits:
            with self.subTest(message=message):
                self.assertFalse(
                    WhatsAppWebhookService._contains_multiple_mutation_clauses(message)
                )

    async def test_shared_verb_multi_edit_is_rejected_without_mutating_proposal(self) -> None:
        sender = "router-multi-edit-atomic"
        renewal = await self._prepare_confirmed_renewal(sender)
        message = f"Remove {renewal.lines[0].line_id} and {renewal.lines[1].line_id}"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter(
                {
                    message: _agent_intent(
                        "set_disposition",
                        line_id=renewal.lines[0].line_id,
                        disposition="remove",
                    )
                }
            ),
        )

        await self._send(service, sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.active_scenario is not None
        active = after.scenarios[after.active_scenario]
        self.assertTrue(
            all(line.disposition != MigrationDisposition.REMOVE for line in active.lines)
        )
        self.assertIn("one product change at a time", _text_bodies(client)[-1].casefold())

    async def test_ambiguous_model_candidate_description_does_not_confirm(self) -> None:
        sender = "router-model-sku-confirm"
        renewal = await self._prepare_confirmed_renewal(sender)
        requested = await self.orchestrator.replace_sku(
            sender,
            renewal.lines[0].line_id,
            "Power BI Premium",
            10,
        )
        self.assertEqual(requested.state, "confirmation_required")
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_sku_change is not None
        self.assertFalse(session.pending_sku_change.candidate_narrowing_required)
        message = "the per-user choice you showed"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {message: _agent_intent("confirm_sku", candidate_number=1)}
            ),
        )

        await self._send(service, sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertIsNotNone(after.pending_sku_change)
        assert after.pending_sku_change is not None
        self.assertEqual(after.pending_sku_change.id, session.pending_sku_change.id)
        assert after.active_scenario is not None
        self.assertFalse(
            any(
                "power bi premium" in line.sku_title.casefold()
                and line.proposed_quantity == 10
                for line in after.scenarios[after.active_scenario].lines
            )
        )

    async def test_model_cancel_closes_pending_sku_once(self) -> None:
        sender = "router-model-sku-cancel"
        renewal = await self._prepare_confirmed_renewal(sender)
        requested = await self.orchestrator.replace_sku(
            sender,
            renewal.lines[0].line_id,
            "Power BI Premium",
            10,
        )
        self.assertEqual(requested.state, "confirmation_required")
        message = "please abandon the catalogue selection"
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(
            client,
            _ScriptedInterpreter({message: _agent_intent("cancel_sku")}),
        )

        await self._send(service, sender, message)

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertIsNone(after.pending_sku_change)
        latest = _text_bodies(client)[-1].casefold()
        self.assertIn("cancelled that product change", latest)
        self.assertNotIn("no pending", latest)


if __name__ == "__main__":
    unittest.main()
