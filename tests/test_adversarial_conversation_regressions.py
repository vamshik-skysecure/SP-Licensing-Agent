from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.agent import AgentIntent
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import (
    MigrationDisposition,
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
        except KeyError as error:  # pragma: no cover - a test fixture contract failure
            raise AssertionError(f"No scripted intent for {message!r}") from error


class _QuietService(WhatsAppWebhookService):
    """Exercise state transitions without spending test time rendering PDFs/images."""

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


def _text_bodies(client: FakeWhatsAppClient) -> list[str]:
    return [
        message.text.body
        for message in client.messages
        if getattr(message, "text", None) is not None
    ]


class AdversarialConversationRegressionTests(unittest.IsolatedAsyncioTestCase):
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
    ) -> _QuietService:
        return _QuietService(
            client,  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=interpreter,
            requirement_extractor=extractor,  # type: ignore[arg-type]
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

    async def _analyze_titles(self, sender: str, *titles: str):
        return await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=index + 2,
                    product_title=title,
                    total_licenses=(index + 1) * 5,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=(index + 1) * 5,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
                for index, title in enumerate(titles)
            ],
        )

    async def test_complete_replace_supersedes_stale_pending_add(self) -> None:
        sender = "adversarial-stale-add"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="How many Visio Plan 2 licences should I add?",
                context_message="Add Visio Plan 2",
                operation="add_sku",
                awaiting_slot="quantity",
                scope="scenario",
                product_query="Visio Plan 2",
            ),
        )
        text = f"Replace {source.sku_title} with Power BI Pro for 9 licences"
        interpreter = _ScriptedInterpreter(
            {
                text: _agent_intent(
                    "replace_sku",
                    line_id=source.line_id,
                    product_query="Power BI Pro",
                    quantity=9,
                )
            }
        )
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            interpreter,
        )

        await service._handle_text(sender, text)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        self.assertFalse(any("Visio Plan 2" in line.sku_title for line in scenario.lines))
        replacement = next(
            line for line in scenario.lines if line.sku_title == "Power BI Pro"
        )
        self.assertEqual(replacement.proposed_quantity, 9)
        self.assertIsNone(session.pending_dialogue)

    async def test_complete_add_supersedes_stale_pending_sku_replacement(self) -> None:
        sender = "adversarial-stale-replacement-choice"
        renewal = await self._prepare_confirmed_renewal(sender)
        stale = await self.orchestrator.replace_sku(
            sender,
            renewal.lines[0].line_id,
            "Copilot",
            5,
        )
        self.assertEqual(stale.state, "confirmation_required")
        text = "Add 10 Visio Plan 2 licences"
        interpreter = _ScriptedInterpreter(
            {text: _agent_intent("add_sku", product_query="Visio Plan 2", quantity=10)}
        )
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            interpreter,
        )

        await service._handle_text(sender, text)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_sku_change is not None
        self.assertEqual(session.pending_sku_change.action, "add")
        self.assertEqual(session.pending_sku_change.product_query, "Visio Plan 2")
        self.assertEqual(session.pending_sku_change.quantity, 10)
        self.assertIsNone(session.pending_sku_change.source_line_id)

    async def test_numeric_slot_wins_over_conversational_model_label(self) -> None:
        sender = "adversarial-numeric-slot"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="How many Microsoft Defender for Endpoint P2 licences should I add?",
                context_message="Add Microsoft Defender for Endpoint P2",
                operation="add_sku",
                awaiting_slot="quantity",
                scope="scenario",
                product_query="Microsoft Defender for Endpoint P2",
            ),
        )
        interpreter = _ScriptedInterpreter(
            {"51": _agent_intent("answer_question", response_text="I noted 51.")}
        )
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            interpreter,
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
            )
        )
        self.assertIsNone(session.pending_dialogue)

    async def test_out_of_scope_interruption_clears_structured_pending_slots(self) -> None:
        sender = "adversarial-pending-interruption"
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
        question = "Who is Virat Kohli?"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    question: _agent_intent(
                        "out_of_scope",
                        response_text="That is outside this licensing advisor's scope.",
                    )
                }
            ),
        )

        await service._handle_text(sender, question)

        interrupted = await self.orchestrator.get_session(sender)
        assert interrupted is not None
        self.assertIsNone(interrupted.pending_dialogue)
        self.assertIsNone(interrupted.pending_sku_change)
        assert interrupted.active_scenario is not None
        self.assertFalse(
            any(
                line.sku_title == "Visio Plan 2" and line.proposed_quantity == 7
                for line in interrupted.scenarios[interrupted.active_scenario].lines
            )
        )

    async def test_final_validation_correction_reopens_then_requires_fresh_confirmation(self) -> None:
        sender = "adversarial-final-correction"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
        await self.orchestrator.request_finalization(sender)
        text = f"Change {source.sku_title} to 77 licences"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {text: _agent_intent("set_quantity", line_id=source.line_id, quantity=77)}
            ),
        )

        await service._handle_text(sender, text)

        corrected = await self.orchestrator.get_session(sender)
        assert corrected is not None and corrected.active_scenario is not None
        self.assertEqual(corrected.stage, WorkflowStage.REVIEWING_SCENARIO)
        self.assertEqual(
            next(
                line.proposed_quantity
                for line in corrected.scenarios[corrected.active_scenario].lines
                if line.line_id == source.line_id
            ),
            77,
        )
        await self.orchestrator.request_finalization(sender)
        awaiting = await self.orchestrator.get_session(sender)
        assert awaiting is not None
        self.assertEqual(awaiting.stage, WorkflowStage.AWAITING_FINAL_VALIDATION)

    async def test_remove_it_answers_disposition_instead_of_cancelling(self) -> None:
        sender = "adversarial-remove-disposition"
        renewal = await self._prepare_confirmed_renewal(sender)
        source = renewal.lines[0]
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question=f"Should I retain, remove, migrate, or include {source.sku_title}?",
                operation="set_disposition",
                awaiting_slot="disposition",
                scope="scenario",
                source_line_id=source.line_id,
            ),
        )
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {"remove it": _agent_intent("set_disposition", disposition="remove")}
            ),
        )

        await service._handle_text(sender, "remove it")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        line = next(
            item
            for item in session.scenarios[session.active_scenario].lines
            if item.line_id == source.line_id
        )
        self.assertEqual(line.disposition, MigrationDisposition.REMOVE)
        self.assertEqual(line.proposed_quantity, 0)
        self.assertIsNone(session.pending_dialogue)

    async def test_explicit_me5_edit_builds_target_when_only_renew_exists(self) -> None:
        sender = "adversarial-explicit-me5-target"
        renewal = await self._prepare_confirmed_renewal(sender)
        self.assertEqual((await self.orchestrator.get_session(sender)).scenarios.keys(), {ScenarioType.RENEW_AS_IS})  # type: ignore[union-attr]
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    "Set L1 to 52 in ME5": _agent_intent(
                        "set_quantity",
                        scenario="me5_copilot",
                        line_id="L1",
                        quantity=52,
                    )
                }
            ),
        )

        await service._handle_text(sender, "Set L1 to 52 in ME5")

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIn(ScenarioType.ME5_COPILOT, session.scenarios)
        self.assertEqual(session.active_scenario, ScenarioType.ME5_COPILOT)
        self.assertEqual(
            next(
                line.proposed_quantity
                for line in session.scenarios[ScenarioType.ME5_COPILOT].lines
                if line.line_id == "L1"
            ),
            52,
        )
        self.assertEqual(
            next(line.proposed_quantity for line in renewal.lines if line.line_id == "L1"),
            next(
                line.proposed_quantity
                for line in session.scenarios[ScenarioType.RENEW_AS_IS].lines
                if line.line_id == "L1"
            ),
        )

    async def test_unresolved_correction_targets_named_line_and_vague_request_asks(self) -> None:
        sender = "adversarial-unresolved-target"
        estate = await self._analyze_titles(sender, "E1", "E3")
        self.assertEqual([line.line_id for line in estate.pending_lines], ["L1", "L2"])
        text = "Use Microsoft 365 E3 for the E3 licence"
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            _ScriptedInterpreter(
                {
                    text: _agent_intent(
                        "replace_sku",
                        product_query="Microsoft 365 E3",
                        quantity=10,
                    )
                }
            ),
            extractor=SingleTurnRequirementExtractor("Microsoft 365 E3", 10),
        )

        await service._handle_text(sender, text)

        targeted = await self.orchestrator.get_session(sender)
        assert targeted is not None and targeted.estate is not None
        self.assertTrue(any(line.line_id == "L1" for line in targeted.estate.pending_lines))
        self.assertFalse(any(line.line_id == "L2" for line in targeted.estate.pending_lines))

        vague_sender = "adversarial-unresolved-ambiguous"
        vague_estate = await self._analyze_titles(vague_sender, "E1", "E3")
        vague_text = "Use Microsoft 365 E5"
        vague_client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        vague_service = self._service(
            vague_client,
            _ScriptedInterpreter(
                {
                    vague_text: _agent_intent(
                        "replace_sku",
                        product_query="Microsoft 365 E5",
                        quantity=10,
                    )
                }
            ),
            extractor=SingleTurnRequirementExtractor("Microsoft 365 E5", 10),
        )
        await vague_service._handle_text(vague_sender, vague_text)
        ambiguous = await self.orchestrator.get_session(vague_sender)
        assert ambiguous is not None and ambiguous.estate is not None
        self.assertEqual(ambiguous.estate.lines, vague_estate.lines)
        self.assertIsNotNone(ambiguous.pending_dialogue)
        self.assertEqual(ambiguous.pending_dialogue.awaiting_slot, "line")  # type: ignore[union-attr]
        response = "\n".join(_text_bodies(vague_client))
        self.assertIn("L1", response)
        self.assertIn("L2", response)

    async def test_invalid_bulk_removal_is_atomic(self) -> None:
        sender = "adversarial-bulk-remove"
        await self._analyze_titles(
            sender,
            "Microsoft 365 E3",
            "Power BI Pro",
            "Microsoft Teams Phone Standard",
        )
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.estate is not None
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(client)

        handled = await service._try_handle_bulk_requirement_removal(
            sender,
            "Remove L1 and L999",
            before,
        )

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.estate is not None
        self.assertTrue(handled)
        self.assertEqual(after.estate.lines, before.estate.lines)
        self.assertIn("did not remove anything", "\n".join(_text_bodies(client)))

    async def test_stale_interactive_match_token_cannot_mutate_new_capture(self) -> None:
        sender = "adversarial-stale-capture-token"
        old = await self._analyze_titles(sender, "E1")
        self.assertTrue(old.pending_lines)
        old_token = old.capture_token[:16]
        await self.orchestrator.reset_session(sender)
        current = await self._analyze_titles(sender, "E3")
        self.assertTrue(current.pending_lines)
        before_lines = current.lines
        client = FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain"))
        service = self._service(client)

        await service.handle(
            _webhook(
                {
                    "id": "stale-capture-selection",
                    "from": sender,
                    "type": "interactive",
                    "interactive": {
                        "type": "list_reply",
                        "list_reply": {
                            "id": f"licensing|match_confirm|{old_token}|L1|1",
                            "title": "Option 1",
                        },
                    },
                }
            )
        )

        after = await self.orchestrator.get_session(sender)
        assert after is not None and after.estate is not None
        self.assertEqual(after.estate.capture_token, current.capture_token)
        self.assertEqual(after.estate.lines, before_lines)
        response = "\n".join(_text_bodies(client)).casefold()
        self.assertIn("earlier requirement", response)
        self.assertIn("no longer active", response)
        self.assertNotIn("l1 is not awaiting", response)

    async def test_media_cannot_overwrite_finalized_requirement_without_start_fresh(self) -> None:
        sender = "adversarial-finalized-media"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.request_finalization(sender)
        await self.orchestrator.confirm_finalization(sender)
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.estate is not None
        client = FakeWhatsAppClient(
            WhatsAppMedia(CUSTOMER.read_bytes(), CUSTOMER.name, "text/csv")
        )
        service = self._service(client)

        await service.handle(
            _webhook(
                {
                    "id": "blocked-finalized-file",
                    "from": sender,
                    "type": "document",
                    "document": {
                        "id": "customer-file",
                        "filename": CUSTOMER.name,
                        "mime_type": "text/csv",
                    },
                }
            )
        )
        blocked = await self.orchestrator.get_session(sender)
        assert blocked is not None and blocked.estate is not None
        self.assertEqual(blocked.stage, WorkflowStage.FINALIZED)
        self.assertEqual(blocked.estate.capture_token, before.estate.capture_token)

        await service.handle(
            _webhook(
                {
                    "id": "fresh-finalized-file",
                    "from": sender,
                    "type": "document",
                    "document": {
                        "id": "customer-file-fresh",
                        "filename": CUSTOMER.name,
                        "mime_type": "text/csv",
                        "caption": "Start fresh",
                    },
                }
            )
        )
        fresh = await self.orchestrator.get_session(sender)
        assert fresh is not None and fresh.estate is not None
        self.assertNotEqual(fresh.estate.capture_token, before.estate.capture_token)
        self.assertIsNone(fresh.confirmed_as_is)
        self.assertNotEqual(fresh.stage, WorkflowStage.FINALIZED)

    async def test_repeated_new_capture_does_not_delete_unseen_saved_draft(self) -> None:
        sender = "adversarial-saved-draft"
        estate = await self._analyze_titles(sender, "Microsoft 365 E3")
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="resume_session",
                question="Resume the saved draft or start fresh?",
                operation="none",
                awaiting_slot="resume_choice",
            ),
        )
        extractor = SingleTurnRequirementExtractor("Power BI Pro", 25)
        text = "25 Power BI Pro licences"
        interpreter = _ScriptedInterpreter(
            {text: _agent_intent("capture_requirement", product_query="Power BI Pro", quantity=25)}
        )
        service = self._service(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
            interpreter,
            extractor=extractor,
        )

        await service._handle_text(sender, text)
        await service._handle_text(sender, text)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.estate.capture_token, estate.capture_token)
        self.assertEqual(session.estate.lines, estate.lines)
        self.assertEqual(extractor.inputs, [])
        self.assertIsNone(session.pending_dialogue)

    async def test_one_word_planner_and_viva_fill_pending_product_slot(self) -> None:
        for product in ("Planner", "Viva"):
            with self.subTest(product=product):
                sender = f"adversarial-one-word-{product.casefold()}"
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
                service = self._service(
                    FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),
                    _ScriptedInterpreter(
                        {
                            product: _agent_intent(
                                "clarify",
                                clarification="Which exact product?",
                            )
                        }
                    ),
                )

                await service._handle_text(sender, product)

                session = await self.orchestrator.get_session(sender)
                assert session is not None
                self.assertIsNone(session.pending_dialogue)
                if session.pending_sku_change is not None:
                    self.assertEqual(session.pending_sku_change.action, "add")
                    self.assertEqual(session.pending_sku_change.product_query, product)
                    self.assertEqual(session.pending_sku_change.quantity, 5)
                else:
                    assert session.active_scenario is not None
                    self.assertTrue(
                        any(
                            product.casefold() in line.sku_title.casefold()
                            and line.proposed_quantity == 5
                            for line in session.scenarios[session.active_scenario].lines
                        )
                    )

    async def test_common_copilot_typo_returns_only_relevant_candidates(self) -> None:
        candidates = await self.orchestrator.catalog_candidates("micosoft copliot")

        self.assertTrue(candidates)
        self.assertIn("copilot", candidates[0].sku_title.casefold())
        self.assertTrue(
            all("copilot" in candidate.sku_title.casefold() for candidate in candidates[:5])
        )


if __name__ == "__main__":
    unittest.main()
