from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.mobile_tables import (
    render_estate_table_images,
    render_simple_comparison_table_images,
    render_simple_pricing_table_images,
)
from app.core.licensing.models import ScenarioType
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.renderer import (
    format_estate,
    render_estate_pdf,
    render_simple_commercial_pdf,
)
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v6_distributor.xlsx"
CUSTOMER_FILE = ROOT / "docs" / "uat" / "synthetic_enterprise_estate.csv"
OUTPUT_DIR = ROOT / "artifacts" / "uat"
SHEET_NAME = "Outcome Sheet"
PRICE_BASIS = "Expectec Disti Price to Skysecure"
CUSTOMER_PRICE_LABEL = "Applicable annual licence price"


def pdf_metadata(content: bytes) -> dict[str, object]:
    return {
        "bytes": len(content),
        "pages": len(re.findall(rb"/Type\s*/Page\b", content)),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def png_metadata(content: bytes) -> dict[str, object]:
    from io import BytesIO

    from PIL import Image

    with Image.open(BytesIO(content)) as image:
        width, height = image.size
    return {
        "bytes": len(content),
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


async def run() -> dict[str, object]:
    """Run the current V1 seller-confirmed as-is/revision journey locally."""

    provider = RateCardProvider(
        LocalRateCardSource(WORKBOOK),
        sheet_name=SHEET_NAME,
        refresh_seconds=3600,
    )
    try:
        orchestrator = LicensingOrchestrator(
            analyzer=LicenseAnalyzer(provider),
            rate_cards=provider,
            scenarios=ScenarioEngine(
                apply_bundle_rules=False,
                price_basis="distributor_expected",
            ),
            store=InMemoryWorkflowStore(),
            default_term_duration="P1Y",
            default_billing_plan="Annual",
            default_segment="Commercial",
        )
        sender = "synthetic-uat-seller"
        estate = await orchestrator.analyze_document(
            sender=sender,
            filename=CUSTOMER_FILE.name,
            content=CUSTOMER_FILE.read_bytes(),
        )
        if estate.pending_lines:
            raise RuntimeError(
                "The golden fixture has unresolved catalogue matches; explicit seller "
                "confirmation is required before it can be priced."
            )

        # Production pricing is deliberately gated behind seller confirmation.
        estate = await orchestrator.request_requirement_validation(sender)
        estate = await orchestrator.confirm_requirement(sender)
        as_is = await orchestrator.build_scenario(sender, ScenarioType.RENEW_AS_IS)
        if any(line.price_unavailable for line in as_is.lines):
            raise RuntimeError("The golden fixture contains an unavailable V6 price.")
        await orchestrator.save_confirmed_as_is(sender, as_is)

        # A seller-requested quantity change is recalculated against the confirmed baseline.
        revised = await orchestrator.edit_quantity(sender, "L1", 125)
        revised = await orchestrator.add_comment(
            sender,
            "Synthetic UAT only; final customer approval pending.",
        )
        revised = await orchestrator.finalize(sender)
        estate, confirmed_as_is, revised = await orchestrator.simple_review(sender)

        estate_pdf = render_estate_pdf(estate, include_migration_review=False)
        commercial_pdf = render_simple_commercial_pdf(
            estate,
            confirmed_as_is,
            revised,
            pricing_source=CUSTOMER_PRICE_LABEL,
        )
        estate_images = render_estate_table_images(estate)
        current_images = render_simple_pricing_table_images(
            confirmed_as_is,
            title="Confirmed Renew As-Is cost",
            pricing_source=CUSTOMER_PRICE_LABEL,
        )
        revised_images = render_simple_pricing_table_images(
            revised,
            title="Seller-requested revised configuration",
            pricing_source=CUSTOMER_PRICE_LABEL,
        )
        comparison_images = render_simple_comparison_table_images(
            confirmed_as_is,
            revised,
            pricing_source=CUSTOMER_PRICE_LABEL,
        )

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        binary_outputs: dict[str, bytes] = {
            "synthetic-estate.pdf": estate_pdf,
            "synthetic-commercial-proposal.pdf": commercial_pdf,
        }
        image_groups = {
            "estate": estate_images,
            "current": current_images,
            "revised": revised_images,
            "comparison": comparison_images,
        }
        for filename, content in binary_outputs.items():
            (OUTPUT_DIR / filename).write_bytes(content)
        for label, contents in image_groups.items():
            for index, content in enumerate(contents, start=1):
                (OUTPUT_DIR / f"synthetic-{label}-table-{index}.png").write_bytes(
                    content
                )

        catalogue = await provider.get()
        difference = revised.total_value - confirmed_as_is.total_value
        result: dict[str, object] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": {
                "rate_card": "local",
                "workflow_store": "memory",
                "workflow_mode": "simple_pricing",
                "annual_contract_enforced": True,
                "bundle_rules_applied": False,
                "openai_used": False,
                "azure_used": False,
                "whatsapp_used": False,
            },
            "inputs": {
                "workbook": str(WORKBOOK.relative_to(ROOT)),
                "workbook_sha256": hashlib.sha256(WORKBOOK.read_bytes()).hexdigest(),
                "customer_file": str(CUSTOMER_FILE.relative_to(ROOT)),
                "customer_sha256": hashlib.sha256(CUSTOMER_FILE.read_bytes()).hexdigest(),
            },
            "capture": {
                "seller_confirmation_recorded_before_pricing": True,
                "status": estate.status.value,
                "line_count": len(estate.lines),
                "pending_matches": len(estate.pending_lines),
                "chat_output": format_estate(
                    estate,
                    include_migration_review=False,
                ),
            },
            "commercial_review": {
                "confirmed_as_is_total": str(confirmed_as_is.total_value),
                "revised_total": str(revised.total_value),
                "difference": str(difference),
                "edited_line": "L1",
                "before_quantity": next(
                    line.proposed_quantity
                    for line in confirmed_as_is.lines
                    if line.line_id == "L1"
                ),
                "after_quantity": next(
                    line.proposed_quantity
                    for line in revised.lines
                    if line.line_id == "L1"
                ),
                "status": revised.status.value,
                "unavailable_price_lines": sum(
                    line.price_unavailable for line in revised.lines
                ),
                "seller_comments": revised.comments,
            },
            "rate_card": {
                "sheet": SHEET_NAME,
                "internal_price_column": PRICE_BASIS,
                "customer_facing_label": CUSTOMER_PRICE_LABEL,
                "rows_loaded": len(catalogue.items),
            },
            "pdfs": {
                "estate": {
                    "path": "artifacts/uat/synthetic-estate.pdf",
                    **pdf_metadata(estate_pdf),
                },
                "commercial_proposal": {
                    "path": "artifacts/uat/synthetic-commercial-proposal.pdf",
                    **pdf_metadata(commercial_pdf),
                },
            },
            "mobile_table_images": {
                label: [
                    {
                        "path": (
                            f"artifacts/uat/synthetic-{label}-table-{index}.png"
                        ),
                        **png_metadata(content),
                    }
                    for index, content in enumerate(contents, start=1)
                ]
                for label, contents in image_groups.items()
            },
        }
        evidence_path = OUTPUT_DIR / "golden-run.json"
        evidence_path.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return result
    finally:
        await provider.close()


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2, ensure_ascii=False))
