from __future__ import annotations

import unittest
from pathlib import Path

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.agent import AgentIntent
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.candidate_policy import MAX_PRESENTABLE_SKU_CANDIDATES
from app.core.licensing.models import ParsedLicenseRow
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import RateCardCatalog, parse_rate_card
from app.core.licensing.renderer import format_pending_matches, format_sku_candidate
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v6_distributor.xlsx"
SHEET = "Outcome Sheet"


class _StaticProvider:
    def __init__(self, catalog: RateCardCatalog) -> None:
        self.catalog = catalog

    async def get(self) -> RateCardCatalog:
        return self.catalog


class _WhatsAppRecorder:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}


def _intent(action: str, **updates: object) -> AgentIntent:
    values: dict[str, object] = {
        "action": action,
        "scenario": "none",
        "line_id": "",
        "quantity": -1,
        "copilot_quantity": -1,
        "product_query": "",
        "disposition": "none",
        "boolean_value": "none",
        "percentage": -1.0,
        "amount": -1.0,
        "term_duration": "",
        "billing_plan": "",
        "segment": "",
        "currency": "",
        "candidate_number": -1,
        "match_selections": [],
        "comment": "",
        "detail_label": "",
        "detail_value": "",
        "response_text": "",
        "clarification": "",
    }
    values.update(updates)
    return AgentIntent.model_validate(values)


class CandidateNarrowingGuardTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        items = parse_rate_card(WORKBOOK.read_bytes(), WORKBOOK.name, SHEET)
        cls.catalog = RateCardCatalog(items, "v6-candidate-volume-test")

    async def asyncSetUp(self) -> None:
        provider = _StaticProvider(self.catalog)
        self.store = InMemoryWorkflowStore(session_ttl_minutes=5)
        self.orchestrator = LicensingOrchestrator(
            analyzer=LicenseAnalyzer(provider),  # type: ignore[arg-type]
            rate_cards=provider,  # type: ignore[arg-type]
            scenarios=ScenarioEngine(
                apply_bundle_rules=False,
                price_basis="distributor_expected",
            ),
            store=self.store,
            default_term_duration="P1Y",
            default_billing_plan="Annual",
            default_segment="Commercial",
        )
        self.client = _WhatsAppRecorder()
        self.service = WhatsAppWebhookService(
            self.client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()

    async def _capture(self, sender: str, product: str, quantity: int = 10):
        return await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-input.xlsx",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title=product,
                    total_licenses=quantity,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=quantity,
                )
            ],
        )

    async def test_v6_broad_windows_upload_requires_narrowing_without_option_dump(
        self,
    ) -> None:
        sender = "broad-windows"
        estate = await self._capture(sender, "windows", 37)
        line = estate.pending_lines[0]

        self.assertGreater(len(line.candidates), MAX_PRESENTABLE_SKU_CANDIDATES)
        self.assertTrue(line.candidate_narrowing_required)
        rendered = format_pending_matches(estate)
        self.assertIn("too broad to choose safely", rendered)
        self.assertIn("37", str(line.renewal_quantity))
        self.assertNotIn(format_sku_candidate(line.candidates[0]), rendered)

        await self.service._send_pending_match_lists(sender, [line])
        interactive = [
            item for item in self.client.messages if getattr(item, "interactive", None)
        ]
        self.assertEqual(interactive, [])

        with self.assertRaisesRegex(ValueError, "too broad for numbered selection"):
            await self.service._confirm_requirement_candidate(
                sender,
                capture_token=estate.capture_token[:16],
                line_id="L1",
                candidate_number=1,
            )

    async def test_v6_relevant_copilot_set_shows_every_candidate(self) -> None:
        sender = "relevant-copilot"
        estate = await self._capture(sender, "Copilot", 15)
        line = estate.pending_lines[0]

        self.assertGreaterEqual(len(line.candidates), 10)
        self.assertLessEqual(len(line.candidates), 15)
        self.assertFalse(line.candidate_narrowing_required)
        rendered = format_pending_matches(estate)
        for candidate in line.candidates:
            self.assertIn(format_sku_candidate(candidate), rendered)

        await self.service._send_pending_match_lists(sender, [line])
        rows = [
            row
            for message in self.client.messages
            if getattr(message, "interactive", None) is not None
            for section in message.interactive.action.sections
            for row in section.rows
        ]
        self.assertEqual(len(rows), len(line.candidates))

    async def test_add_narrowing_retains_action_quantity_and_target_context(self) -> None:
        sender = "narrow-add-context"
        await self._capture(sender, "Microsoft 365 E3", 12)
        broad = await self.orchestrator.add_requirement_sku(sender, "teams", 51)
        assert broad.confirmation is not None
        self.assertTrue(broad.confirmation.candidate_narrowing_required)
        self.assertEqual(broad.confirmation.action, "add")
        self.assertEqual(broad.confirmation.quantity, 51)

        handled = await self.service._supersede_pending_sku_change(
            sender,
            "phone",
            broad.confirmation,
            _intent(
                "capture_requirement",
                product_query="phone",
            ),
        )

        self.assertTrue(handled)
        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.pending_sku_change is not None
        narrowed = session.pending_sku_change
        self.assertEqual(narrowed.action, "add")
        self.assertEqual(narrowed.quantity, 51)
        self.assertIsNone(narrowed.source_line_id)
        self.assertEqual(narrowed.product_query.casefold(), "teams phone")
        self.assertFalse(narrowed.candidate_narrowing_required)
        self.assertGreaterEqual(len(narrowed.candidates), 10)
        self.assertLessEqual(len(narrowed.candidates), 15)
        self.assertTrue(
            all("teams" in item.sku_title.casefold() for item in narrowed.candidates)
        )

    async def test_replace_broad_query_preserves_source_line_and_quantity(self) -> None:
        sender = "broad-replace-context"
        await self._capture(sender, "Microsoft 365 E3", 12)

        result = await self.orchestrator.replace_requirement_sku(
            sender,
            "L1",
            "premium",
            27,
        )

        assert result.confirmation is not None
        pending = result.confirmation
        self.assertTrue(pending.candidate_narrowing_required)
        self.assertEqual(pending.action, "replace")
        self.assertEqual(pending.source_line_id, "L1")
        self.assertEqual(pending.quantity, 27)
        self.assertGreater(len(pending.candidates), MAX_PRESENTABLE_SKU_CANDIDATES)


if __name__ == "__main__":
    unittest.main()
