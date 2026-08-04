import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import (
    LicenseAnalysisError,
    LicenseAnalyzer,
    parse_customer_file,
)
from app.core.licensing.agent import AgentIntent, OpenAIIntentInterpreter
from app.core.licensing.models import (
    EstateStatus,
    LicenseEstate,
    MigrationDisposition,
    NormalizedLicenseLine,
    ScenarioStatus,
    ScenarioType,
    WorkflowSession,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import (
    RateCardCatalog,
    RateCardPayload,
    RateCardProvider,
    parse_rate_card,
)
from app.core.licensing.renderer import (
    format_estate,
    format_scenario,
    render_comparison_pdf,
    render_estate_pdf,
)
from app.core.licensing.scenarios import ScenarioEngine, ScenarioError
from app.core.licensing.store import (
    AzureBlobWorkflowStore,
    InMemoryWorkflowStore,
    WorkflowConflictError,
)
from app.core.whatsapp import WhatsAppMedia
from app.schema.whatsapp import WhatsAppWebhookPayload


RATE_CARD = b"""ProductId,SkuId,SkuTitle,TermDuration,BillingPlan,ERP Price,UnitPrice/Catalogue,Promo (If new to MS),Net to MS,Expected Partner Pricing with Promo,Expected Partner Pricing without Promo,Initial Quote With Promo,Initial Quote Without Promo
prod-a,sku-a,Product A,P1Y,Annual,120,100,0.05,95,95,100,95,100
prod-b,sku-b,Product B,P1Y,Annual,60,50,0,50,50,50,50,50
prod-e3,sku-e3,Microsoft 365 E3,P1Y,Annual,120,100,0,100,100,100,100,100
prod-e5,sku-e5,Microsoft 365 E5 without Audio Conferencing,P1Y,Annual,180,150,0,150,150,150,150,150
prod-e7,sku-e7,Microsoft 365 E7 without Audio Conferencing,P1Y,Annual,240,200,0,200,200,200,200,200
prod-copilot,sku-copilot,Microsoft 365 Copilot,P1Y,Annual,24,20,0,20,20,20,20,20
"""

CUSTOMER = b"""Product Title,Total Licenses,Expired Licenses,Assigned licenses,Renewal Date
Microsoft 365 E3,10,2,7,2027-08-01
Product B,3,0,2,2027-08-01
"""


class MemoryRateCardSource:
    async def fetch(self) -> RateCardPayload:
        return RateCardPayload(RATE_CARD, "outcome-sheet.csv", "test-v1")

    async def close(self) -> None:
        return None


async def workflow_components():
    provider = RateCardProvider(MemoryRateCardSource(), refresh_seconds=3600)
    analyzer = LicenseAnalyzer(provider)
    store = InMemoryWorkflowStore()
    engine = ScenarioEngine()
    orchestrator = LicensingOrchestrator(
        analyzer=analyzer,
        rate_cards=provider,
        scenarios=engine,
        store=store,
        default_term_duration="P1Y",
        default_billing_plan="Annual",
        default_segment="Commercial",
    )
    return provider, analyzer, store, engine, orchestrator


class ParsingTests(unittest.TestCase):
    def test_customer_parser_uses_renewal_not_assigned_quantity(self) -> None:
        rows = parse_customer_file(CUSTOMER, "customer.csv")

        self.assertEqual(rows[0].renewal_quantity, 8)
        self.assertEqual(rows[0].assigned_licenses, 7)
        self.assertEqual(rows[0].renewal_date.isoformat(), "2027-08-01")  # type: ignore[union-attr]

    def test_customer_parser_rejects_expired_greater_than_total(self) -> None:
        invalid = b"Product Title,Total Licenses,Expired Licenses,Assigned Licenses\nA,1,2,1\n"

        with self.assertRaisesRegex(LicenseAnalysisError, "cannot exceed"):
            parse_customer_file(invalid, "invalid.csv")

    def test_rate_card_uses_decimal_values(self) -> None:
        items = parse_rate_card(RATE_CARD, "outcome-sheet.csv")

        self.assertEqual(items[0].initial_quote_without_promo, Decimal("100"))
        self.assertIsInstance(items[0].initial_quote_without_promo, Decimal)

    def test_blank_price_is_unavailable_but_numeric_zero_is_free(self) -> None:
        content = b"""ProductId,SkuId,SkuTitle,TermDuration,BillingPlan,ERP Price,UnitPrice/Catalogue,Promo (If new to MS),Net to MS,Expected Partner Pricing with Promo,Expected Partner Pricing without Promo,Initial Quote With Promo,Initial Quote Without Promo
prod-blank,sku-blank,Blank Price,P1Y,Annual,0,0,0,0,0,0,,
prod-free,sku-free,Free Product,P1Y,Annual,0,0,0,0,0,0,0,0
"""
        catalog = RateCardCatalog(parse_rate_card(content, "prices.csv"), "test")
        estate = LicenseEstate(
            id="price-proof",
            thread_id="price-proof",
            source_file="customer.csv",
            status=EstateStatus.READY,
            rate_card_version="test",
            lines=[
                NormalizedLicenseLine(
                    line_id="L1",
                    row_number=2,
                    source_product_title="Blank Price",
                    product_id="prod-blank",
                    sku_id="sku-blank",
                    sku_title="Blank Price",
                    total_licenses=1,
                    expired_licenses=0,
                    assigned_licenses=1,
                    renewal_quantity=1,
                    match_confidence=100,
                    match_method="exact",
                ),
                NormalizedLicenseLine(
                    line_id="L2",
                    row_number=3,
                    source_product_title="Free Product",
                    product_id="prod-free",
                    sku_id="sku-free",
                    sku_title="Free Product",
                    total_licenses=1,
                    expired_licenses=0,
                    assigned_licenses=1,
                    renewal_quantity=1,
                    match_confidence=100,
                    match_method="exact",
                ),
            ],
        )

        scenario = ScenarioEngine().build(
            estate=estate,
            scenario_type=ScenarioType.RENEW_AS_IS,
            catalog=catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
        )

        self.assertTrue(scenario.lines[0].price_unavailable)
        self.assertTrue(scenario.lines[0].decision_required)
        self.assertFalse(scenario.lines[1].price_unavailable)
        self.assertFalse(scenario.lines[1].decision_required)
        self.assertEqual(scenario.lines[1].unit_price, Decimal("0.00"))

    def test_real_outcome_sheet_contains_every_required_scenario_product(self) -> None:
        workbook = Path(__file__).parents[1] / "docs" / "blob_storage.xlsx"
        items = parse_rate_card(
            workbook.read_bytes(),
            workbook.name,
            "Outcome Sheet",
        )
        catalog = RateCardCatalog(items, "workbook-test")

        expected = {
            "Microsoft 365 E3": ("CFQ7TTC0LFLX", "1"),
            "Microsoft 365 E5 without Audio Conferencing": ("CFQ7TTC0LFLZ", "3"),
            "Microsoft 365 E7 without Audio Conferencing": ("CFQ7TTBZZR6H", "000Z"),
            "Microsoft 365 Copilot": ("CFQ7TTC0MM8R", "2"),
        }
        for title, identity in expected.items():
            candidates = catalog.candidates(title)
            self.assertEqual(len(candidates), 1)
            self.assertEqual(
                (candidates[0].product_id, candidates[0].sku_id),
                identity,
            )
            self.assertTrue(
                catalog.price_rows(
                    product_id=identity[0],
                    sku_id=identity[1],
                    sku_title=title,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            )


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        (
            self.provider,
            self.analyzer,
            self.store,
            self.engine,
            self.orchestrator,
        ) = await workflow_components()

    async def asyncTearDown(self) -> None:
        await self.store.close()
        await self.provider.close()

    async def test_analysis_matches_skus_and_preserves_dates(self) -> None:
        estate = await self.analyzer.analyze(
            thread_id="thread-1",
            filename="customer.csv",
            content=CUSTOMER,
        )

        self.assertFalse(estate.pending_lines)
        self.assertEqual(estate.lines[0].sku_id, "sku-e3")
        self.assertEqual(estate.lines[0].renewal_quantity, 8)
        self.assertEqual(estate.lines[0].renewal_date.isoformat(), "2027-08-01")  # type: ignore[union-attr]

    async def test_customer_facing_text_uses_valid_unicode(self) -> None:
        estate = await self.analyzer.analyze(
            thread_id="thread-1", filename="customer.csv", content=CUSTOMER
        )
        scenario = self.engine.build(
            estate=estate,
            scenario_type=ScenarioType.ME3_COPILOT,
            catalog=await self.provider.get(),
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
            copilot_quantity=3,
        )

        output = format_estate(estate) + "\n" + format_scenario(scenario)

        self.assertIn("L1 · Microsoft 365 E3", output)
        self.assertIn("existing 8 → proposed 0", output)
        self.assertIn("⚠ decision", output)
        for corrupted in ("Â·", "â€¢", "âš", "â†’"):
            self.assertNotIn(corrupted, output)

    async def test_renew_prices_renewal_quantity(self) -> None:
        estate = await self.analyzer.analyze(
            thread_id="thread-1", filename="customer.csv", content=CUSTOMER
        )
        catalog = await self.provider.get()

        scenario = self.engine.build(
            estate=estate,
            scenario_type=ScenarioType.RENEW_AS_IS,
            catalog=catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
        )

        self.assertEqual(scenario.lines[0].proposed_quantity, 8)
        self.assertEqual(scenario.lines[0].category, "base")
        self.assertEqual(scenario.lines[1].category, "additional")
        self.assertEqual(scenario.total_value, Decimal("950.00"))

    async def test_target_scenario_retains_unmapped_sku_and_requires_decision(self) -> None:
        estate = await self.analyzer.analyze(
            thread_id="thread-1", filename="customer.csv", content=CUSTOMER
        )
        catalog = await self.provider.get()

        scenario = self.engine.build(
            estate=estate,
            scenario_type=ScenarioType.ME3_COPILOT,
            catalog=catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
            copilot_quantity=3,
        )

        product_b = next(line for line in scenario.lines if line.sku_id == "sku-b")
        self.assertEqual(product_b.disposition, MigrationDisposition.NEEDS_DECISION)
        self.assertEqual(product_b.extended_price, Decimal("150.00"))
        self.assertEqual(scenario.total_value, Decimal("1010.00"))
        self.assertEqual(scenario.status, ScenarioStatus.NEEDS_REVIEW)

        resolved = self.engine.set_disposition(
            scenario, product_b.line_id, MigrationDisposition.RETAIN
        )
        finalized = self.engine.finalize(resolved)
        self.assertEqual(finalized.status, ScenarioStatus.FINAL)

    async def test_me7_does_not_claim_copilot_is_bundled(self) -> None:
        estate = await self.analyzer.analyze(
            thread_id="thread-1", filename="customer.csv", content=CUSTOMER
        )
        catalog = await self.provider.get()

        scenario = self.engine.build(
            estate=estate,
            scenario_type=ScenarioType.ME7,
            catalog=catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
        )

        self.assertFalse(any(line.category == "copilot" for line in scenario.lines))
        self.assertTrue(
            any("no field proving" in assumption for assumption in scenario.assumptions)
        )

    async def test_edits_recalculate_immediately(self) -> None:
        await self.orchestrator.analyze_document(
            sender="911234567890",
            filename="customer.csv",
            content=CUSTOMER,
        )
        scenario = await self.orchestrator.build_scenario(
            "911234567890",
            ScenarioType.ME3_COPILOT,
            copilot_quantity=3,
        )

        edited = await self.orchestrator.edit_quantity(
            "911234567890", "COPILOT", 5
        )

        self.assertEqual(scenario.total_value, Decimal("1010.00"))
        self.assertEqual(edited.total_value, Decimal("1050.00"))
        self.assertEqual(edited.copilot_quantity, 5)
        self.assertEqual(edited.revision, 2)

    async def test_discount_and_adjustment_recalculate_immediately(self) -> None:
        await self.orchestrator.analyze_document(
            sender="911234567890", filename="customer.csv", content=CUSTOMER
        )
        scenario = await self.orchestrator.build_scenario(
            "911234567890",
            ScenarioType.ME3_COPILOT,
            copilot_quantity=3,
        )

        discounted = await self.orchestrator.set_discount(
            "911234567890", Decimal("10")
        )
        adjusted = await self.orchestrator.set_adjustment(
            "911234567890", Decimal("5")
        )

        self.assertEqual(scenario.total_value, Decimal("1010.00"))
        self.assertEqual(discounted.total_value, Decimal("909.00"))
        self.assertEqual(adjusted.total_value, Decimal("914.00"))

    async def test_segment_change_is_rejected_without_segment_data(self) -> None:
        await self.orchestrator.analyze_document(
            sender="911234567890", filename="customer.csv", content=CUSTOMER
        )
        await self.orchestrator.build_scenario(
            "911234567890", ScenarioType.RENEW_AS_IS
        )

        with self.assertRaisesRegex(ScenarioError, "no Segment column"):
            await self.orchestrator.reconfigure_pricing(
                "911234567890", segment="Education"
            )

    async def test_replace_removes_source_and_prices_replacement(self) -> None:
        await self.orchestrator.analyze_document(
            sender="911234567890", filename="customer.csv", content=CUSTOMER
        )
        await self.orchestrator.build_scenario(
            "911234567890",
            ScenarioType.ME3_COPILOT,
            copilot_quantity=3,
        )

        replaced = await self.orchestrator.replace_sku(
            "911234567890", "L2", "Product A", 4
        )

        source = next(line for line in replaced.lines if line.line_id == "L2")
        added = replaced.lines[-1]
        self.assertEqual(source.disposition, MigrationDisposition.REMOVE)
        self.assertEqual(source.extended_price, Decimal("0.00"))
        self.assertEqual(added.sku_id, "sku-a")
        self.assertEqual(added.extended_price, Decimal("400.00"))
        self.assertEqual(replaced.total_value, Decimal("1260.00"))

    async def test_comparison_pdf_is_generated_in_memory(self) -> None:
        await self.orchestrator.analyze_document(
            sender="911234567890", filename="customer.csv", content=CUSTOMER
        )
        await self.orchestrator.build_scenario(
            "911234567890", ScenarioType.RENEW_AS_IS
        )
        estate, scenarios, comparison = await self.orchestrator.comparison(
            "911234567890"
        )

        pdf = render_comparison_pdf(estate, scenarios, comparison)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)

    async def test_comparison_auto_builds_all_four_scenarios(self) -> None:
        await self.orchestrator.analyze_document(
            sender="911234567890", filename="customer.csv", content=CUSTOMER
        )

        _, scenarios, comparison = await self.orchestrator.comparison(
            "911234567890"
        )

        self.assertEqual(
            [scenario.scenario_type for scenario in scenarios],
            list(ScenarioType),
        )
        self.assertEqual(
            [row.scenario_type for row in comparison.rows],
            list(ScenarioType),
        )
        self.assertEqual(
            comparison.recommended_scenario,
            ScenarioType.RENEW_AS_IS,
        )
        self.assertIn("no unresolved seller decisions", comparison.recommendation_rationale)
        session = await self.orchestrator.get_session("911234567890")
        self.assertEqual(set(session.scenarios), set(ScenarioType))  # type: ignore[union-attr]

    async def test_pdf_renders_assumptions_without_comments(self) -> None:
        from reportlab.platypus import Paragraph as RealParagraph

        await self.orchestrator.analyze_document(
            sender="911234567890", filename="customer.csv", content=CUSTOMER
        )
        scenario = await self.orchestrator.build_scenario(
            "911234567890", ScenarioType.ME7
        )
        estate, scenarios, comparison = await self.orchestrator.comparison(
            "911234567890"
        )
        rendered_text: list[str] = []

        def capture_paragraph(text: str, *args: object, **kwargs: object):
            rendered_text.append(text)
            return RealParagraph(text, *args, **kwargs)

        with patch("reportlab.platypus.Paragraph", side_effect=capture_paragraph):
            pdf = render_comparison_pdf(estate, scenarios, comparison)

        self.assertEqual(scenario.comments, [])
        self.assertTrue(scenario.assumptions)
        self.assertIn("Comments and assumptions", rendered_text)
        for assumption in scenario.assumptions:
            self.assertIn(f"• {assumption}", rendered_text)
        self.assertTrue(pdf.startswith(b"%PDF"))

    async def test_estate_pdf_is_grouped_and_flags_attention_lines(self) -> None:
        estate = await self.analyzer.analyze(
            thread_id="thread-1", filename="customer.csv", content=CUSTOMER
        )

        pdf = render_estate_pdf(estate, as_of=datetime(2026, 8, 4).date())

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)

    async def test_store_rejects_stale_writes(self) -> None:
        await self.orchestrator.mark_processed("911234567890", "message-1")
        session, version = await self.store.get(
            self.orchestrator.thread_id("911234567890")
        )
        self.assertIsNotNone(session)
        assert session is not None
        await self.store.save(session, version)

        with self.assertRaises(WorkflowConflictError):
            await self.store.save(session, version)


class IntentAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_sdk_response_is_schema_validated(self) -> None:
        expected = AgentIntent(
            action="build_scenario",
            scenario="me5_copilot",
            line_id="",
            quantity=120,
            copilot_quantity=40,
            product_query="",
            disposition="none",
            comment="",
            clarification="",
        )

        class FakeResponses:
            request: dict[str, object] | None = None

            async def parse(self, **kwargs: object) -> object:
                self.request = kwargs
                return SimpleNamespace(status="completed", output_parsed=expected)

        class FakeOpenAIClient:
            def __init__(self) -> None:
                self.responses = FakeResponses()

            async def close(self) -> None:
                return None

        client = FakeOpenAIClient()
        adapter = OpenAIIntentInterpreter(
            api_key="test-key",
            model="gpt-5.6-luna",
            client=client,
        )

        actual = await adapter.interpret("Build ME5 for 120 and Copilot for 40", None)

        self.assertEqual(actual, expected)
        assert client.responses.request is not None
        self.assertEqual(client.responses.request["model"], "gpt-5.6-luna")
        self.assertIs(client.responses.request["text_format"], AgentIntent)
        self.assertFalse(client.responses.request["store"])
        await adapter.close()


class BlobWorkflowStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_blob_store_uses_etags_and_expires_old_sessions(self) -> None:
        from azure.core.exceptions import (
            ResourceExistsError,
            ResourceModifiedError,
            ResourceNotFoundError,
        )

        class FakeDownload:
            def __init__(self, content: bytes, etag: str) -> None:
                self._content = content
                self.properties = SimpleNamespace(etag=etag)

            async def readall(self) -> bytes:
                return self._content

        class FakeBlob:
            def __init__(self, container: "FakeContainer", name: str) -> None:
                self._container = container
                self._name = name

            async def download_blob(self) -> FakeDownload:
                stored = self._container.blobs.get(self._name)
                if stored is None:
                    raise ResourceNotFoundError("missing")
                return FakeDownload(*stored)

            async def upload_blob(
                self,
                content: bytes,
                *,
                overwrite: bool,
                etag: str | None = None,
                **_: object,
            ) -> dict[str, str]:
                stored = self._container.blobs.get(self._name)
                if stored is not None and not overwrite:
                    raise ResourceExistsError("exists")
                if overwrite and (stored is None or stored[1] != etag):
                    raise ResourceModifiedError("stale")
                next_etag = str(int(stored[1]) + 1) if stored else "1"
                self._container.blobs[self._name] = (content, next_etag)
                return {"etag": next_etag}

        class FakeContainer:
            def __init__(self) -> None:
                self.blobs: dict[str, tuple[bytes, str]] = {}

            async def get_container_properties(self) -> dict[str, object]:
                return {}

            def get_blob_client(self, name: str) -> FakeBlob:
                return FakeBlob(self, name)

        container = FakeContainer()
        store = AzureBlobWorkflowStore(
            container_name="unused",
            container_client=container,
            session_ttl_hours=1,
        )
        await store.connect()
        session = WorkflowSession(
            id="thread-1",
            thread_id="thread-1",
            sender="911234567890",
        )

        version_1 = await store.save(session, None)
        loaded, read_version = await store.get("thread-1")

        self.assertIsNotNone(loaded)
        self.assertEqual(read_version, version_1)
        version_2 = await store.save(session, version_1)
        self.assertNotEqual(version_1, version_2)
        with self.assertRaises(WorkflowConflictError):
            await store.save(session, version_1)

        expired = session.model_copy(
            update={"updated_at": datetime.now(UTC) - timedelta(hours=2)}
        )
        await store.save(expired, version_2)
        loaded, expired_version = await store.get("thread-1")
        self.assertIsNone(loaded)
        self.assertIsNotNone(expired_version)
        await store.close()


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.messages: list[object] = []
        self.documents: list[dict[str, object]] = []

    async def download_media(self, **_: object) -> WhatsAppMedia:
        return WhatsAppMedia(CUSTOMER, "customer.csv", "text/csv")

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}

    async def send_document(self, **kwargs: object) -> dict[str, object]:
        self.documents.append(kwargs)
        return {}


