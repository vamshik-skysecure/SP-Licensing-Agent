from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.agent import AgentIntent
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
from tests.test_simple_pricing_workflow import (
    CUSTOMER,
    WORKBOOK,
    FakeWhatsAppClient,
    _agent_intent,
    _webhook,
)


class _ScriptedInterpreter:
    def __init__(self, mapping: dict[str, AgentIntent]) -> None:
        self.mapping = mapping

    async def interpret(self, message: str, *_: object) -> AgentIntent:
        try:
            return self.mapping[message]
        except KeyError as error:  # pragma: no cover - fixture contract failure
            raise AssertionError(f"No scripted intent for {message!r}") from error


class _StateOnlyService(WhatsAppWebhookService):
    """Exercise real state transitions without rendering commercial artifacts."""

    async def _send_scenario(self, sender: str, scenario: object) -> None:
        await self._send_text(sender, "scenario-updated")

    async def _send_updated_requirement(self, sender: str, estate: object) -> None:
        await self._send_text(sender, "requirement-updated")

    async def _send_simple_as_is(self, sender: str, scenario: object) -> None:
        await self._send_text(sender, "as-is-priced")

    async def _send_recommendation_prompt(self, sender: str) -> None:
        await self._send_text(sender, "recommendation-prompt")

    async def _send_enterprise_comparison(self, sender: str) -> None:
        await self._orchestrator.comparison(sender)
        await self._send_text(sender, "enterprise-comparison")


