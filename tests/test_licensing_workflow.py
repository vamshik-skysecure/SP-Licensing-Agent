import io
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import (
    LicenseAnalysisError,
    LicenseAnalyzer,
    MAX_CUSTOMER_FILE_ROWS,
    parse_customer_file,
)
from app.core.licensing.agent import (
    AgentIntent,
    IntentInterpretationError,
    OfficialProductAnswer,
    OfficialRecommendation,
    OpenAIIntentInterpreter,
    OpenAIMicrosoftRecommendationAdvisor,
)
from app.core.licensing.migration_rules import (
    MigrationSeedCatalog,
    MigrationSeedDocument,
)
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
    RateCardError,
    RateCardCatalog,
    RateCardPayload,
    RateCardProvider,
    normalize_product_title,
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
    def test_whatsapp_chunking_preserves_table_code_blocks(self) -> None:
        body = "```\n" + "\n".join(f"L{index:04d} product row" for index in range(500)) + "\n```"

        chunks = WhatsAppWebhookService._text_chunks(body, limit=500)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 500 for chunk in chunks))
        self.assertTrue(all(chunk.count("```") % 2 == 0 for chunk in chunks))

    def test_real_migration_seed_sources_counts_and_workbook_patterns(self) -> None:
        root = Path(__file__).parents[1]
        seeds = MigrationSeedCatalog.load(root / "config" / "migration_seed.json")
        items = parse_rate_card(
            (root / "tests" / "fixtures" / "legacy_outcome_sheet.xlsx").read_bytes(),
            "legacy_outcome_sheet.xlsx",
            "Outcome Sheet",
        )
        titles = [normalize_product_title(item.sku_title) for item in items]

        self.assertEqual(len(seeds.rules), 30)
        self.assertEqual(seeds.document.sourced_row_count, 10)
        self.assertEqual(seeds.document.unsourced_row_count, 20)
        self.assertEqual(
            sum(rule.source != "heuristic_unverified" for rule in seeds.rules),
            10,
        )
        for rule in seeds.rules:
            self.assertFalse(rule.approved)
            if rule.source == "heuristic_unverified":
                self.assertIsNone(rule.source_url)
                self.assertIsNone(rule.verified_date)
            else:
                self.assertIsNotNone(rule.source_url)
                self.assertEqual(rule.verified_date.isoformat(), "2026-08-04")  # type: ignore[union-attr]
            pattern = normalize_product_title(rule.title_pattern)
            self.assertTrue(
                any(pattern in title for title in titles),
                msg=f"Seed pattern did not match the Outcome Sheet: {rule.title_pattern}",
            )
        self.assertEqual(
            {
                rule.id
                for rule in seeds.rules
                if rule.source == "third_party_sourced"
            },
            {"power-bi-family", "teams-phone-family", "audio-conferencing-family"},
        )
        by_id = {rule.id: rule for rule in seeds.rules}
        expected = {
            "copilot-family": ("retain", "retain", "included"),
            "enterprise-mobility-security": ("included", "included", "included"),
            "power-bi-family": ("retain", "included", "included"),
            "teams-phone-family": ("retain", "included", "included"),
            "audio-conferencing-family": ("retain", "included", "included"),
            "windows-11-family": ("included", "included", "included"),
            "dynamics-365-family": ("retain", "retain", "retain"),
            "entra-family": ("needs_decision", "needs_decision", "needs_decision"),
            "intune-family": ("needs_decision", "needs_decision", "needs_decision"),
            "defender-family": ("needs_decision", "needs_decision", "needs_decision"),
        }
        for rule_id, dispositions in expected.items():
            rule = by_id[rule_id]
            self.assertEqual(
                tuple(
                    rule.suggested_dispositions[scenario].value
                    for scenario in (
                        ScenarioType.ME3_COPILOT,
                        ScenarioType.ME5_COPILOT,
                        ScenarioType.ME7,
                    )
                ),
                dispositions,
            )

    def test_customer_parser_uses_renewal_not_assigned_quantity(self) -> None:
        rows = parse_customer_file(CUSTOMER, "customer.csv")

        self.assertEqual(rows[0].renewal_quantity, 8)
        self.assertEqual(rows[0].assigned_licenses, 7)
        self.assertEqual(rows[0].renewal_date.isoformat(), "2027-08-01")  # type: ignore[union-attr]

    def test_customer_parser_rejects_expired_greater_than_total(self) -> None:
        invalid = b"Product Title,Total Licenses,Expired Licenses,Assigned Licenses\nA,1,2,1\n"

        with self.assertRaisesRegex(LicenseAnalysisError, "cannot exceed"):
            parse_customer_file(invalid, "invalid.csv")

    def test_customer_parser_rejects_excessive_rows_before_materializing_all_input(self) -> None:
        content = (
            "Product Title,Total Licenses\n"
            + "\n".join("Microsoft 365 E3,1" for _ in range(MAX_CUSTOMER_FILE_ROWS + 1))
        ).encode()

        with self.assertRaisesRegex(LicenseAnalysisError, "more than 1,000 data rows"):
            parse_customer_file(content, "oversized.csv")

    def test_customer_parser_returns_safe_error_for_corrupt_workbook(self) -> None:
        with self.assertRaisesRegex(LicenseAnalysisError, "damaged or is not a valid"):
            parse_customer_file(b"not-an-xlsx", "customer.xlsx")

    def test_customer_parser_rejects_unsafe_expanded_workbook(self) -> None:
        content = io.BytesIO()
        with ZipFile(content, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"x" * 512)

        with patch(
            "app.core.licensing.analysis.MAX_CUSTOMER_XLSX_EXPANDED_BYTES",
            128,
        ):
            with self.assertRaisesRegex(LicenseAnalysisError, "safe processing limit"):
                parse_customer_file(content.getvalue(), "customer.xlsx")

    def test_rate_card_uses_decimal_values(self) -> None:
        items = parse_rate_card(RATE_CARD, "outcome-sheet.csv")

        self.assertEqual(items[0].initial_quote_without_promo, Decimal("100"))
        self.assertIsInstance(items[0].initial_quote_without_promo, Decimal)

    def test_rate_card_parser_rejects_excessive_rows(self) -> None:
        content = b"""ProductId,SkuId,SkuTitle,TermDuration,BillingPlan,Initial Quote With Promo,Initial Quote Without Promo
p1,s1,Product 1,P1Y,Annual,10,10
p2,s2,Product 2,P1Y,Annual,20,20
p3,s3,Product 3,P1Y,Annual,30,30
"""

        with patch("app.core.licensing.rate_card.MAX_RATE_CARD_ROWS", 2):
            with self.assertRaisesRegex(RateCardError, "more than 2 data rows"):
                parse_rate_card(content, "rate-card.csv")

    def test_rate_card_parser_returns_safe_error_for_corrupt_workbook(self) -> None:
        with self.assertRaisesRegex(RateCardError, "damaged or invalid"):
            parse_rate_card(b"not-an-xlsx", "rate-card.xlsx")

    def test_rate_card_parser_rejects_unsafe_expanded_workbook(self) -> None:
        content = io.BytesIO()
        with ZipFile(content, "w", compression=ZIP_DEFLATED) as archive:
            archive.writestr("xl/worksheets/sheet1.xml", b"x" * 512)

        with patch(
            "app.core.licensing.rate_card.MAX_RATE_CARD_XLSX_EXPANDED_BYTES",
            128,
        ):
            with self.assertRaisesRegex(RateCardError, "safe processing limit"):
                parse_rate_card(content.getvalue(), "rate-card.xlsx")

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
        workbook = (
            Path(__file__).parents[1]
            / "tests"
            / "fixtures"
            / "legacy_outcome_sheet.xlsx"
        )
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

    async def test_persisted_session_does_not_store_raw_whatsapp_number(self) -> None:
        sender = "919876543210"

        await self.orchestrator.remember_capture_message(sender, "Microsoft 365 E3")
        session = await self.orchestrator.get_session(sender)

        assert session is not None
        self.assertEqual(session.sender, self.orchestrator.thread_id(sender))
        self.assertNotIn(sender, session.model_dump_json())

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

        self.assertIn("*L1 — Microsoft 365 E3*", output)
        self.assertIn("*BASE — Microsoft 365 E3*", output)
        self.assertIn("L2: seller decision required", output)
        self.assertNotIn("```", output)
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

    async def test_seller_comments_are_bounded_before_session_persistence(self) -> None:
        estate = await self.analyzer.analyze(
            thread_id="thread-1", filename="customer.csv", content=CUSTOMER
        )
        scenario = self.engine.build(
            estate=estate,
            scenario_type=ScenarioType.RENEW_AS_IS,
            catalog=await self.provider.get(),
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
        )

        with self.assertRaisesRegex(ScenarioError, "1,000 characters"):
            self.engine.add_comment(scenario, "x" * 1_001)

        for index in range(20):
            scenario = self.engine.add_comment(scenario, f"Comment {index + 1}")
        with self.assertRaisesRegex(ScenarioError, "maximum of 20"):
            self.engine.add_comment(scenario, "One more")

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

    async def test_unapproved_migration_seed_is_suggestion_only(self) -> None:
        root = Path(__file__).parents[1]
        workbook = root / "tests" / "fixtures" / "legacy_outcome_sheet.xlsx"
        catalog = RateCardCatalog(
            parse_rate_card(workbook.read_bytes(), workbook.name, "Outcome Sheet"),
            "seed-test",
        )
        candidate = catalog.candidates("Advanced Data Residency")[0]
        estate = LicenseEstate(
            id="seed-estate",
            thread_id="seed-thread",
            source_file="seed.csv",
            status=EstateStatus.READY,
            rate_card_version="seed-test",
            lines=[
                NormalizedLicenseLine(
                    line_id="L1",
                    row_number=2,
                    source_product_title=candidate.sku_title,
                    product_id=candidate.product_id,
                    sku_id=candidate.sku_id,
                    sku_title=candidate.sku_title,
                    total_licenses=5,
                    expired_licenses=0,
                    assigned_licenses=5,
                    renewal_quantity=5,
                    match_confidence=100,
                    match_method="exact",
                )
            ],
        )
        seeds = MigrationSeedCatalog.load(root / "config" / "migration_seed.json")

        unapproved = ScenarioEngine(seeds).build(
            estate=estate,
            scenario_type=ScenarioType.ME3_COPILOT,
            catalog=catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
            base_quantity=5,
            copilot_quantity=0,
        )
        source = next(line for line in unapproved.lines if line.line_id == "L1")

        self.assertEqual(source.disposition, MigrationDisposition.NEEDS_DECISION)
        self.assertTrue(source.decision_required)
        self.assertIn("Suggested default only: retain", source.note or "")
        self.assertIn("approved=false", source.note or "")
        self.assertIn("No migration action was auto-applied", source.note or "")

        seed_rule = next(
            rule for rule in seeds.rules if rule.id == "advanced-data-residency"
        )
        approved_document = MigrationSeedDocument(
            version=1,
            description="Test-only approved copy",
            sourced_row_count=0,
            unsourced_row_count=1,
            rules=[seed_rule.model_copy(update={"approved": True})],
        )
        approved = ScenarioEngine(MigrationSeedCatalog(approved_document)).build(
            estate=estate,
            scenario_type=ScenarioType.ME3_COPILOT,
            catalog=catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
            base_quantity=5,
            copilot_quantity=0,
        )
        approved_source = next(line for line in approved.lines if line.line_id == "L1")

        self.assertEqual(approved_source.disposition, MigrationDisposition.RETAIN)
        self.assertFalse(approved_source.decision_required)
        self.assertIn("Approved migration seed", approved_source.note or "")

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

        result = await self.orchestrator.replace_sku(
            "911234567890", "L2", "Product A", 4
        )
        self.assertEqual(result.state, "applied")
        self.assertIsNotNone(result.scenario)
        replaced = result.scenario
        assert replaced is not None

        source = next(line for line in replaced.lines if line.line_id == "L2")
        added = replaced.lines[-1]
        self.assertEqual(source.disposition, MigrationDisposition.REMOVE)
        self.assertEqual(source.extended_price, Decimal("0.00"))
        self.assertEqual(added.sku_id, "sku-a")
        self.assertEqual(added.extended_price, Decimal("400.00"))
        self.assertEqual(replaced.total_value, Decimal("1260.00"))

    async def test_fuzzy_add_requires_confirmation_before_mutation(self) -> None:
        sender = "911234567890"
        await self.orchestrator.analyze_document(
            sender=sender, filename="customer.csv", content=CUSTOMER
        )
        original = await self.orchestrator.build_scenario(
            sender, ScenarioType.RENEW_AS_IS
        )

        result = await self.orchestrator.add_sku(sender, "Prod A", 4)
        after_request = await self.orchestrator.get_session(sender)

        self.assertEqual(result.state, "confirmation_required")
        self.assertIsNone(result.scenario)
        self.assertIsNotNone(result.confirmation)
        assert result.confirmation is not None
        self.assertEqual(result.confirmation.candidates[0].sku_title, "Product A")
        self.assertLess(result.confirmation.candidates[0].confidence, 100)
        assert after_request is not None
        unchanged = after_request.scenarios[ScenarioType.RENEW_AS_IS]
        self.assertEqual(unchanged.revision, original.revision)
        self.assertEqual(unchanged.total_value, original.total_value)
        self.assertEqual(len(unchanged.lines), len(original.lines))

        confirmed = await self.orchestrator.confirm_sku_change(sender, 1)

        self.assertEqual(confirmed.state, "applied")
        self.assertIsNotNone(confirmed.scenario)
        assert confirmed.scenario is not None
        self.assertEqual(confirmed.scenario.lines[-1].sku_id, "sku-a")
        self.assertEqual(confirmed.scenario.lines[-1].proposed_quantity, 4)
        self.assertIsNone((await self.orchestrator.get_session(sender)).pending_sku_change)  # type: ignore[union-attr]

    async def test_fuzzy_replace_does_not_remove_source_before_confirmation(self) -> None:
        sender = "911234567890"
        await self.orchestrator.analyze_document(
            sender=sender, filename="customer.csv", content=CUSTOMER
        )
        original = await self.orchestrator.build_scenario(
            sender, ScenarioType.RENEW_AS_IS
        )

        result = await self.orchestrator.replace_sku(
            sender, "L2", "Prod A", 4
        )
        session = await self.orchestrator.get_session(sender)

        self.assertEqual(result.state, "confirmation_required")
        assert session is not None
        unchanged = session.scenarios[ScenarioType.RENEW_AS_IS]
        source = next(line for line in unchanged.lines if line.line_id == "L2")
        self.assertEqual(source.disposition, MigrationDisposition.RETAIN)
        self.assertEqual(unchanged.total_value, original.total_value)

        confirmed = await self.orchestrator.confirm_sku_change(sender, 1)
        assert confirmed.scenario is not None
        source = next(
            line for line in confirmed.scenario.lines if line.line_id == "L2"
        )
        self.assertEqual(source.disposition, MigrationDisposition.REMOVE)
        self.assertEqual(confirmed.scenario.lines[-1].sku_id, "sku-a")

    async def test_comparison_pdf_is_generated_in_memory(self) -> None:
        from reportlab.platypus import Paragraph as RealParagraph
        from reportlab.platypus import Table as RealTable

        await self.orchestrator.analyze_document(
            sender="911234567890", filename="customer.csv", content=CUSTOMER
        )
        await self.orchestrator.build_scenario(
            "911234567890", ScenarioType.RENEW_AS_IS
        )
        await self.orchestrator.set_discount("911234567890", Decimal("10"))
        await self.orchestrator.set_adjustment("911234567890", Decimal("5"))
        estate, scenarios, comparison = await self.orchestrator.comparison(
            "911234567890"
        )
        rendered_text: list[str] = []
        rendered_tables: list[list[list[object]]] = []

        def capture_paragraph(text: str, *args: object, **kwargs: object):
            rendered_text.append(text)
            return RealParagraph(text, *args, **kwargs)

        def capture_table(data: list[list[object]], *args: object, **kwargs: object):
            rendered_tables.append(data)
            return RealTable(data, *args, **kwargs)

        with (
            patch("reportlab.platypus.Paragraph", side_effect=capture_paragraph),
            patch("reportlab.platypus.Table", side_effect=capture_table),
        ):
            pdf = render_comparison_pdf(estate, scenarios, comparison)

        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertGreater(len(pdf), 1000)
        table_text = "\n".join(
            str(getattr(cell, "text", cell))
            for table in rendered_tables
            for row in table
            for cell in row
        )
        for required in (
            "Licence term",
            "Billing plan",
            "Renewal / expiration",
            "Replacement / target note",
            "Subtotal",
            "Discount percentage",
            "10.00%",
            "Discount amount",
            "Adjustment amount",
            "2027-08-01",
        ):
            self.assertIn(required, table_text)
        self.assertIn("Unresolved decisions", rendered_text)

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
        self.assertIn("Proposal notes", rendered_text)
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
        self.assertNotIn("message-1", session.model_dump_json())
        self.assertTrue(
            await self.orchestrator.has_processed("911234567890", "message-1")
        )
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
            boolean_value="none",
            percentage=-1,
            amount=-1,
            term_duration="",
            billing_plan="",
            segment="",
            currency="",
            candidate_number=-1,
            match_selections=[],
            comment="",
            detail_label="",
            detail_value="",
            response_text="",
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

    async def test_official_recommendation_uses_microsoft_only_web_search(self) -> None:
        expected = OfficialRecommendation(
            recommendation=(
                "Microsoft 365 E5 is the candidate supported for review when the seller "
                "requires the documented E5 capabilities."
            ),
            clarification_question="",
            suggested_candidate_numbers=[1],
            source_urls=[
                "https://learn.microsoft.com/en-us/microsoft-365/enterprise/"
            ],
        )

        class FakeResponses:
            request: dict[str, object] | None = None

            async def parse(self, **kwargs: object) -> object:
                self.request = kwargs
                return SimpleNamespace(status="completed", output_parsed=expected)

        client = SimpleNamespace(responses=FakeResponses())
        advisor = OpenAIMicrosoftRecommendationAdvisor(
            api_key="test-key",
            model="gpt-5.6-luna",
            client=client,
        )

        actual = await advisor.advise(
            seller_request="Suggest an option with documented E5 capabilities",
            current_sku="Microsoft 365 E3",
            quantity=100,
            candidate_skus=["Microsoft 365 E5"],
        )

        self.assertEqual(actual.suggested_candidate_numbers, [1])
        request = client.responses.request
        assert request is not None
        self.assertEqual(request["tool_choice"], "required")
        self.assertFalse(request["store"])
        self.assertIs(request["text_format"], OfficialRecommendation)
        self.assertEqual(
            request["tools"][0]["filters"]["allowed_domains"],  # type: ignore[index]
            ["learn.microsoft.com", "microsoft.com"],
        )

    async def test_official_product_answer_uses_microsoft_only_web_search(self) -> None:
        expected = OfficialProductAnswer(
            answer="Microsoft Teams availability depends on the named Microsoft 365 plan.",
            clarification_question="",
            table_title="",
            table_headers=[],
            table_rows=[],
            source_urls=[
                "https://learn.microsoft.com/en-us/microsoft-365/enterprise/"
            ],
        )

        class FakeResponses:
            request: dict[str, object] | None = None

            async def parse(self, **kwargs: object) -> object:
                self.request = kwargs
                return SimpleNamespace(status="completed", output_parsed=expected)

        client = SimpleNamespace(responses=FakeResponses())
        advisor = OpenAIMicrosoftRecommendationAdvisor(
            api_key="test-key",
            model="gpt-5.6-luna",
            client=client,
        )

        actual = await advisor.answer_product_question(
            seller_question="Can I use Teams in these products?",
            product_names=["Microsoft 365 E3"],
            proposal_context="One active proposal line.",
        )

        self.assertEqual(actual, expected)
        request = client.responses.request
        assert request is not None
        self.assertEqual(request["tool_choice"], "required")
        self.assertFalse(request["store"])
        self.assertIs(request["text_format"], OfficialProductAnswer)
        self.assertEqual(
            request["tools"][0]["filters"]["allowed_domains"],  # type: ignore[index]
            ["learn.microsoft.com", "microsoft.com"],
        )

    async def test_official_recommendation_rejects_non_microsoft_sources(self) -> None:
        parsed = OfficialRecommendation(
            recommendation="Use candidate one.",
            clarification_question="",
            suggested_candidate_numbers=[1],
            source_urls=["https://example.com/unverified"],
        )

        class FakeResponses:
            async def parse(self, **_: object) -> object:
                return SimpleNamespace(status="completed", output_parsed=parsed)

        advisor = OpenAIMicrosoftRecommendationAdvisor(
            api_key="test-key",
            model="gpt-5.6-luna",
            client=SimpleNamespace(responses=FakeResponses()),
        )

        with self.assertRaises(IntentInterpretationError):
            await advisor.advise(
                seller_request="Recommend a better SKU",
                current_sku="Microsoft 365 E3",
                quantity=100,
                candidate_skus=["Microsoft 365 E5"],
            )


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
            session_ttl_minutes=5,
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
            update={"updated_at": datetime.now(UTC) - timedelta(minutes=6)}
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
        self.images: list[dict[str, object]] = []

    async def download_media(self, **_: object) -> WhatsAppMedia:
        return WhatsAppMedia(CUSTOMER, "customer.csv", "text/csv")

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}

    async def send_document(self, **kwargs: object) -> dict[str, object]:
        self.documents.append(kwargs)
        return {}

    async def send_image(self, **kwargs: object) -> dict[str, object]:
        self.images.append(kwargs)
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
            boolean_value="none",
            percentage=-1,
            amount=-1,
            term_duration="",
            billing_plan="",
            segment="",
            currency="",
            candidate_number=-1,
            match_selections=[],
            comment="",
            detail_label="",
            detail_value="",
            response_text="",
            clarification="",
        )

    async def close(self) -> None:
        return None


