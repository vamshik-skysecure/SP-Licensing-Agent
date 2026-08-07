import csv
from io import BytesIO
import unittest
from pathlib import Path
import zipfile

from PIL import Image
from pypdf import PdfReader

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

    def test_multiformat_pack_contains_one_consistent_synthetic_requirement(self) -> None:
        root = Path(__file__).parents[1]
        assets = root / "docs" / "uat" / "input_formats"
        expected_titles = [
            "Microsoft 365 E3",
            "Power BI Pro",
            "Enterprise Mobility + Security E3",
            "Microsoft Teams Phone Standard",
            "Microsoft Defender for Office 365 (Plan 1)",
        ]
        expected_quantities = [120, 30, 120, 60, 120]

        parsed_versions = []
        for filename in (
            "licensing_requirement.csv",
            "licensing_requirement.xlsx",
            "licensing_requirement_arbitrary_layout.xlsx",
        ):
            rows = parse_customer_file((assets / filename).read_bytes(), filename)
            parsed_versions.append(
                [
                    (
                        row.product_id,
                        row.sku_id,
                        row.product_title,
                        row.total_licenses,
                        row.term_duration,
                        row.billing_plan,
                        row.renewal_date,
                    )
                    for row in rows
                ]
            )
        self.assertEqual(parsed_versions[0], parsed_versions[1])
        self.assertEqual(parsed_versions[0], parsed_versions[2])
        self.assertEqual([row[2] for row in parsed_versions[0]], expected_titles)
        self.assertEqual([row[3] for row in parsed_versions[0]], expected_quantities)
        self.assertTrue(all(row[4:6] == ("P1Y", "Annual") for row in parsed_versions[0]))

        with (assets / "licensing_requirement.tsv").open(
            newline="", encoding="utf-8-sig"
        ) as handle:
            tsv_rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual([row["SKU"] for row in tsv_rows], expected_titles)

        text = (assets / "licensing_requirement.txt").read_text(encoding="utf-8")
        voice_script = (assets / "voice_note_script.txt").read_text(encoding="utf-8")
        for title in expected_titles:
            self.assertIn(title, text)
            self.assertIn(title, voice_script)

        with zipfile.ZipFile(assets / "licensing_requirement.docx") as archive:
            self.assertIsNone(archive.testzip())
            document_xml = archive.read("word/document.xml").decode("utf-8")
        for title in expected_titles:
            self.assertIn(title.replace("&", "&amp;"), document_xml)

        text_pdf = PdfReader(assets / "licensing_requirement_text.pdf")
        extracted = "\n".join(page.extract_text() or "" for page in text_pdf.pages)
        for title in expected_titles:
            self.assertIn(title, extracted)
        scanned_pdf = PdfReader(assets / "licensing_requirement_scanned.pdf")
        self.assertEqual("".join(page.extract_text() or "" for page in scanned_pdf.pages), "")

        for filename, expected_format in (
            ("licensing_requirement.png", "PNG"),
            ("licensing_requirement.jpg", "JPEG"),
            ("licensing_requirement.webp", "WEBP"),
            ("negative_unclear_quantity.jpg", "JPEG"),
        ):
            with Image.open(BytesIO((assets / filename).read_bytes())) as image:
                self.assertEqual(image.format, expected_format)
                self.assertEqual(image.size, (1700, 990))

        bundle = assets.parent / "ssp_multiformat_uat_pack.zip"
        with zipfile.ZipFile(bundle) as archive:
            bundled_names = set(archive.namelist())
        self.assertEqual(
            bundled_names,
            {path.name for path in assets.iterdir() if path.is_file()},
        )

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