class ConversationStateMachineAuditTests(unittest.IsolatedAsyncioTestCase):
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
        interpreter: _ScriptedInterpreter | None = None,
    ) -> _StateOnlyService:
        return _StateOnlyService(
            FakeWhatsAppClient(WhatsAppMedia(b"", "unused", "text/plain")),  # type: ignore[arg-type]
            self.orchestrator,
            self.configuration,
            intent_interpreter=interpreter,
        )

    async def _send(self, service: _StateOnlyService, sender: str, text: str) -> None:
        self.message_number += 1
        await service.handle(
            _webhook(
                {
                    "id": f"state-audit-{self.message_number}",
                    "from": sender,
                    "type": "text",
                    "text": {"body": text},
                }
            )
        )

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

    async def test_explicit_confirmation_cannot_drop_pending_add_and_price_old_estate(self) -> None:
        sender = "state-audit-confirm-pending-add"
        estate = await self._analyze_titles(sender, "Microsoft 365 E3")
        self.assertFalse(estate.pending_lines)
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="How many Visio Plan 2 licences should I add?",
                context_message="Add Visio Plan 2",
                operation="add_sku",
                awaiting_slot="quantity",
                scope="requirement",
                product_query="Visio Plan 2",
            ),
        )

        await self._send(self._service(), sender, "confirm requirement")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertIsNone(session.confirmed_as_is)
        self.assertFalse(session.scenarios)
        self.assertEqual(session.estate.lines, estate.lines)

    async def test_unresolved_sku_blocks_both_comparison_and_scenario_build(self) -> None:
        cases = (
            (
                "compare",
                "Compare all four options",
                _agent_intent("compare_enterprise_options"),
            ),
            (
                "build",
                "Prepare ME5",
                _agent_intent("build_scenario", scenario="me5_copilot"),
            ),
        )
        for label, text, intent in cases:
            with self.subTest(route=label):
                sender = f"state-audit-unresolved-{label}"
                estate = await self._analyze_titles(sender, "E1")
                self.assertTrue(estate.pending_lines)
                service = self._service(_ScriptedInterpreter({text: intent}))

                await self._send(service, sender, text)

                session = await self.orchestrator.get_session(sender)
                assert session is not None and session.estate is not None
                self.assertEqual(session.stage, WorkflowStage.AWAITING_MATCH_CONFIRMATION)
                self.assertTrue(session.estate.pending_lines)
                self.assertIsNone(session.confirmed_as_is)
                self.assertFalse(session.scenarios)
                self.assertIsNone(session.active_scenario)

    async def test_cancel_is_processed_before_comment_slot_compatibility(self) -> None:
        sender = "state-audit-cancel-comment"
        renewal = await self._prepare_confirmed_renewal(sender)
        before_comments = list(renewal.comments)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="What comment should I add?",
                operation="add_comment",
                awaiting_slot="comment",
                scope="scenario",
            ),
        )
        service = self._service(
            _ScriptedInterpreter(
                {"cancel": _agent_intent("clarify", clarification="What comment?")}
            )
        )

        await self._send(service, sender, "cancel")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        self.assertEqual(
            session.scenarios[session.active_scenario].comments,
            before_comments,
        )
        self.assertIsNone(session.pending_dialogue)

    async def test_cancel_advisory_dialogue_does_not_delete_unresolved_requirement(self) -> None:
        sender = "state-audit-cancel-advisory"
        estate = await self._analyze_titles(sender, "E1")
        self.assertEqual(len(estate.pending_lines), 1)
        await self.orchestrator.set_pending_dialogue(
            sender,
            PendingDialogue(
                kind="agent_clarification",
                question="What business capability should the licence support?",
                context_message="Recommend a suitable licence",
            ),
        )

        await self._send(self._service(), sender, "cancel")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_MATCH_CONFIRMATION)
        self.assertEqual(session.estate.capture_token, estate.capture_token)
        self.assertEqual(session.estate.lines, estate.lines)
        self.assertIsNone(session.pending_dialogue)

    async def test_scenario_answer_advances_to_missing_line_without_losing_me5(self) -> None:
        sender = "state-audit-scenario-then-line"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.build_scenario(sender, ScenarioType.ME5_COPILOT)
        await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
        first = "Change a quantity to 10"
        second = "ME5"
        service = self._service(
            _ScriptedInterpreter(
                {
                    first: _agent_intent("set_quantity", quantity=10),
                    second: _agent_intent("clarify", clarification="Which licence?"),
                }
            )
        )

        await self._send(service, sender, first)
        await self._send(service, sender, second)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(session.pending_dialogue.operation, "set_quantity")
        self.assertEqual(session.pending_dialogue.scenario_type, ScenarioType.ME5_COPILOT)
        self.assertEqual(session.pending_dialogue.awaiting_slot, "line")
        self.assertEqual(session.pending_dialogue.quantity, 10)

    async def test_invalid_line_id_is_not_persisted_as_a_resolved_target(self) -> None:
        sender = "state-audit-invalid-line"
        await self._prepare_confirmed_renewal(sender)
        text = "Change L999 to 25"
        service = self._service(
            _ScriptedInterpreter(
                {text: _agent_intent("set_quantity", line_id="L999", quantity=25)}
            )
        )

        await self._send(service, sender, text)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_dialogue is not None
        self.assertEqual(session.pending_dialogue.operation, "set_quantity")
        self.assertEqual(session.pending_dialogue.awaiting_slot, "line")
        self.assertEqual(session.pending_dialogue.source_line_id, "")
        self.assertEqual(session.pending_dialogue.quantity, 25)

    async def test_zero_copilot_quantity_survives_scenario_target_follow_up(self) -> None:
        sender = "state-audit-zero-copilot"
        await self._prepare_confirmed_renewal(sender)
        await self.orchestrator.build_scenario(sender, ScenarioType.ME5_COPILOT)
        await self.orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
        first = "Set Copilot to 0"
        second = "ME5"
        service = self._service(
            _ScriptedInterpreter(
                {
                    first: _agent_intent("set_copilot", copilot_quantity=0),
                    second: _agent_intent("clarify", clarification="Which proposal?"),
                }
            )
        )

        await self._send(service, sender, first)
        await self._send(service, sender, second)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_dialogue)
        self.assertEqual(session.active_scenario, ScenarioType.ME5_COPILOT)
        copilot = next(
            line
            for line in session.scenarios[ScenarioType.ME5_COPILOT].lines
            if line.line_id == "COPILOT"
        )
        self.assertEqual(copilot.proposed_quantity, 0)


if __name__ == "__main__":
    unittest.main()
