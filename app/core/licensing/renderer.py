from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from xml.sax.saxutils import escape

from .models import (
    CommercialComparison,
    CommercialScenario,
    LicenseEstate,
    NormalizedLicenseLine,
)


def format_money(value: Decimal, currency: str = "INR") -> str:
    return f"{currency} {value:,.2f}"


def format_estate(estate: LicenseEstate) -> str:
    lines = [
        "*Current licence estate*",
        f"File: {estate.source_file}",
        f"Rate card: {estate.rate_card_version}",
        "",
    ]
    for item in estate.lines:
        date_value = item.renewal_date or item.expiration_date
        sku = (
            f"{item.product_id}/{item.sku_id}"
            if item.product_id and item.sku_id
            else "Needs confirmation"
        )
        lines.extend(
            [
                f"*{item.line_id} · {item.display_title}*",
                f"SKU: {sku}",
                f"Total {item.total_licenses:,} | Expired {item.expired_licenses:,} | "
                f"Renew {item.renewal_quantity:,}",
                f"Assigned {item.assigned_licenses:,} | Renewal/expiry: {_date(date_value)}",
                "",
            ]
        )
    if estate.pending_lines:
        lines.append(
            f"{len(estate.pending_lines)} SKU matches require confirmation before pricing."
        )
    else:
        lines.append("Choose a commercial scenario to prepare.")
    return "\n".join(lines).strip()


def format_pending_matches(estate: LicenseEstate) -> str:
    lines = ["*Confirm SKU matches*"]
    for pending in estate.pending_lines:
        lines.extend(("", f"{pending.line_id}. {pending.source_product_title}"))
        for index, candidate in enumerate(pending.candidates, start=1):
            lines.append(
                f"  {index}) {candidate.sku_title} "
                f"[{candidate.product_id}/{candidate.sku_id}] ({candidate.confidence:.1f}%)"
            )
    lines.extend(
        (
            "",
            "Reply once with every choice, for example:",
            "/confirm L1=PRODUCT_ID,SKU_ID; L2=PRODUCT_ID,SKU_ID",
        )
    )
    return "\n".join(lines)


_CORE_SUITE_TITLES = {
    "microsoft 365 e3",
    "microsoft 365 e5 without audio conferencing",
    "microsoft 365 e7 without audio conferencing",
}


def product_family(title: str) -> str:
    """Return a deterministic display grouping derived from the workbook title."""
    normalized = title.casefold()
    rules = (
        ("enterprise mobility + security", "Enterprise Mobility + Security"),
        ("microsoft 365", "Microsoft 365"),
        ("office 365", "Office 365"),
        ("dynamics 365", "Dynamics 365"),
        ("power bi", "Power BI"),
        ("power apps", "Power Platform"),
        ("power automate", "Power Platform"),
        ("dataverse", "Power Platform / Dataverse"),
        ("copilot", "Copilot"),
        ("defender", "Microsoft Defender"),
        ("teams", "Microsoft Teams"),
        ("azure", "Azure"),
    )
    for marker, family in rules:
        if marker in normalized:
            return family
    return "Other / Add-ons"


def migration_review_status(line: NormalizedLicenseLine) -> str:
    if line.match_method == "unresolved":
        return "SKU match required"
    normalized = " ".join(line.display_title.casefold().split())
    if normalized in _CORE_SUITE_TITLES:
        return "Core-suite path available"
    if normalized == "microsoft 365 copilot":
        return "Copilot quantity review"
    return "Seller decision required"


def estate_line_flags(
    line: NormalizedLicenseLine,
    *,
    as_of: date,
    near_expiry_days: int,
) -> list[str]:
    flags: list[str] = []
    deadline = line.renewal_date or line.expiration_date
    if deadline is None:
        flags.append("Date not supplied")
    elif deadline < as_of:
        flags.append("Renewal/expiry overdue")
    elif deadline <= as_of + timedelta(days=near_expiry_days):
        flags.append(f"Due within {near_expiry_days} days")
    review = migration_review_status(line)
    if review in {"SKU match required", "Seller decision required"}:
        flags.append(review)
    return flags


