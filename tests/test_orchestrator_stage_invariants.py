import unittest
from pathlib import Path

from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import (
    EstateStatus,
    MigrationDisposition,
    PendingDialogue,
    PendingSkuChange,
    ParsedLicenseRow,
    ScenarioType,
    SkuMatchCandidate,
    WorkflowStage,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine, ScenarioError
from app.core.licensing.store import InMemoryWorkflowStore


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v5.xlsx"
CUSTOMER = ROOT / "docs" / "client_upload_sheet.csv"


class OrchestratorStageInvariantTests(unittest.IsolatedAsyncioTestCase):
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

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    async def _prepare_reviewing(self, sender: str):
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)
        renew = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, renew)
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.REVIEWING_SCENARIO)
        return session

    async def _replace_session(self, sender: str, **updates: object):
        thread_id = self.orchestrator.thread_id(sender)
        session, version = await self.store.get(thread_id)
        assert session is not None and version is not None
        await self.store.save(session.model_copy(update=updates), version)
        updated = await self.orchestrator.get_session(sender)
        assert updated is not None
        return updated

    async def _inject_pending_sku_change(self, sender: str) -> None:
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.active_scenario is not None
        scenario = session.scenarios[session.active_scenario]
        candidate = (await self.provider.get()).candidates(
            "Power BI Pro",
            limit=None,
        )[0]
        pending = PendingSkuChange(
            id="stale-confirmation",
            action="add",
            scenario_type=session.active_scenario,
            scenario_revision=scenario.revision,
            product_query="Power BI Pro",
            quantity=5,
            candidates=[candidate],
        )
        await self._replace_session(sender, pending_sku_change=pending)

    def _commercial_mutations(self, sender: str):
        return (
            (
                "build scenario",
                lambda: self.orchestrator.build_scenario(
                    sender,
                    ScenarioType.ME5_COPILOT,
                ),
            ),
            (
                "edit quantity",
                lambda: self.orchestrator.edit_quantity(sender, "L1", 99),
            ),
            (
                "set disposition",
                lambda: self.orchestrator.set_disposition(
                    sender,
                    "L1",
                    MigrationDisposition.REMOVE,
                ),
            ),
            (
                "add SKU",
                lambda: self.orchestrator.add_sku(sender, "Power BI Pro", 5),
            ),
            (
                "replace SKU",
                lambda: self.orchestrator.replace_sku(
                    sender,
                    "L1",
                    "Power BI Pro",
                    5,
                ),
            ),
            (
                "recommendation",
                lambda: self.orchestrator.recommend_higher_tier(
                    sender,
                    line_id="L1",
                ),
            ),
            ("comparison", lambda: self.orchestrator.comparison(sender)),
            (
                "confirm stale SKU",
                lambda: self.orchestrator.confirm_sku_change(
                    sender,
                    1,
                    confirmation_id="stale-confirmation",
                ),
            ),
            (
                "seller detail",
                lambda: self.orchestrator.set_requirement_detail(
                    sender,
                    label="Customer",
                    value="Contoso",
                ),
            ),
        )

    async def test_finalized_proposal_rejects_every_commercial_mutation(self) -> None:
        sender = "domain-finalized-guard"
        await self._prepare_reviewing(sender)
        await self.orchestrator.request_finalization(sender)
        await self.orchestrator.confirm_finalization(sender)
        await self._inject_pending_sku_change(sender)

        for label, operation in self._commercial_mutations(sender):
            with self.subTest(operation=label):
                before = await self.orchestrator.get_session(sender)
                assert before is not None
                with self.assertRaisesRegex(ScenarioError, "proposal is finalized"):
                    await operation()
                after = await self.orchestrator.get_session(sender)
                self.assertEqual(after, before)

    async def test_final_validation_rejects_edits_until_explicitly_cancelled(self) -> None:
        sender = "domain-final-validation-guard"
        await self._prepare_reviewing(sender)
        await self.orchestrator.request_finalization(sender)
        await self._inject_pending_sku_change(sender)

        for label, operation in self._commercial_mutations(sender):
            with self.subTest(operation=label):
                before = await self.orchestrator.get_session(sender)
                assert before is not None
                with self.assertRaisesRegex(ScenarioError, "Cancel finalization"):
                    await operation()
                after = await self.orchestrator.get_session(sender)
                self.assertEqual(after, before)

        self.assertTrue(await self.orchestrator.cancel_finalization(sender))
        self.assertTrue(await self.orchestrator.cancel_sku_change(sender))
        changed = await self.orchestrator.edit_quantity(sender, "L1", 99)
        self.assertEqual(
            next(line.proposed_quantity for line in changed.lines if line.line_id == "L1"),
            99,
        )
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.REVIEWING_SCENARIO)

    async def test_finalization_rejects_each_blocking_pending_state(self) -> None:
        cases = ("dialogue", "capture", "sku", "unresolved_match")
        for case in cases:
            with self.subTest(blocker=case):
                sender = f"domain-finalization-blocker-{case}"
                session = await self._prepare_reviewing(sender)
                if case == "dialogue":
                    await self.orchestrator.set_pending_dialogue(
                        sender,
                        PendingDialogue(
                            kind="agent_clarification",
                            question="Which product should I add?",
                            operation="add_sku",
                            scope="scenario",
                        ),
                    )
                elif case == "capture":
                    await self.orchestrator.remember_capture_message(
                        sender,
                        "Microsoft 365 Copilot",
                    )
                elif case == "sku":
                    await self._inject_pending_sku_change(sender)
                else:
                    assert session.estate is not None
                    source = session.estate.lines[0]
                    pending_line = source.model_copy(
                        update={
                            "match_method": "unresolved",
                            "match_confidence": 0,
                            "candidates": [
                                SkuMatchCandidate(
                                    product_id=source.product_id or "PRODUCT",
                                    sku_id=source.sku_id or "SKU",
                                    sku_title=source.display_title,
                                    confidence=90,
                                )
                            ],
                        }
                    )
                    estate = session.estate.model_copy(
                        update={
                            "status": EstateStatus.AWAITING_MATCH_CONFIRMATION,
                            "lines": [pending_line, *session.estate.lines[1:]],
                        }
                    )
                    await self._replace_session(sender, estate=estate)

                before = await self.orchestrator.get_session(sender)
                assert before is not None
                with self.assertRaisesRegex(ScenarioError, "Resolve the pending"):
                    await self.orchestrator.request_finalization(sender)
                after = await self.orchestrator.get_session(sender)
                self.assertEqual(after, before)

    async def test_initial_confirmation_and_scenario_build_remain_supported(self) -> None:
        sender = "domain-initial-confirm-build"
        estate = await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        self.assertFalse(estate.pending_lines)
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)

        scenario = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, scenario)

        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.REVIEWING_SCENARIO)
        self.assertEqual(session.confirmed_as_is, scenario)
        self.assertEqual(
            session.scenarios[ScenarioType.RENEW_AS_IS],
            scenario,
        )

    async def test_requirement_confirmation_preserves_pending_add_or_replace(self) -> None:
        confirmation_methods = (
            ("confirm requirement", self.orchestrator.confirm_requirement),
            (
                "confirm and price atomically",
                self.orchestrator.confirm_requirement_and_price_as_is,
            ),
        )
        for action in ("add", "replace"):
            for method_name, confirmation in confirmation_methods:
                with self.subTest(action=action, confirmation=method_name):
                    sender = f"domain-pending-{action}-{method_name.replace(' ', '-')}"
                    estate = await self.orchestrator.analyze_document(
                        sender=sender,
                        filename=CUSTOMER.name,
                        content=CUSTOMER.read_bytes(),
                    )
                    self.assertFalse(estate.pending_lines)
                    await self.orchestrator.request_requirement_validation(sender)
                    if action == "add":
                        requested = await self.orchestrator.add_requirement_sku(
                            sender,
                            "Power BI",
                            10,
                        )
                    else:
                        requested = await self.orchestrator.replace_requirement_sku(
                            sender,
                            estate.lines[0].line_id,
                            "Power BI",
                            10,
                        )
                    self.assertEqual(requested.state, "confirmation_required")

                    before = await self.orchestrator.get_session(sender)
                    assert before is not None and before.pending_sku_change is not None
                    with self.assertRaisesRegex(
                        ScenarioError,
                        "Resolve the pending requirement question",
                    ):
                        await confirmation(sender)

                    after = await self.orchestrator.get_session(sender)
                    self.assertEqual(after, before)

    async def test_direct_finalize_cannot_bypass_seller_validation(self) -> None:
        sender = "domain-direct-finalize"
        before = await self._prepare_reviewing(sender)

        with self.assertRaisesRegex(ScenarioError, "requires seller validation"):
            await self.orchestrator.finalize(sender)

        after = await self.orchestrator.get_session(sender)
        self.assertEqual(after, before)

    async def test_requirement_edit_returns_to_confirmation_gate(self) -> None:
        sender = "domain-reconfirm-edited-requirement"
        estate = await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        await self.orchestrator.confirm_requirement(sender)

        edited = await self.orchestrator.edit_requirement_quantity(
            sender,
            estate.lines[0].line_id,
            estate.lines[0].renewal_quantity + 7,
        )
        session = await self.orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertFalse(session.requirement_confirmed)
        self.assertEqual(
            edited.lines[0].renewal_quantity,
            estate.lines[0].renewal_quantity + 7,
        )
        with self.assertRaisesRegex(ScenarioError, "Confirm the complete"):
            await self.orchestrator.build_scenario(
                sender,
                ScenarioType.RENEW_AS_IS,
            )

        await self.orchestrator.confirm_requirement(sender)
        scenario = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
        )
        self.assertEqual(scenario.scenario_type, ScenarioType.RENEW_AS_IS)

    async def test_final_confirmation_rejects_state_added_after_prompt(self) -> None:
        sender = "domain-final-confirm-pending-state"
        await self._prepare_reviewing(sender)
        await self.orchestrator.request_finalization(sender)
        before = await self._replace_session(
            sender,
            capture_messages=["and add 20 Power BI licences"],
        )

        with self.assertRaisesRegex(ScenarioError, "unfinished product or quantity"):
            await self.orchestrator.confirm_finalization(sender)

        after = await self.orchestrator.get_session(sender)
        self.assertEqual(after, before)

    async def test_finalized_session_rejects_new_analysis_without_reset(self) -> None:
        sender = "domain-finalized-analysis"
        await self._prepare_reviewing(sender)
        await self.orchestrator.request_finalization(sender)
        await self.orchestrator.confirm_finalization(sender)
        before = await self.orchestrator.get_session(sender)
        assert before is not None

        with self.assertRaisesRegex(ScenarioError, "explicitly start fresh"):
            await self.orchestrator.analyze_document(
                sender=sender,
                filename=CUSTOMER.name,
                content=CUSTOMER.read_bytes(),
            )

        after = await self.orchestrator.get_session(sender)
        self.assertEqual(after, before)

    async def test_attachment_cannot_discard_pending_sku_choice(self) -> None:
        sender = "domain-pending-choice-attachment"
        await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        requested = await self.orchestrator.add_requirement_sku(
            sender,
            "Power BI",
            10,
        )
        self.assertEqual(requested.state, "confirmation_required")
        before = await self.orchestrator.get_session(sender)
        assert before is not None and before.pending_sku_change is not None

        with self.assertRaisesRegex(ScenarioError, "unconfirmed SKU choice"):
            await self.orchestrator.append_document(
                sender=sender,
                filename=CUSTOMER.name,
                content=CUSTOMER.read_bytes(),
            )

        after = await self.orchestrator.get_session(sender)
        self.assertEqual(after, before)

    async def test_stale_requirement_choice_is_not_restored(self) -> None:
        sender = "domain-stale-requirement-choice"
        estate = await self.orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )
        await self.orchestrator.request_requirement_validation(sender)
        requested = await self.orchestrator.add_requirement_sku(
            sender,
            "Power BI",
            10,
        )
        assert requested.confirmation is not None
        pending = requested.confirmation
        await self.orchestrator.cancel_sku_change(sender)
        changed_line = estate.lines[0].model_copy(
            update={
                "total_licenses": estate.lines[0].total_licenses + 1,
                "renewal_quantity": estate.lines[0].renewal_quantity + 1,
            }
        )
        changed_estate = estate.model_copy(
            update={"lines": [changed_line, *estate.lines[1:]]}
        )
        await self._replace_session(sender, estate=changed_estate)

        restored = await self.orchestrator.restore_pending_sku_change(sender, pending)
        self.assertIsNone(restored.pending_sku_change)

    async def test_extracted_capture_is_consumed_only_by_successful_transition(self) -> None:
        sender = "domain-atomic-capture-consumption"
        await self.orchestrator.remember_capture_message(sender, "Power BI Pro")
        rows = [
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
        ]

        with self.assertRaisesRegex(ScenarioError, "already active"):
            await self.orchestrator.analyze_extracted(
                sender=sender,
                source_file="typed.txt",
                rows=rows,
            )
        blocked = await self.orchestrator.get_session(sender)
        assert blocked is not None
        self.assertEqual(blocked.capture_messages, ["Power BI Pro"])

        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="typed.txt",
            rows=rows,
            consume_capture_messages=True,
        )
        consumed = await self.orchestrator.get_session(sender)
        assert consumed is not None
        self.assertEqual(consumed.capture_messages, [])
        self.assertIsNotNone(consumed.estate)


if __name__ == "__main__":
    unittest.main()
