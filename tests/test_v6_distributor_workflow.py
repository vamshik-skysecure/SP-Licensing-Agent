import unittest
from decimal import Decimal
from pathlib import Path

from app.core.licensing.models import (
    EstateStatus,
    LicenseEstate,
    NormalizedLicenseLine,
    ScenarioType,
)
from app.core.licensing.rate_card import RateCardCatalog, parse_rate_card
from app.core.licensing.renderer import format_sku_candidate
from app.core.licensing.scenarios import ScenarioEngine


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


if __name__ == "__main__":
    unittest.main()
