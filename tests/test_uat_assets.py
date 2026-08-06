import csv
import unittest
from pathlib import Path

from app.core.licensing.analysis import parse_customer_file


class UATAssetTests(unittest.TestCase):
    def test_synthetic_csv_and_xlsx_have_the_same_exact_estate(self) -> None:
        root = Path(__file__).parents[1]
        csv_rows = parse_customer_file(
            (root / "docs" / "uat" / "synthetic_enterprise_estate.csv").read_bytes(),
            "synthetic_enterprise_estate.csv",
        )
        xlsx_rows = parse_customer_file(
            (root / "docs" / "uat" / "synthetic_enterprise_estate.xlsx").read_bytes(),
            "synthetic_enterprise_estate.xlsx",
        )

        self.assertEqual(csv_rows, xlsx_rows)
        self.assertEqual(len(csv_rows), 5)
        self.assertTrue(all(row.product_id and row.sku_id for row in csv_rows))
        self.assertTrue(all(row.renewal_date is not None for row in csv_rows))

    def test_business_review_contains_all_migration_seed_rows(self) -> None:
        root = Path(__file__).parents[1]
        review_path = root / "docs" / "uat" / "migration_seed_business_review.csv"
        with review_path.open(newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))

        self.assertEqual(len(rows), 30)
        self.assertTrue(all(row["currently_approved"] == "false" for row in rows))
        self.assertTrue(all(row["business_decision"] == "" for row in rows))


if __name__ == "__main__":
    unittest.main()
