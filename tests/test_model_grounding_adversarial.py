from __future__ import annotations

import unittest

from app.api.whatsapp.service import ServiceConfiguration
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import ParsedLicenseRow, PendingDialogue, ScenarioType
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from tests.test_adversarial_conversation_regressions import _ScriptedInterpreter
from tests.test_generic_seller_language_boundary import _BoundaryService
from tests.test_simple_pricing_workflow import (
    CUSTOMER,
    WORKBOOK,
    FakeWhatsAppClient,
    _agent_intent,
)


class AdversarialModelGroundingTests(unittest.IsolatedAsyncioTestCase):
    """Commercial values must remain safe under hostile model classifications."""

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

    async def _prepare_confirmed_single_line(self, sender: str):
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Power BI Pro",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        if estate.pending_lines:
            line = estate.pending_lines[0]
            candidate = line.candidates[0]
            await self.orchestrator.confirm_matches(
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
        await self.orchestrator.confirm_requirement(sender)
        renewal = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renewal)
        return renewal

    @staticmethod
    def _committed_snapshot(session) -> dict:
        return session.model_dump(
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

    @staticmethod
    def _active_scenario(session):
        assert session.active_scenario is not None
        return session.scenarios[session.active_scenario]

    @staticmethod
    def _text_bodies(client: FakeWhatsAppClient) -> list[str]:
        return [
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        ]

    async def test_out_of_domain_imperatives_cannot_mutate_commercial_state(self) -> None:
        cases = (
            (
                "timer",
                "Set a timer for 50 minutes",
                _agent_intent("set_quantity", line_id="L1", quantity=50),
                True,
            ),
            (
                "shopping-list",
                "Add milk to my shopping list",
                _agent_intent("add_comment", comment="milk"),
                False,
            ),
        )
        for suffix, message, intent, one_line in cases:
            with self.subTest(case=suffix):
                sender = f"adversarial-ood-{suffix}"
                if one_line:
                    await self._prepare_confirmed_single_line(sender)
                else:
                    await self._prepare_confirmed_renewal(sender)
                before = await self.orchestrator.get_session(sender)
                assert before is not None
                snapshot = self._committed_snapshot(before)
                _client, service = self._service({message: intent})

                await service._handle_text(sender, message)

                after = await self.orchestrator.get_session(sender)
                assert after is not None
                self.assertEqual(self._committed_snapshot(after), snapshot)
                self.assertIsNone(after.pending_dialogue)
                self.assertIsNone(after.pending_sku_change)

    async def test_pending_product_rejects_narrative_product_mention(self) -> None:
        narrative_sender = "adversarial-pending-product-narrative"
        await self._prepare_confirmed_renewal(narrative_sender)
        pending = PendingDialogue(
            kind="agent_clarification",
            question="Which exact Microsoft product should I add?",
            operation="add_sku",
            awaiting_slot="product",
            scope="scenario",
            quantity=5,
        )
        await self.orchestrator.set_pending_dialogue(narrative_sender, pending)
        before = await self.orchestrator.get_session(narrative_sender)
        assert before is not None
        snapshot = self._committed_snapshot(before)
        narrative = "I watched a video about Power BI Pro yesterday"
        _client, service = self._service(
            {
                narrative: _agent_intent(
                    "capture_requirement",
                    product_query="Power BI Pro",
                    quantity=5,
                )
            }
        )

        await service._handle_text(narrative_sender, narrative)

        after_narrative = await self.orchestrator.get_session(narrative_sender)
        assert after_narrative is not None
        self.assertEqual(self._committed_snapshot(after_narrative), snapshot)

    async def test_pending_product_accepts_bare_exact_title_despite_model_label(
        self,
    ) -> None:
        exact_sender = "adversarial-pending-product-exact"
        baseline = await self._prepare_confirmed_renewal(exact_sender)
        pending = PendingDialogue(
            kind="agent_clarification",
            question="Which exact Microsoft product should I add?",
            operation="add_sku",
            awaiting_slot="product",
            scope="scenario",
            quantity=5,
        )
        await self.orchestrator.set_pending_dialogue(exact_sender, pending)
        exact_title = "Power BI Pro"
        _client, service = self._service(
            {
                exact_title: _agent_intent(
                    "out_of_scope",
                    response_text="MODEL_WRONGLY_CALLED_THIS_OUT_OF_SCOPE",
                )
            }
        )

        await service._handle_text(exact_sender, exact_title)

        after_exact = await self.orchestrator.get_session(exact_sender)
        assert after_exact is not None
        existing_ids = {line.line_id for line in baseline.lines}
        added = [
            line
            for line in self._active_scenario(after_exact).lines
            if line.line_id not in existing_ids
        ]
        self.assertTrue(
            any(
                line.sku_title == exact_title and line.proposed_quantity == 5
                for line in added
            )
        )
        self.assertIsNone(after_exact.pending_dialogue)

    async def test_from_to_quantity_uses_target_value_not_source_value(self) -> None:
        sender = "adversarial-from-to-quantity"
        await self._prepare_confirmed_renewal(sender)
        message = "Change L1 from 100 to 50"
        _client, service = self._service(
            {message: _agent_intent("set_quantity", line_id="L1", quantity=100)}
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        line = next(
            item for item in self._active_scenario(session).lines if item.line_id == "L1"
        )
        self.assertEqual(line.proposed_quantity, 50)

    async def test_instead_of_rejected_product_cannot_be_selected(self) -> None:
        sender = "adversarial-instead-of-product"
        baseline = await self._prepare_confirmed_renewal(sender)
        message = "Add 10 Power BI Pro instead of Visio Plan 2"
        _client, service = self._service(
            {
                message: _agent_intent(
                    "add_sku",
                    product_query="Visio Plan 2",
                    quantity=10,
                )
            }
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        existing_ids = {line.line_id for line in baseline.lines}
        new_titles = {
            line.sku_title.casefold()
            for line in self._active_scenario(session).lines
            if line.line_id not in existing_ids
        }
        pending_query = (
            session.pending_sku_change.product_query.casefold()
            if session.pending_sku_change is not None
            else ""
        )
        self.assertNotIn("visio plan 2", new_titles)
        self.assertNotEqual(pending_query, "visio plan 2")
        self.assertTrue(
            "power bi pro" in new_titles or pending_query == "power bi pro"
        )

    async def test_negated_comment_preserves_seller_polarity(self) -> None:
        sender = "adversarial-comment-polarity"
        await self._prepare_confirmed_renewal(sender)
        message = "Add a comment that customer approval is not pending"
        _client, service = self._service(
            {
                message: _agent_intent(
                    "add_comment",
                    comment="customer approval pending",
                )
            }
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        comments = [
            " ".join(comment.casefold().strip(" .").split())
            for comment in self._active_scenario(session).comments
        ]
        self.assertNotIn("customer approval pending", comments)
        self.assertTrue(any("not pending" in comment for comment in comments))

    async def test_metadata_value_requires_a_real_seller_span(self) -> None:
        sender = "adversarial-metadata-substring"
        await self._prepare_unconfirmed_requirement(sender)
        message = "Customer name is Contoso"
        _client, service = self._service(
            {
                message: _agent_intent(
                    "set_requirement_detail",
                    detail_label="customer name",
                    detail_value="US",
                )
            }
        )

        await service._handle_text(sender, message)

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        details = {
            detail.label.casefold().replace("_", " "): detail.value
            for detail in session.estate.seller_details
        }
        self.assertEqual(details.get("customer name"), "Contoso")
        self.assertNotIn("US", details.values())

    async def test_out_of_domain_model_response_is_never_relayed(self) -> None:
        sentinel = "MODEL_SENTINEL: Virat Kohli has scored many international runs."
        for action in ("answer_question", "out_of_scope"):
            with self.subTest(action=action):
                sender = f"adversarial-ood-response-{action}"
                message = "Who is Virat Kohli?"
                client, service = self._service(
                    {message: _agent_intent(action, response_text=sentinel)}
                )

                await service._handle_text(sender, message)

                response = "\n".join(self._text_bodies(client))
                self.assertNotIn("MODEL_SENTINEL", response)
                self.assertIn("licens", response.casefold())
                self.assertIn("scope", response.casefold())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
