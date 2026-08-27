import unittest
from pathlib import Path

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import ParsedLicenseRow, ScenarioType, WorkflowStage
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v5.xlsx"


class RecordingWhatsAppClient:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}


class StaleValidationInteractionTests(unittest.IsolatedAsyncioTestCase):
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
        self.client = RecordingWhatsAppClient()
        self.service = WhatsAppWebhookService(
            self.client,  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                seller_allowlist=frozenset(),
                max_document_bytes=10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="simple_pricing",
            ),
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    async def _prepare_initial_validation(self, sender: str, quantity: int) -> None:
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Office 365 E1",
                    total_licenses=quantity,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=quantity,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        await self.orchestrator.request_requirement_validation(sender)

    async def _prepare_final_validation(self, sender: str, quantity: int) -> None:
        await self._prepare_initial_validation(sender, quantity)
        await self.orchestrator.confirm_requirement(sender)
        scenario = await self.orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=False,
        )
        await self.orchestrator.save_confirmed_as_is(sender, scenario)
        await self.orchestrator.request_finalization(sender)

    async def test_old_initial_approval_cannot_confirm_a_later_requirement(self) -> None:
        sender = "stale-initial-approval-seller"
        await self._prepare_initial_validation(sender, 10)
        await self.orchestrator.reset_session(sender)
        await self._prepare_initial_validation(sender, 45)
        before = await self.orchestrator.get_session(sender)
        assert before is not None

        await self.service._handle_interactive(
            sender,
            "licensing|validate_initial|confirm",
        )

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertEqual(after, before)
        self.assertEqual(after.stage, WorkflowStage.AWAITING_INITIAL_VALIDATION)
        self.assertIsNone(after.confirmed_as_is)
        response = self.client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("earlier proposal", response)
        self.assertIn("Nothing was confirmed or finalized", response)

    async def test_old_final_approval_cannot_finalize_a_later_proposal(self) -> None:
        sender = "stale-final-approval-seller"
        await self._prepare_final_validation(sender, 10)
        await self.orchestrator.reset_session(sender)
        await self._prepare_final_validation(sender, 45)
        before = await self.orchestrator.get_session(sender)
        assert before is not None

        await self.service._handle_interactive(
            sender,
            "licensing|validate_final|confirm",
        )

        after = await self.orchestrator.get_session(sender)
        assert after is not None
        self.assertEqual(after, before)
        self.assertEqual(after.stage, WorkflowStage.AWAITING_FINAL_VALIDATION)
        self.assertNotEqual(
            after.scenarios[after.active_scenario].status.value,  # type: ignore[index]
            "final",
        )
        response = self.client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("earlier proposal", response)
        self.assertIn("Nothing was confirmed or finalized", response)


if __name__ == "__main__":
    unittest.main()
