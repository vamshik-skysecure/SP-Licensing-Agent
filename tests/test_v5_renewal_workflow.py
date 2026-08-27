import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.agent import AgentIntent
from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import ScenarioType, WorkflowStage
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import (
    LocalRateCardSource,
    RateCardCatalog,
    RateCardProvider,
    parse_rate_card,
)
from app.core.licensing.renderer import format_comparison
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore
from app.core.whatsapp import WhatsAppMedia
from app.schema.whatsapp import WhatsAppWebhookPayload


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v5.xlsx"
CUSTOMER = ROOT / "docs" / "uat" / "synthetic_enterprise_estate.csv"


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
        "percentage": -1,
        "amount": -1,
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


class FixedIntentInterpreter:
    def __init__(self, intent: AgentIntent) -> None:
        self.intent = intent

    async def interpret(self, _message: str, _session: object) -> AgentIntent:
        return self.intent

    async def close(self) -> None:
        return None


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.documents: list[dict[str, object]] = []
        self.images: list[dict[str, object]] = []

    async def download_media(self, **_: object) -> WhatsAppMedia:
        return WhatsAppMedia(
            CUSTOMER.read_bytes(),
            CUSTOMER.name,
            "text/csv",
        )

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}

    async def send_document(self, **kwargs: object) -> dict[str, object]:
        self.documents.append(kwargs)
        return {}

    async def send_image(self, **kwargs: object) -> dict[str, object]:
        self.images.append(kwargs)
        return {}


def _document_webhook(message_id: str = "upload-v5") -> WhatsAppWebhookPayload:
    return WhatsAppWebhookPayload.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": message_id,
                                        "from": "911234567890",
                                        "type": "document",
                                        "document": {
                                            "id": "media-v5",
                                            "filename": CUSTOMER.name,
                                            "mime_type": "text/csv",
                                        },
                                    }
                                ]
                            }
                        }
                    ]
                }
            ],
        }
    )


class V5PricebookTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.items = parse_rate_card(
            WORKBOOK.read_bytes(),
            WORKBOOK.name,
            "Final Output Sheet",
        )

    def test_final_output_sheet_maps_partner_best_offer_safely(self) -> None:
        self.assertEqual(len(self.items), 4030)
        self.assertEqual(len(RateCardCatalog(self.items, "v5").identities), 1302)
        self.assertEqual(sum(item.partner_best_offer == 0 for item in self.items), 254)
        self.assertEqual(
            sum(
                item.partner_best_offer == 0
                and not item.initial_quote_with_promo_available
                and not item.initial_quote_without_promo_available
                for item in self.items
            ),
            254,
        )

        e3_annual = next(
            item
            for item in self.items
            if item.sku_title == "Microsoft 365 E3"
            and item.term_duration == "P1Y"
            and item.billing_plan == "Annual"
        )
        self.assertEqual(e3_annual.partner_best_offer, Decimal("20894.074740"))
        self.assertEqual(e3_annual.initial_quote_with_promo, e3_annual.partner_best_offer)
        self.assertTrue(e3_annual.initial_quote_with_promo_available)
        self.assertFalse(e3_annual.initial_quote_without_promo_available)
        self.assertEqual(e3_annual.promo_name, "New-to-Microsoft Promotion")
        self.assertEqual(e3_annual.customer_eligibility, "New customer to Microsoft")

    def test_placeholder_zero_ids_fall_back_to_exact_title(self) -> None:
        catalog = RateCardCatalog(self.items, "v5")

        candidates = catalog.candidates(
            "Access LTSC 2024",
            product_id="0",
            sku_id="0",
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].sku_title, "Access LTSC 2024")
        self.assertEqual(candidates[0].confidence, 100)

    def test_ambiguous_e5_never_offers_an_unrelated_dynamics_sku(self) -> None:
        catalog = RateCardCatalog(self.items, "v5")

        candidates = catalog.candidates("E5 license", limit=3)

        self.assertEqual(
            [candidate.sku_title for candidate in candidates],
            [
                "Microsoft 365 E5 without Audio Conferencing",
                "Office 365 E5 without Audio Conferencing",
                "Enterprise Mobility + Security E5",
            ],
        )
        self.assertTrue(all(candidate.confidence < 100 for candidate in candidates))
        self.assertFalse(
            any("Dynamics 365" in candidate.sku_title for candidate in candidates)
        )

    def test_family_aliases_and_defender_plan_wording_resolve_safely(self) -> None:
        catalog = RateCardCatalog(self.items, "v5")

        self.assertEqual(
            [candidate.sku_title for candidate in catalog.candidates("M365 E3")],
            ["Microsoft 365 E3"],
        )
        self.assertEqual(
            [candidate.sku_title for candidate in catalog.candidates("EMS E5")],
            ["Enterprise Mobility + Security E5"],
        )
        defender = catalog.candidates(
            "Microsoft Defender for Office 365 Plan Two",
            limit=3,
        )
        self.assertEqual(
            [candidate.sku_title for candidate in defender],
            ["Microsoft Defender for Office 365 (Plan 2)"],
        )
        self.assertFalse(
            any(candidate.sku_title.startswith("Office 365 E") for candidate in defender)
        )


class RenewalOnlyWhatsAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_table_natural_discount_and_final_pdf(self) -> None:
        provider = RateCardProvider(
            LocalRateCardSource(WORKBOOK),
            sheet_name="Final Output Sheet",
            refresh_seconds=3600,
        )
        store = InMemoryWorkflowStore()
        orchestrator = LicensingOrchestrator(
            analyzer=LicenseAnalyzer(provider),
            rate_cards=provider,
            scenarios=ScenarioEngine(),
            store=store,
            default_term_duration="P1Y",
            default_billing_plan="Annual",
            default_segment="Commercial",
        )
        client = FakeWhatsAppClient()
        interpreter = FixedIntentInterpreter(
            _intent("set_discount", percentage=5)
        )
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                allow_all_sellers=True,
                workflow_mode="renewal_only",
            ),
            intent_interpreter=interpreter,
        )

        await service.handle(_document_webhook())

        self.assertGreaterEqual(len(client.images), 2)
        self.assertEqual(client.images[0]["filename"], "licence-estate-table-1.png")
        self.assertTrue(client.images[0]["content"].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            client.images[1]["filename"],
            "renew_as_is-proposal-table-1.png",
        )
        self.assertTrue(client.images[1]["content"].startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(BytesIO(client.images[0]["content"])) as estate_image:
            self.assertEqual(estate_image.format, "PNG")
            self.assertEqual(estate_image.width, 1080)
            self.assertGreater(estate_image.height, 700)
        with Image.open(BytesIO(client.images[1]["content"])) as scenario_image:
            self.assertEqual(scenario_image.format, "PNG")
            self.assertEqual(scenario_image.width, 1080)
            self.assertGreater(scenario_image.height, 900)
        self.assertIn("Licence estate table", client.images[0]["caption"])
        self.assertIn("Renew As-Is", client.images[1]["caption"])
        self.assertEqual(client.documents[0]["filename"], "customer-licence-estate.pdf")
        validation_bodies = [
            message.text.body
            for message in client.messages
            if getattr(message, "type", None) == "text"
            and getattr(message, "text", None) is not None
        ]
        self.assertTrue(
            any("attest" in body and "promotional pricing" in body for body in validation_bodies)
        )
        self.assertFalse(
            any(getattr(message, "type", None) == "interactive" for message in client.messages)
        )

        session = await orchestrator.get_session("911234567890")
        assert session is not None and session.active_scenario is not None
        self.assertEqual(session.active_scenario, ScenarioType.RENEW_AS_IS)
        initial = session.scenarios[ScenarioType.RENEW_AS_IS]
        self.assertTrue(initial.promo_eligible)
        self.assertEqual(initial.unresolved_decisions, [])
        self.assertFalse(next(line for line in initial.lines if line.line_id == "L1").price_unavailable)
        before = initial.total_value

        interpreter.intent = _intent("set_discount", percentage=5)
        with self.assertRaisesRegex(ValueError, "Seller validation is required"):
            await service._handle_text(
                "911234567890",
                "Please apply a five percent discount",
            )

        interpreter.intent = _intent("confirm_validation")
        await service._handle_text(
            "911234567890",
            "I confirm the analysis and pricing",
        )
        session = await orchestrator.get_session("911234567890")
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.REVIEWING_SCENARIO)

        interpreter.intent = _intent("set_discount", percentage=5)
        await service._handle_text("911234567890", "Please apply a five percent discount")
        session = await orchestrator.get_session("911234567890")
        assert session is not None
        discounted = session.scenarios[ScenarioType.RENEW_AS_IS]
        self.assertEqual(discounted.discount_percentage, Decimal("5.0"))
        self.assertEqual(
            discounted.total_value,
            (before * Decimal("0.95")).quantize(Decimal("0.01")),
        )

        documents_before_finalization = len(client.documents)
        await service._handle_text("911234567890", "/finalize")
        self.assertEqual(len(client.documents), documents_before_finalization)
        session = await orchestrator.get_session("911234567890")
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.AWAITING_FINAL_VALIDATION)
        final_prompts = [
            message.text.body
            for message in client.messages
            if getattr(message, "type", None) == "text"
            and getattr(message, "text", None) is not None
        ]
        self.assertTrue(
            any("Final seller validation required" in body for body in final_prompts)
        )

        await service._handle_text("911234567890", "/cancel-finalize")
        session = await orchestrator.get_session("911234567890")
        assert session is not None
        self.assertEqual(session.stage, WorkflowStage.REVIEWING_SCENARIO)
        self.assertEqual(len(client.documents), documents_before_finalization)

        await service._handle_text("911234567890", "/finalize")
        await service._handle_text("911234567890", "/confirm-finalize")
        self.assertEqual(client.documents[-1]["filename"], "licensing-renewal-proposal.pdf")
        pdf = client.documents[-1]["content"]
        self.assertIsInstance(pdf, bytes)
        self.assertTrue(pdf.startswith(b"%PDF"))  # type: ignore[union-attr]
        self.assertGreater(len(pdf), 2000)  # type: ignore[arg-type]

        await store.close()
        await provider.close()