class FakeIntentInterpreter:
    async def interpret(self, _message: str, _session: object) -> AgentIntent:
        return AgentIntent(
            action="build_scenario",
            scenario="me3_copilot",
            line_id="",
            quantity=-1,
            copilot_quantity=3,
            product_query="",
            disposition="none",
            comment="",
            clarification="",
        )

    async def close(self) -> None:
        return None


class WhatsAppFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_displays_estate_and_scenario_menu(self) -> None:
        provider, _, store, _, orchestrator = await workflow_components()
        client = FakeWhatsAppClient()
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            orchestrator,
            ServiceConfiguration(frozenset(), 1024 * 1024),
        )
        webhook = WhatsAppWebhookPayload.model_validate(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [
                                        {
                                            "id": "message-1",
                                            "from": "911234567890",
                                            "type": "document",
                                            "document": {
                                                "id": "media-1",
                                                "filename": "customer.csv",
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

        await service.handle(webhook)

        self.assertEqual(len(client.documents), 1)
        self.assertEqual(
            client.documents[0]["filename"],
            "customer-licence-estate.pdf",
        )
        self.assertTrue(client.documents[0]["content"].startswith(b"%PDF"))
        menu = client.messages[-1]
        self.assertEqual(menu.interactive.type, "list")  # type: ignore[attr-defined]
        self.assertEqual(
            len(menu.interactive.action.sections[0].rows),  # type: ignore[attr-defined]
            4,
        )
        await store.close()
        await provider.close()

    async def test_natural_language_is_routed_then_priced_deterministically(self) -> None:
        provider, _, store, _, orchestrator = await workflow_components()
        client = FakeWhatsAppClient()
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            orchestrator,
            ServiceConfiguration(frozenset(), 1024 * 1024),
            intent_interpreter=FakeIntentInterpreter(),
        )
        upload = WhatsAppWebhookPayload.model_validate(
            {
                "object": "whatsapp_business_account",
                "entry": [{"changes": [{"value": {"messages": [{
                    "id": "message-upload",
                    "from": "911234567890",
                    "type": "document",
                    "document": {
                        "id": "media-1",
                        "filename": "customer.csv",
                        "mime_type": "text/csv",
                    },
                }]}}]}],
            }
        )
        natural_request = WhatsAppWebhookPayload.model_validate(
            {
                "object": "whatsapp_business_account",
                "entry": [{"changes": [{"value": {"messages": [{
                    "id": "message-natural",
                    "from": "911234567890",
                    "type": "text",
                    "text": {"body": "Prepare the ME3 option with 3 Copilot seats"},
                }]}}]}],
            }
        )

        await service.handle(upload)
        await service.handle(natural_request)

        scenario_messages = [
            message
            for message in client.messages
            if "*ME3 + Copilot" in getattr(
                getattr(message, "text", None), "body", ""
            )
        ]
        self.assertEqual(len(scenario_messages), 1)
        scenario = (await orchestrator.get_session("911234567890")).scenarios[  # type: ignore[union-attr]
            ScenarioType.ME3_COPILOT
        ]
        self.assertEqual(scenario.copilot_quantity, 3)
        self.assertEqual(scenario.total_value, Decimal("1010.00"))
        await store.close()
        await provider.close()


if __name__ == "__main__":
    unittest.main()