def render_estate_pdf(
    estate: LicenseEstate,
    *,
    as_of: date | None = None,
    near_expiry_days: int = 90,
) -> bytes:
    """Render the immediate post-upload estate as a grouped customer-facing PDF."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    report_date = as_of or date.today()
    grouped: dict[str, list[NormalizedLicenseLine]] = defaultdict(list)
    for line in estate.lines:
        grouped[product_family(line.display_title)].append(line)

    attention = [
        (line, estate_line_flags(
            line,
            as_of=report_date,
            near_expiry_days=near_expiry_days,
        ))
        for line in estate.lines
    ]
    attention = [(line, flags) for line, flags in attention if flags]

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="Customer Licence Estate",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "EstateTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17365D"),
    )
    story: list[object] = [
        Paragraph("Customer Licence Estate", title_style),
        Spacer(1, 3 * mm),
        Paragraph(f"Source file: {escape(estate.source_file)}", styles["Normal"]),
        Paragraph(f"Report date: {report_date.isoformat()}", styles["Normal"]),
        Paragraph(
            f"Near-expiry window: {near_expiry_days} days | "
            f"Rate-card version: {escape(estate.rate_card_version)}",
            styles["Normal"],
        ),
        Spacer(1, 4 * mm),
        _table(
            [
                ["SKUs", "Renewal quantity", "Expired", "Attention required"],
                [
                    str(len(estate.lines)),
                    str(estate.total_renewal_quantity),
                    str(sum(line.expired_licenses for line in estate.lines)),
                    str(len(attention)),
                ],
            ],
            [30 * mm, 45 * mm, 30 * mm, 45 * mm],
        ),
        Spacer(1, 5 * mm),
    ]

    for family in sorted(grouped):
        story.append(Paragraph(escape(family), styles["Heading2"]))
        data: list[list[object]] = [[
            "Line",
            "SKU",
            "Product",
            "Total",
            "Expired",
            "Renew",
            "Assigned",
            "Renewal / expiry",
            "Migration review",
        ]]
        for line in sorted(grouped[family], key=lambda item: item.display_title.casefold()):
            sku = (
                f"{line.product_id}/{line.sku_id}"
                if line.product_id and line.sku_id
                else "Needs confirmation"
            )
            data.append([
                line.line_id,
                Paragraph(escape(sku), styles["BodyText"]),
                Paragraph(escape(line.display_title), styles["BodyText"]),
                str(line.total_licenses),
                str(line.expired_licenses),
                str(line.renewal_quantity),
                str(line.assigned_licenses),
                _date(line.renewal_date or line.expiration_date),
                Paragraph(escape(migration_review_status(line)), styles["BodyText"]),
            ])
        story.extend([
            _table(
                data,
                [12 * mm, 35 * mm, 65 * mm, 15 * mm, 15 * mm, 15 * mm, 18 * mm, 28 * mm, 43 * mm],
            ),
            Spacer(1, 4 * mm),
        ])

    story.append(Paragraph("Attention required", styles["Heading2"]))
    if attention:
        attention_data: list[list[object]] = [["Line", "Product", "Flags"]]
        for line, flags in attention:
            attention_data.append([
                line.line_id,
                Paragraph(escape(line.display_title), styles["BodyText"]),
                Paragraph(escape("; ".join(flags)), styles["BodyText"]),
            ])
        story.append(_table(attention_data, [16 * mm, 95 * mm, 125 * mm]))
    else:
        story.append(Paragraph("No near-expiry, missing-date, match, or migration-review flags.", styles["Normal"]))

    document.build(story)
    return output.getvalue()


def format_scenario(
    scenario: CommercialScenario,
    currency: str = "INR",
) -> str:
    lines = [
        f"*{scenario.scenario_type.label} · revision {scenario.revision}*",
        f"Term: {scenario.term_duration} | Billing: {scenario.billing_plan} | "
        f"Segment: {scenario.segment}",
        f"Promo pricing: {'on' if scenario.promo_eligible else 'off'}",
        "",
    ]
    for item in scenario.lines:
        decision = " ⚠ decision" if item.decision_required else ""
        if item.disposition.value in {"migrate", "included", "remove"}:
            price_status = " · price not applicable"
        elif item.price_unavailable:
            price_status = " ⚠ PRICE UNAVAILABLE"
        elif item.unit_price == 0:
            price_status = " · no-charge price"
        else:
            price_status = ""
        lines.extend(
            [
                f"*{item.line_id} · {item.sku_title}*{decision}",
                f"{item.disposition.value} | existing {item.existing_quantity:,} → "
                f"proposed {item.proposed_quantity:,}",
                f"{format_money(item.unit_price, currency)} each | "
                f"{format_money(item.extended_price, currency)}{price_status}",
            ]
        )
        if item.note:
            lines.append(f"Note: {item.note}")
        lines.append("")
    lines.extend(
        [
            f"Subtotal: {format_money(scenario.subtotal, currency)}",
            f"Discount: {scenario.discount_percentage}%",
            f"Adjustments: {format_money(scenario.adjustment_amount, currency)}",
            f"*Final total: {format_money(scenario.total_value, currency)}*",
        ]
    )
    if scenario.assumptions:
        lines.extend(("", "*Assumptions*"))
        lines.extend(f"- {value}" for value in scenario.assumptions)
    if scenario.unresolved_decisions:
        lines.extend(("", "*Seller decisions required*"))
        lines.extend(f"- {value}" for value in scenario.unresolved_decisions)
    lines.extend(
        (
            "",
            "Edit commands: /set LINE QTY, /retain LINE, /remove LINE, "
            "/add PRODUCT | QTY, /replace LINE | PRODUCT | QTY, /copilot QTY, "
            "/promo on|off, /discount PERCENT, /adjust AMOUNT, /term TERM, "
            "/billing PLAN, /segment SEGMENT, /currency CODE, /comment TEXT, /finalize",
        )
    )
    return "\n".join(lines)


def format_comparison(
    comparison: CommercialComparison,
    currency: str = "INR",
) -> str:
    lines = [
        "*Commercial comparison*",
        "",
        f"*Recommended option: {comparison.recommended_scenario.label}*",
        comparison.recommendation_rationale,
    ]
    for row in comparison.rows:
        lines.extend(
            [
                "",
                f"*{row.scenario_type.label}*",
                f"Base: {format_money(row.base_licences, currency)}",
                f"Copilot: {format_money(row.copilot, currency)}",
                f"Additional/retained: {format_money(row.additional_or_retained, currency)}",
                f"Total: *{format_money(row.total_cost, currency)}*",
            ]
        )
    return "\n".join(lines)


def render_comparison_pdf(
    estate: LicenseEstate,
    scenarios: list[CommercialScenario],
    comparison: CommercialComparison,
    *,
    currency: str = "INR",
) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title="SP/SSP Licensing Commercial Comparison",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17365D"),
    )
    story = [
        Paragraph("SP/SSP Licensing Commercial Recommendation", title_style),
        Spacer(1, 5 * mm),
        Paragraph(
            f"Recommended option: <b>{comparison.recommended_scenario.label}</b>",
            styles["Heading2"],
        ),
        Paragraph(escape(comparison.recommendation_rationale), styles["Normal"]),
        Spacer(1, 3 * mm),
        Paragraph(f"Source file: {estate.source_file}", styles["Normal"]),
        Paragraph(f"Rate-card version: {estate.rate_card_version}", styles["Normal"]),
        Spacer(1, 5 * mm),
    ]

    estate_data = [["SKU", "Product", "Total", "Expired", "Renew", "Renewal/Expiry"]]
    for line in estate.lines:
        estate_data.append(
            [
                f"{line.product_id or '-'} / {line.sku_id or '-'}",
                line.display_title,
                str(line.total_licenses),
                str(line.expired_licenses),
                str(line.renewal_quantity),
                _date(line.renewal_date or line.expiration_date),
            ]
        )
    story.extend(
        [
            Paragraph("Current licence estate", styles["Heading2"]),
            _table(estate_data, [42 * mm, 95 * mm, 20 * mm, 20 * mm, 20 * mm, 36 * mm]),
            Spacer(1, 6 * mm),
        ]
    )

    comparison_data = [["Scenario", "Base", "Copilot", "Additional / Retained", "Total"]]
    for row in comparison.rows:
        comparison_data.append(
            [
                row.scenario_type.label,
                format_money(row.base_licences, currency),
                format_money(row.copilot, currency),
                format_money(row.additional_or_retained, currency),
                format_money(row.total_cost, currency),
            ]
        )
    story.extend(
        [
            Paragraph("Scenario comparison", styles["Heading2"]),
            _table(comparison_data, [48 * mm, 43 * mm, 43 * mm, 56 * mm, 43 * mm]),
        ]
    )

    for scenario in scenarios:
        story.extend((PageBreak(), Paragraph(scenario.scenario_type.label, title_style)))
        detail = [[
            "SKU",
            "Action",
            "Existing",
            "Proposed",
            "Unit price",
            "Extended",
            "Price status",
        ]]
        for line in scenario.lines:
            if line.disposition.value in {"migrate", "included", "remove"}:
                price_status = "Not applicable"
            elif line.price_unavailable:
                price_status = "PRICE UNAVAILABLE"
            elif line.unit_price == 0:
                price_status = "No-charge price"
            else:
                price_status = "Priced"
            detail.append(
                [
                    line.sku_title,
                    line.disposition.value,
                    str(line.existing_quantity),
                    str(line.proposed_quantity),
                    format_money(line.unit_price, currency),
                    format_money(line.extended_price, currency),
                    price_status,
                ]
            )
        story.extend(
            [
                Spacer(1, 4 * mm),
                _table(
                    detail,
                    [65 * mm, 27 * mm, 20 * mm, 20 * mm, 31 * mm, 31 * mm, 41 * mm],
                ),
                Spacer(1, 4 * mm),
                Paragraph(
                    f"Final total: <b>{format_money(scenario.total_value, currency)}</b>",
                    styles["Heading3"],
                ),
            ]
        )
        if scenario.comments or scenario.assumptions:
            story.append(Paragraph("Comments and assumptions", styles["Heading3"]))
            for comment in [*scenario.comments, *scenario.assumptions]:
                story.append(Paragraph(f"• {comment}", styles["Normal"]))

    document.build(story)
    return output.getvalue()


def _table(data: list[list[object]], widths: list[float]):
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#17365D")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B7C9E2")),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6FA")]),
                ("FONTSIZE", (0, 0), (-1, -1), 8),
                ("LEFTPADDING", (0, 0), (-1, -1), 4),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _date(value: date | None) -> str:
    return value.isoformat() if value else "Not supplied"
