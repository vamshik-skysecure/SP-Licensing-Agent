from __future__ import annotations

import asyncio
import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.licensing.analysis import LicenseAnalyzer
from app.core.licensing.models import ScenarioType
from app.core.licensing.mobile_tables import (
    render_comparison_table_images,
    render_estate_table_images,
    render_scenario_table_images,
)
from app.core.licensing.orchestrator import LicensingOrchestrator
from app.core.licensing.rate_card import LocalRateCardSource, RateCardProvider
from app.core.licensing.renderer import (
    format_comparison,
    format_estate,
    format_scenario,
    render_comparison_pdf,
    render_estate_pdf,
    render_proposal_pdf,
)
from app.core.licensing.scenarios import ScenarioEngine
from app.core.licensing.store import InMemoryWorkflowStore


ROOT = Path(__file__).parents[1]
WORKBOOK = ROOT / "docs" / "microsoft_sku_v5.xlsx"
CUSTOMER_FILE = ROOT / "docs" / "uat" / "synthetic_enterprise_estate.csv"
OUTPUT_DIR = ROOT / "artifacts" / "uat"


def pdf_metadata(content: bytes) -> dict[str, object]:
    return {
        "bytes": len(content),
        "pages": len(re.findall(rb"/Type\s*/Page\b", content)),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def png_metadata(content: bytes) -> dict[str, object]:
    from PIL import Image
    from io import BytesIO

    with Image.open(BytesIO(content)) as image:
        width, height = image.size
    return {
        "bytes": len(content),
        "width": width,
        "height": height,
        "sha256": hashlib.sha256(content).hexdigest(),
    }


async def run() -> dict[str, object]:
    provider = RateCardProvider(
        LocalRateCardSource(WORKBOOK),
        sheet_name="Final Output Sheet",
        refresh_seconds=3600,
    )
    try:
        orchestrator = LicensingOrchestrator(
            analyzer=LicenseAnalyzer(provider),
            rate_cards=provider,
            scenarios=ScenarioEngine(apply_bundle_rules=False),
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
        scenario = await orchestrator.build_scenario(
            sender,
            ScenarioType.RENEW_AS_IS,
        )
        before_total = scenario.total_value
        initial_unresolved = list(scenario.unresolved_decisions)
        scenario = await orchestrator.reconfigure_pricing(
            sender,
            promo_eligible=True,
        )
        eligible_total = scenario.total_value
        scenario = await orchestrator.edit_quantity(sender, "L1", 120)
        scenario = await orchestrator.set_discount(sender, Decimal("5"))
        scenario = await orchestrator.set_adjustment(sender, Decimal("-25000"))
        scenario = await orchestrator.add_comment(
            sender,
            "Synthetic UAT only; final licensing approval pending.",
        )
        scenario = await orchestrator.finalize(sender)

        estate, scenarios, comparison = await orchestrator.comparison(sender)

        session = await orchestrator.get_session(sender)
        assert session is not None and session.estate is not None
        estate = session.estate
        estate_pdf = render_estate_pdf(estate, include_migration_review=False)
        proposal_pdf = render_proposal_pdf(estate, scenario)
        comparison_pdf = render_comparison_pdf(estate, scenarios, comparison)
        estate_images = render_estate_table_images(estate)
        scenario_images = render_scenario_table_images(scenario)
        comparison_images = render_comparison_table_images(comparison)

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        estate_path = OUTPUT_DIR / "synthetic-estate.pdf"
        proposal_path = OUTPUT_DIR / "synthetic-renewal-proposal.pdf"
        comparison_path = OUTPUT_DIR / "synthetic-annual-comparison.pdf"
        estate_path.write_bytes(estate_pdf)
        proposal_path.write_bytes(proposal_pdf)
        comparison_path.write_bytes(comparison_pdf)
        image_paths: dict[str, list[Path]] = {
            "estate": [],
            "scenario": [],
            "comparison": [],
        }
        for label, contents in (
            ("estate", estate_images),
            ("scenario", scenario_images),
            ("comparison", comparison_images),
        ):
            for index, content in enumerate(contents, start=1):
                path = OUTPUT_DIR / f"synthetic-{label}-table-{index}.png"
                path.write_bytes(content)
                image_paths[label].append(path)

        result: dict[str, object] = {
            "generated_at": datetime.now(UTC).isoformat(),
            "mode": {
                "rate_card": "local",
                "workflow_store": "memory",
                "workflow_mode": "upgrade_comparison",
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
            "estate": {
                "status": estate.status.value,
                "line_count": len(estate.lines),
                "pending_matches": len(estate.pending_lines),
                "dated_lines": sum(
                    bool(line.expiration_date or line.renewal_date)
                    for line in estate.lines
                ),
                "chat_output": format_estate(
                    estate,
                    include_migration_review=False,
                ),
            },
            "renewal_edit": {
                "before_total": str(before_total),
                "initial_unresolved_decisions": initial_unresolved,
                "promotion_eligible_total": str(eligible_total),
                "after_total": str(scenario.total_value),
                "revision": scenario.revision,
                "status": scenario.status.value,
                "l1_quantity": next(
                    line.proposed_quantity
                    for line in scenario.lines
                    if line.line_id == "L1"
                ),
                "promo_eligible": scenario.promo_eligible,
                "discount_percentage": str(scenario.discount_percentage),
                "adjustment_amount": str(scenario.adjustment_amount),
                "unresolved_decisions": scenario.unresolved_decisions,
                "chat_output": format_scenario(scenario),
            },
            "annual_comparison": {
                "chat_output": format_comparison(comparison),
                "recommended": comparison.recommended_scenario.value,
                "rationale": comparison.recommendation_rationale,
                "scenarios": [
                    {
                        "scenario": item.scenario_type.value,
                        "term_duration": item.term_duration,
                        "billing_plan": item.billing_plan,
                        "promo_eligible": item.promo_eligible,
                        "subtotal": str(item.subtotal),
                        "discount_percentage": str(item.discount_percentage),
                        "adjustment_amount": str(item.adjustment_amount),
                        "total": str(item.total_value),
                        "difference_from_renew_as_is": str(
                            next(
                                row.difference_from_renew_as_is
                                for row in comparison.rows
                                if row.scenario_type == item.scenario_type
                            )
                        ),
                        "retained_addons": [
                            {
                                "line_id": line.line_id,
                                "title": line.sku_title,
                                "quantity": line.proposed_quantity,
                                "disposition": line.disposition.value,
                                "note": line.note,
                            }
                            for line in item.lines
                            if line.category == "additional"
                            and line.disposition.value == "retain"
                        ],
                        "unresolved_decisions": item.unresolved_decisions,
                    }
                    for item in scenarios
                ],
            },
            "pricebook": {
                "sheet": "Final Output Sheet",
                "price_basis": "Partner Best Offer",
                "rows": 4030,
            },
            "pdfs": {
                "estate": {
                    "path": str(estate_path.relative_to(ROOT)),
                    **pdf_metadata(estate_pdf),
                },
                "proposal": {
                    "path": str(proposal_path.relative_to(ROOT)),
                    **pdf_metadata(proposal_pdf),
                },
                "comparison": {
                    "path": str(comparison_path.relative_to(ROOT)),
                    **pdf_metadata(comparison_pdf),
                },
            },
            "mobile_table_images": {
                label: [
                    {
                        "path": str(path.relative_to(ROOT)),
                        **png_metadata(content),
                    }
                    for path, content in zip(image_paths[label], contents, strict=True)
                ]
                for label, contents in (
                    ("estate", estate_images),
                    ("scenario", scenario_images),
                    ("comparison", comparison_images),
                )
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