def agent_intent(action: str, **updates: object) -> AgentIntent:
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


class MutableIntentInterpreter:
    def __init__(self, intent: AgentIntent) -> None:
        self.intent = intent

    async def interpret(self, _message: str, _session: object) -> AgentIntent:
        return self.intent

    async def close(self) -> None:
        return None


class WhatsAppFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_fuzzy_add_prompts_whatsapp_confirmation_before_change(self) -> None:
        provider, _, store, _, orchestrator = await workflow_components()
        client = FakeWhatsAppClient()
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            orchestrator,
            ServiceConfiguration(
                frozenset(), 1024 * 1024, allow_all_sellers=True
            ),
        )
        sender = "911234567890"
        await orchestrator.analyze_document(
            sender=sender, filename="customer.csv", content=CUSTOMER
        )
        original = await orchestrator.build_scenario(
            sender, ScenarioType.RENEW_AS_IS
        )

        await service._handle_text(sender, "/add Prod A | 2")

        prompt_bodies = [
            message.text.body  # type: ignore[attr-defined]
            for message in client.messages
            if getattr(message, "text", None) is not None
        ]
        self.assertTrue(
            any("*I need to confirm the intended SKU*" in body for body in prompt_bodies)
        )
        self.assertTrue(any("No change was made" in body for body in prompt_bodies))
        confirmation_menu = client.messages[-1]
        self.assertEqual(confirmation_menu.interactive.type, "list")  # type: ignore[attr-defined]
        session = await orchestrator.get_session(sender)
        assert session is not None
        self.assertEqual(
            session.scenarios[ScenarioType.RENEW_AS_IS].revision,
            original.revision,
        )

        await service._handle_text(sender, "/confirm-sku 1")

        session = await orchestrator.get_session(sender)
        assert session is not None
        self.assertIsNone(session.pending_sku_change)
        self.assertEqual(
            session.scenarios[ScenarioType.RENEW_AS_IS].lines[-1].sku_id,
            "sku-a",
        )
        await store.close()
        await provider.close()

    async def test_upload_displays_estate_and_natural_scenario_prompt(self) -> None:
        provider, _, store, _, orchestrator = await workflow_components()
        client = FakeWhatsAppClient()
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            orchestrator,
            ServiceConfiguration(
                frozenset(), 1024 * 1024, allow_all_sellers=True
            ),
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
        prompt = client.messages[-1].text.body  # type: ignore[attr-defined]
        self.assertIn("Which annual option", prompt)
        self.assertIn("Renew As-Is, ME3, ME5, or ME7", prompt)
        self.assertFalse(
            any(
                getattr(message, "interactive", None) is not None
                for message in client.messages
            )
        )
        await store.close()
        await provider.close()

    async def test_natural_language_is_routed_then_priced_deterministically(self) -> None:
        provider, _, store, _, orchestrator = await workflow_components()
        client = FakeWhatsAppClient()
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            orchestrator,
            ServiceConfiguration(
                frozenset(), 1024 * 1024, allow_all_sellers=True
            ),
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

        scenario_images = [
            image
            for image in client.images
            if image["filename"].startswith("me3_copilot-proposal-table-")
        ]
        self.assertEqual(len(scenario_images), 1)
        self.assertTrue(scenario_images[0]["content"].startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertIn("ME3", scenario_images[0]["caption"])
        scenario = (await orchestrator.get_session("911234567890")).scenarios[  # type: ignore[union-attr]
            ScenarioType.ME3_COPILOT
        ]
        self.assertEqual(scenario.copilot_quantity, 3)
        self.assertEqual(scenario.total_value, Decimal("1010.00"))
        await store.close()
        await provider.close()

    async def test_natural_language_covers_commercial_edit_operations(self) -> None:
        provider, _, store, _, orchestrator = await workflow_components()
        client = FakeWhatsAppClient()
        interpreter = MutableIntentInterpreter(
            agent_intent("set_quantity", line_id="L2", quantity=5)
        )
        service = WhatsAppWebhookService(
            client,  # type: ignore[arg-type]
            orchestrator,
            ServiceConfiguration(
                frozenset(), 1024 * 1024, allow_all_sellers=True
            ),
            intent_interpreter=interpreter,
        )
        sender = "911234567890"
        await orchestrator.analyze_document(
            sender=sender,
            filename="customer.csv",
            content=CUSTOMER,
        )
        await orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)

        operations = [
            (agent_intent("set_quantity", line_id="L2", quantity=5), "Change L2 to five"),
            (agent_intent("set_promo", boolean_value="true"), "Customer is promo eligible"),
            (agent_intent("set_discount", percentage=5), "Apply five percent discount"),
            (agent_intent("set_adjustment", amount=-10), "Subtract ten as an adjustment"),
            (agent_intent("set_term", term_duration="P1Y"), "Keep a one-year term"),
            (agent_intent("set_billing", billing_plan="Annual"), "Use annual billing"),
            (agent_intent("set_segment", segment="Commercial"), "Commercial segment"),
            (agent_intent("set_currency", currency="INR"), "Keep the currency in INR"),
            (
                agent_intent("add_comment", comment="Customer approval pending"),
                "Add a note that customer approval is pending",
            ),
            (
                agent_intent("add_sku", product_query="Product A", quantity=2),
                "Add two Product A licences",
            ),
            (
                agent_intent(
                    "replace_sku",
                    line_id="L2",
                    product_query="Product A",
                    quantity=4,
                ),
                "Replace L2 with four Product A licences",
            ),
            (
                agent_intent(
                    "set_disposition",
                    line_id="L1",
                    disposition="remove",
                ),
                "Remove L1",
            ),
        ]
        for intent, sentence in operations:
            interpreter.intent = intent
            await service._handle_text(sender, sentence)

        session = await orchestrator.get_session(sender)
        assert session is not None
        scenario = session.scenarios[ScenarioType.RENEW_AS_IS]
        self.assertEqual(scenario.discount_percentage, Decimal("5.0"))
        self.assertEqual(scenario.adjustment_amount, Decimal("-10.00"))
        self.assertEqual(scenario.term_duration, "P1Y")
        self.assertEqual(scenario.billing_plan, "Annual")
        self.assertEqual(scenario.segment, "Commercial")
        self.assertIn("Customer approval pending", scenario.comments)
        self.assertEqual(next(line for line in scenario.lines if line.line_id == "L1").proposed_quantity, 0)
        replaced_source = next(line for line in scenario.lines if line.line_id == "L2")
        self.assertEqual(replaced_source.proposed_quantity, 0)
        replacements = [
            line
            for line in scenario.lines
            if line.source_line_id is None
            and line.sku_id == "sku-a"
            and line.proposed_quantity == 4
        ]
        self.assertEqual(len(replacements), 1)
        self.assertTrue(any(line.source_line_id is None for line in scenario.lines))

        await store.close()
        await provider.close()


if __name__ == "__main__":
    unittest.main()
