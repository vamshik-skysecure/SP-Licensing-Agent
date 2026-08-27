import unittest
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from app.api.whatsapp.service import ServiceConfiguration, WhatsAppWebhookService
from app.core.licensing.analysis import LicenseAnalysisError, LicenseAnalyzer
from app.core.licensing.models import (
    EstateStatus,
    LicenseEstate,
    NormalizedLicenseLine,
    ParsedLicenseRow,
    ScenarioType,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import RateCardCatalog, parse_rate_card
from app.core.licensing.renderer import (
    format_sku_candidate,
    product_family,
    render_estate_pdf,
)
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v6_distributor.xlsx"
SHEET = "Outcome Sheet"


class V6DistributorWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.items = parse_rate_card(WORKBOOK.read_bytes(), WORKBOOK.name, SHEET)
        cls.catalog = RateCardCatalog(cls.items, "v6-test")

    def test_v6_reads_second_row_header_and_retains_all_name_only_rows(
        self,
    ) -> None:
        self.assertEqual(len(self.items), 4030)
        name_only = [
            item for item in self.items if not item.product_id and not item.sku_id
        ]
        self.assertEqual(len(name_only), 297)
        self.assertEqual(
            sum(item.term_duration == "Perpetual" for item in name_only),
            233,
        )
        self.assertTrue(all(item.source_row_number >= 3 for item in self.items))

    def test_name_only_product_is_displayed_without_an_invented_identifier(self) -> None:
        candidates = self.catalog.candidates("Azure SQL Edge - 1 year")

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].product_id, "")
        self.assertEqual(candidates[0].sku_id, "")
        self.assertEqual(
            format_sku_candidate(candidates[0]),
            "Azure SQL Edge - 1 year",
        )

    def test_name_only_confirmation_uses_title_and_rejects_ambiguous_blank_pair(
        self,
    ) -> None:
        candidates = self.catalog.candidates("Azure SQL Edge", limit=None)
        self.assertEqual(
            [candidate.sku_title for candidate in candidates],
            ["Azure SQL Edge - 1 year", "Azure SQL Edge - 3 year"],
        )
        estate = LicenseEstate(
            id="v6-name-only-confirmation",
            thread_id="v6-name-only-confirmation",
            source_file="seller-message",
            status=EstateStatus.AWAITING_MATCH_CONFIRMATION,
            rate_card_version="v6-test",
            lines=[
                NormalizedLicenseLine(
                    line_id="L1",
                    row_number=2,
                    source_product_title="Azure SQL Edge",
                    total_licenses=2,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=2,
                    match_method="unresolved",
                    candidates=candidates,
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        analyzer = LicenseAnalyzer(_StaticProvider(self.catalog))  # type: ignore[arg-type]

        with self.assertRaisesRegex(
            LicenseAnalysisError,
            "blank Product/SKU identifiers.*more than one offered product",
        ):
            analyzer.confirm_matches(estate, {"L1": ("", "")})

        confirmed = analyzer.confirm_matches(
            estate,
            {"L1": ("", "", "Azure SQL Edge - 3 year")},
        )
        self.assertEqual(confirmed.lines[0].sku_title, "Azure SQL Edge - 3 year")
        self.assertEqual(confirmed.lines[0].match_method, "seller_confirmed")
        self.assertFalse(confirmed.lines[0].product_id)
        self.assertFalse(confirmed.lines[0].sku_id)
        pdf = render_estate_pdf(confirmed, include_migration_review=False)
        pdf_text = "\n".join(
            page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages
        )
        normalized_pdf_text = " ".join(pdf_text.split())
        self.assertIn("Name-only catalogue entry", normalized_pdf_text)
        self.assertNotIn("Needs confirmation", normalized_pdf_text)

    def test_conflicting_name_only_prices_are_not_selected_silently(self) -> None:
        item = next(row for row in self.items if row.sku_title == "Access LTSC 2024")
        estate = LicenseEstate(
            id="v6-name-only-conflict",
            thread_id="v6-name-only-conflict",
            source_file="synthetic.csv",
            status=EstateStatus.READY,
            rate_card_version="v6-test",
            lines=[
                NormalizedLicenseLine(
                    line_id="L1",
                    row_number=2,
                    source_product_title=item.sku_title,
                    product_id=None,
                    sku_id=None,
                    sku_title=item.sku_title,
                    total_licenses=1,
                    expired_licenses=0,
                    assigned_licenses=1,
                    renewal_quantity=1,
                    match_confidence=100,
                    match_method="exact",
                    term_duration="Perpetual",
                    billing_plan="OneTime",
                )
            ],
        )
        engine = ScenarioEngine(
            apply_bundle_rules=False,
            price_basis="distributor_expected",
        )

        scenario = engine.build(
            estate=estate,
            scenario_type=ScenarioType.RENEW_AS_IS,
            catalog=self.catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
        )

        self.assertTrue(scenario.lines[0].price_unavailable)
        self.assertIn("Multiple commercial prices", scenario.lines[0].note or "")

    def test_name_only_price_lookup_never_falls_back_to_another_title(self) -> None:
        self.assertEqual(
            self.catalog.price_rows(
                product_id="",
                sku_id="",
                sku_title="Azure SQL Edge - product not in V6",
                term_duration="P1Y",
                billing_plan="Annual",
                segment="Commercial",
            ),
            [],
        )

    def test_v6_maps_supplied_expected_distributor_price_column(self) -> None:
        item = next(
            row
            for row in self.items
            if row.sku_title == "Microsoft 365 E3"
            and row.term_duration == "P1Y"
            and row.billing_plan == "Annual"
        )

        self.assertEqual(item.product_id, "CFQ7TTC0LFLX")
        self.assertEqual(item.sku_id, "1")
        self.assertEqual(item.distributor_price, Decimal("28036.800000"))
        self.assertEqual(item.marketplace_price, Decimal("0.000000"))

    def test_simple_pricing_uses_distributor_price_deterministically(self) -> None:
        item = next(
            row
            for row in self.items
            if row.sku_title == "Microsoft 365 E3"
            and row.term_duration == "P1Y"
            and row.billing_plan == "Annual"
        )
        estate = LicenseEstate(
            id="v6-distributor",
            thread_id="v6-distributor",
            source_file="synthetic.csv",
            status=EstateStatus.READY,
            rate_card_version="v6-test",
            lines=[
                NormalizedLicenseLine(
                    line_id="L1",
                    row_number=2,
                    source_product_title=item.sku_title,
                    product_id=item.product_id,
                    sku_id=item.sku_id,
                    sku_title=item.sku_title,
                    total_licenses=15,
                    expired_licenses=0,
                    assigned_licenses=15,
                    renewal_quantity=15,
                    match_confidence=100,
                    match_method="exact",
                    term_duration="P1Y",
                    billing_plan="Annual",
                )
            ],
        )
        engine = ScenarioEngine(
            apply_bundle_rules=False,
            price_basis="distributor_expected",
        )

        scenario = engine.build(
            estate=estate,
            scenario_type=ScenarioType.RENEW_AS_IS,
            catalog=self.catalog,
            term_duration="P1Y",
            billing_plan="Annual",
            segment="Commercial",
        )

        self.assertEqual(scenario.lines[0].unit_price, Decimal("28036.80"))
        self.assertEqual(scenario.lines[0].extended_price, Decimal("420552.00"))
        self.assertEqual(scenario.total_value, Decimal("420552.00"))

    def test_required_enterprise_options_have_one_annual_distributor_price(self) -> None:
        required = {
            "Microsoft 365 E3",
            "Microsoft 365 E5 without Audio Conferencing",
            "Microsoft 365 E7 without Audio Conferencing",
            "Microsoft 365 Copilot",
        }
        for title in required:
            matches = [
                row
                for row in self.items
                if row.sku_title == title
                and row.term_duration == "P1Y"
                and row.billing_plan == "Annual"
            ]
            self.assertEqual(len(matches), 1, title)
            self.assertGreater(matches[0].distributor_price, 0, title)

    def test_defender_endpoint_plan_two_never_crosses_workload_or_plan(self) -> None:
        for query in (
            "Defender for endpoint plan 2",
            "Defender for endpoint plan two",
            "Defender for endpoint P2",
        ):
            with self.subTest(query=query):
                candidates = self.catalog.candidates(query, limit=None)

                self.assertEqual(len(candidates), 7)
                self.assertEqual(
                    candidates[0].sku_title,
                    "Microsoft Defender for Endpoint P2",
                )
                self.assertEqual(candidates[0].product_id, "CFQ7TTC0LGV0")
                self.assertEqual(candidates[0].sku_id, "1")
                self.assertTrue(
                    all(
                        "defender for endpoint" in item.sku_title.casefold()
                        for item in candidates
                    )
                )
                self.assertTrue(
                    all("p2" in item.sku_title.casefold() for item in candidates)
                )
                titles = "\n".join(item.sku_title for item in candidates).casefold()
                self.assertNotIn("defender for identity", titles)
                self.assertNotIn("defender for office 365", titles)
                self.assertNotIn("defender for cloud apps", titles)
                self.assertNotIn(" f2", titles)

    def test_workload_typo_contributes_before_defender_candidates_are_ranked(
        self,
    ) -> None:
        for query in (
            "defender endpont plan 2",
            "defnder for endpont plan two",
            "defender endpont p2",
        ):
            with self.subTest(query=query):
                candidates = self.catalog.candidates(query, limit=None)

                self.assertTrue(candidates)
                self.assertEqual(
                    candidates[0].sku_title,
                    "Microsoft Defender for Endpoint P2",
                )
                self.assertEqual(candidates[0].product_id, "CFQ7TTC0LGV0")
                self.assertEqual(candidates[0].sku_id, "1")
                self.assertTrue(
                    all(
                        "defender for endpoint" in candidate.sku_title.casefold()
                        for candidate in candidates
                    )
                )

    def test_typo_tolerance_is_generic_across_catalogue_families(self) -> None:
        power_bi = self.catalog.candidates(
            "powr bi premum per user",
            limit=None,
        )
        self.assertTrue(power_bi)
        self.assertEqual(power_bi[0].sku_title, "Power BI Premium Per User")
        self.assertTrue(
            all("power bi premium" in item.sku_title.casefold() for item in power_bi)
        )

        cloud_apps = self.catalog.candidates("defender clod apps", limit=None)
        self.assertGreater(len(cloud_apps), 1)
        self.assertTrue(
            all(
                "defender for cloud apps" in item.sku_title.casefold()
                for item in cloud_apps
            )
        )
        self.assertIn(
            "Microsoft Defender for Cloud Apps",
            [item.sku_title for item in cloud_apps],
        )

        ambiguous = self.catalog.candidates("defnder", limit=None)
        self.assertGreater(len(ambiguous), 1)
        self.assertTrue(
            all("defender" in candidate.sku_title.casefold() for candidate in ambiguous)
        )

    def test_typo_does_not_override_an_incompatible_workload_plan(self) -> None:
        self.assertEqual(
            self.catalog.candidates("defender identty plan 2", limit=None),
            [],
        )

    def test_exact_identifier_matching_remains_authoritative_over_typo_text(
        self,
    ) -> None:
        candidates = self.catalog.candidates(
            "defender for offce plan 2",
            product_id="CFQ7TTC0LGV0",
            sku_id="1",
            limit=None,
        )

        self.assertEqual(len(candidates), 1)
        self.assertEqual(
            candidates[0].sku_title,
            "Microsoft Defender for Endpoint P2",
        )
        self.assertEqual(candidates[0].confidence, 100)

    def test_all_word_matches_and_distinct_catalogue_identities_are_preserved(
        self,
    ) -> None:
        e5_candidates = self.catalog.candidates("E5", limit=None)
        self.assertEqual(len(e5_candidates), 21)
        self.assertEqual(
            len({(item.product_id, item.sku_id) for item in e5_candidates}),
            21,
        )
        self.assertTrue(all("e5" in item.sku_title.casefold() for item in e5_candidates))

        exact_title = self.catalog.candidates("Visio Plan 2", limit=None)
        self.assertEqual(len(exact_title), 2)
        self.assertEqual(
            {(item.product_id, item.sku_id) for item in exact_title},
            {("CFQ7TTC0HD32", "2"), ("CFQ7TTC0HD32", "4")},
        )
        self.assertTrue(
            all("Product ID:" in format_sku_candidate(item) for item in exact_title)
        )
        self.assertTrue(all("SKU ID:" in format_sku_candidate(item) for item in exact_title))

    def test_generic_catalogue_wording_asks_for_a_plan_instead_of_flooding_options(
        self,
    ) -> None:
        for query in ("Microsoft", "Microsoft licence", "Microsoft 365", "Office 365"):
            with self.subTest(query=query):
                self.assertEqual(self.catalog.candidates(query, limit=None), [])

        self.assertTrue(self.catalog.candidates("Microsoft 365 E3", limit=None))
        self.assertTrue(self.catalog.candidates("Defender Endpoint", limit=None))

    def test_me_suite_shorthand_resolves_only_to_the_microsoft_365_family(self) -> None:
        expected = {
            "ME3": "Microsoft 365 E3",
            "ME5": "Microsoft 365 E5",
            "ME7": "Microsoft 365 E7",
        }
        for query, title_prefix in expected.items():
            with self.subTest(query=query):
                candidates = self.catalog.candidates(query, limit=None)

                self.assertTrue(candidates)
                self.assertTrue(
                    all(item.sku_title.startswith(title_prefix) for item in candidates)
                )
                self.assertTrue(
                    all(item.product_id == "CFQ7TTC0LFLX" for item in candidates)
                    if query == "ME3"
                    else all(item.product_id for item in candidates)
                )

    def test_no_teams_suite_variant_stays_in_its_suite_family(self) -> None:
        self.assertEqual(product_family("Office 365 E1 (no Teams)"), "Office 365")
        self.assertEqual(
            product_family("Microsoft 365 E5 (no Teams) without Audio Conferencing"),
            "Microsoft 365",
        )


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


class V6WhatsAppMatchingTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        items = parse_rate_card(WORKBOOK.read_bytes(), WORKBOOK.name, SHEET)
        cls.catalog = RateCardCatalog(items, "v6-whatsapp-test")

    async def asyncSetUp(self) -> None:
        self.store = InMemoryWorkflowStore(session_ttl_minutes=5)
        provider = _StaticProvider(self.catalog)
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

    async def test_plan_two_words_cannot_be_interpreted_as_option_two(self) -> None:
        sender = "defender-plan-two-seller"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Defender for endpoint plan 2",
                    total_licenses=2,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=2,
                )
            ],
        )
        self.assertEqual(len(estate.pending_lines[0].candidates), 7)

        await self.service._handle_text(sender, "Defender for endpoint plan 2")

        session = await self.orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        self.assertEqual(len(session.estate.pending_lines), 1)
        self.assertIsNone(session.estate.lines[0].sku_title)
        self.assertTrue(
            all(
                "defender for endpoint" in item.sku_title.casefold()
                for item in session.estate.pending_lines[0].candidates
            )
        )

    async def test_name_only_candidate_add_replace_and_pricing_keep_selected_title(
        self,
    ) -> None:
        sender = "v6-name-only-edit-seller"
        await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Microsoft 365 E3",
                    total_licenses=1,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=1,
                )
            ],
        )

        requested_add = await self.orchestrator.add_requirement_sku(
            sender,
            "Azure SQL Edge",
            2,
        )
        self.assertEqual(requested_add.state, "confirmation_required")
        assert requested_add.confirmation is not None
        add_number = next(
            index
            for index, candidate in enumerate(
                requested_add.confirmation.candidates,
                start=1,
            )
            if candidate.sku_title == "Azure SQL Edge - 1 year"
        )
        added = await self.orchestrator.confirm_sku_change(sender, add_number)
        assert added.estate is not None
        added_line = next(line for line in added.estate.lines if line.line_id == "L2")
        self.assertEqual(added_line.sku_title, "Azure SQL Edge - 1 year")
        self.assertIsNone(added_line.product_id)
        self.assertIsNone(added_line.sku_id)

        requested_replace = await self.orchestrator.replace_requirement_sku(
            sender,
            "L1",
            "Azure SQL Edge",
            3,
        )
        self.assertEqual(requested_replace.state, "confirmation_required")
        assert requested_replace.confirmation is not None
        replace_number = next(
            index
            for index, candidate in enumerate(
                requested_replace.confirmation.candidates,
                start=1,
            )
            if candidate.sku_title == "Azure SQL Edge - 1 year"
        )
        replaced = await self.orchestrator.confirm_sku_change(sender, replace_number)
        assert replaced.estate is not None
        replaced_line = next(
            line for line in replaced.estate.lines if line.line_id == "L1"
        )
        self.assertEqual(replaced_line.sku_title, "Azure SQL Edge - 1 year")
        self.assertIsNone(replaced_line.product_id)
        self.assertIsNone(replaced_line.sku_id)

        scenario = await self.orchestrator.confirm_requirement_and_price_as_is(sender)
        self.assertEqual(
            [(line.sku_title, line.unit_price) for line in scenario.lines],
            [
                ("Azure SQL Edge - 1 year", Decimal("7438.25")),
                ("Azure SQL Edge - 1 year", Decimal("7438.25")),
            ],
        )
        self.assertEqual(scenario.total_value, Decimal("37191.25"))

    async def test_appending_distinct_name_only_products_never_merges_blank_id_pair(
        self,
    ) -> None:
        sender = "v6-distinct-name-only-lines"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="first-seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Azure SQL Edge - 1 year",
                    total_licenses=2,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=2,
                )
            ],
        )
        self.assertFalse(estate.pending_lines)

        combined = await self.orchestrator.append_extracted(
            sender=sender,
            source_file="second-seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="SQL Server Big Data Node Cores - 1 Year Subscription",
                    total_licenses=3,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=3,
                )
            ],
        )

        self.assertEqual(len(combined.lines), 2)
        self.assertEqual(
            [(line.sku_title, line.renewal_quantity) for line in combined.lines],
            [
                ("Azure SQL Edge - 1 year", 2),
                ("SQL Server Big Data Node Cores - 1 Year Subscription", 3),
            ],
        )
        self.assertTrue(
            all(not line.product_id and not line.sku_id for line in combined.lines)
        )

    async def test_whatsapp_name_only_confirmation_uses_selected_candidate_title(
        self,
    ) -> None:
        sender = "v6-name-only-whatsapp-confirmation"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="seller-message",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="Azure SQL Edge",
                    total_licenses=2,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=2,
                )
            ],
        )
        line = estate.pending_lines[0]
        selected_number = next(
            index
            for index, candidate in enumerate(line.candidates, start=1)
            if candidate.sku_title == "Azure SQL Edge - 3 year"
        )

        confirmed = await self.service._confirm_requirement_candidate(
            sender,
            capture_token=estate.capture_token[:16],
            line_id="L1",
            candidate_number=selected_number,
        )

        self.assertEqual(confirmed.lines[0].sku_title, "Azure SQL Edge - 3 year")
        self.assertEqual(confirmed.lines[0].match_method, "seller_confirmed")

    async def test_whatsapp_paginates_every_matching_e5_identity(self) -> None:
        sender = "all-e5-options-seller"
        estate = await self.orchestrator.analyze_extracted(
            sender=sender,
            source_file="voice-note.wav",
            rows=[
                ParsedLicenseRow(
                    row_number=2,
                    product_title="E5",
                    total_licenses=10,
                    expired_licenses=0,
                    assigned_licenses=0,
                    renewal_quantity=10,
                )
            ],
        )
        line = estate.pending_lines[0]
        self.assertEqual(len(line.candidates), 21)

        await self.service._send_pending_match_lists(sender, [line])

        interactive = [
            message.interactive
            for message in self.client.messages
            if getattr(message, "interactive", None) is not None
        ]
        self.assertEqual(len(interactive), 3)
        self.assertEqual(
            [len(message.action.sections[0].rows) for message in interactive],
            [10, 10, 1],
        )
        ids = [
            row.id
            for message in interactive
            for row in message.action.sections[0].rows
        ]
        self.assertEqual(len(ids), 21)
        self.assertTrue(ids[0].endswith("|1"))
        self.assertTrue(ids[-1].endswith("|21"))
        descriptions = [
            row.description
            for message in interactive
            for row in message.action.sections[0].rows
        ]
        self.assertTrue(all(description.startswith("ID ") for description in descriptions))


if __name__ == "__main__":
    unittest.main()