class AnnualUpgradeComparisonTests(unittest.IsolatedAsyncioTestCase):
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
            scenarios=ScenarioEngine(apply_bundle_rules=False),
            store=self.store,
            default_term_duration="P1Y",
            default_billing_plan="Annual",
            default_segment="Commercial",
        )
        self.sender = "annual-upgrade-seller"
        await self.orchestrator.analyze_document(
            sender=self.sender,
            filename=CUSTOMER.name,
            content=CUSTOMER.read_bytes(),
        )

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    async def test_later_scenario_inherits_validated_promotion_eligibility(self) -> None:
        await self.orchestrator.build_scenario(
            self.sender,
            ScenarioType.RENEW_AS_IS,
            promo_eligible=True,
        )

        me5 = await self.orchestrator.build_scenario(
            self.sender,
            ScenarioType.ME5_COPILOT,
        )

        self.assertTrue(me5.promo_eligible)
        base = next(line for line in me5.lines if line.line_id == "BASE")
        self.assertFalse(base.price_unavailable)
        self.assertNotIn("BASE: confirm promotion eligibility before pricing.", me5.unresolved_decisions)

    async def test_comparison_preserves_independent_proposal_edits_without_bundle_inference(self) -> None:
        await self.orchestrator.request_requirement_validation(self.sender)
        await self.orchestrator.confirm_requirement(self.sender)
        await self.orchestrator.build_scenario(
            self.sender,
            ScenarioType.RENEW_AS_IS,
        )
        await self.orchestrator.reconfigure_pricing(
            self.sender,
            promo_eligible=True,
        )
        renewal = await self.orchestrator.edit_quantity(self.sender, "L1", 121)
        await self.orchestrator.save_confirmed_as_is(self.sender, renewal)

        _estate, scenarios, comparison = await self.orchestrator.comparison(
            self.sender
        )

        self.assertEqual(
            [scenario.scenario_type for scenario in scenarios],
            list(ScenarioType),
        )
        renewal_total = scenarios[0].total_value
        for scenario in scenarios:
            self.assertEqual(scenario.term_duration, "P1Y")
            self.assertEqual(scenario.billing_plan, "Annual")
        self.assertTrue(scenarios[0].promo_eligible)
        self.assertTrue(all(not scenario.promo_eligible for scenario in scenarios[1:]))
        for scenario in scenarios[1:]:
            base = next(line for line in scenario.lines if line.line_id == "BASE")
            self.assertNotEqual(base.proposed_quantity, 121)
            retained = [
                line
                for line in scenario.lines
                if line.source_line_id in {"L2", "L3", "L4", "L5"}
            ]
            self.assertEqual(len(retained), 4)
            self.assertTrue(
                all(line.disposition.value == "retain" for line in retained)
            )
            self.assertTrue(
                all(line.proposed_quantity == line.existing_quantity for line in retained)
            )
            self.assertTrue(
                all(
                    "no add-on bundle entitlement assumption" in (line.note or "")
                    for line in retained
                )
            )
        for scenario in scenarios[1:3]:
            copilot = next(line for line in scenario.lines if line.line_id == "COPILOT")
            self.assertEqual(copilot.proposed_quantity, 0)
        for row, scenario in zip(comparison.rows, scenarios, strict=True):
            self.assertEqual(
                row.difference_from_renew_as_is,
                scenario.total_value - renewal_total,
            )
        mobile_output = format_comparison(comparison)
        self.assertNotIn("```", mobile_output)
        self.assertIn("Annual total:", mobile_output)
        self.assertIn("Difference:", mobile_output)
        self.assertIn("Additional/retained licences:", mobile_output)

    async def test_upgrade_mode_rejects_nonannual_term_and_billing(self) -> None:
        service = WhatsAppWebhookService(
            FakeWhatsAppClient(),  # type: ignore[arg-type]
            self.orchestrator,
            ServiceConfiguration(
                frozenset(),
                10 * 1024 * 1024,
                workflow_mode="upgrade_comparison",
            ),
        )

        service._validate_annual_contract(term_duration="P1Y")
        service._validate_annual_contract(billing_plan="Annual")
        with self.assertRaisesRegex(ValueError, "one-year term"):
            service._validate_annual_contract(term_duration="P3Y")
        with self.assertRaisesRegex(ValueError, "annual billing"):
            service._validate_annual_contract(billing_plan="Monthly")


if __name__ == "__main__":
    unittest.main()
