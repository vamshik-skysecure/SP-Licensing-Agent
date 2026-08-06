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
    ScenarioLine,
)


def format_money(value: Decimal, currency: str = "INR") -> str:
    return f"{currency} {value:,.2f}"


def format_estate(
    estate: LicenseEstate,
    *,
    include_migration_review: bool = True,
) -> str:
    lines = [
        "*Current licence estate*",
        f"Source: {estate.source_file}",
        f"SKU lines: {len(estate.lines)}",
        f"Total licences: {sum(item.total_licenses for item in estate.lines):,}",
    ]
    grouped: dict[str, list[NormalizedLicenseLine]] = defaultdict(list)
    for item in estate.lines:
        grouped[product_family(item.display_title)].append(item)
    for family in sorted(grouped):
        lines.extend(("", f"*{family}*"))
        for item in grouped[family]:
            date_value = item.renewal_date or item.expiration_date
            lines.extend(
                (
                    "",
                    f"*{item.line_id} — {item.display_title}*",
                    f"Total: {item.total_licenses:,} | Assigned: "
                    f"{item.assigned_licenses:,} | Expired: {item.expired_licenses:,}",
                    f"Renewal quantity: {item.renewal_quantity:,}",
                    f"Renewal/expiration: {_date(date_value)}",
                )
            )
    if estate.pending_lines:
        lines.extend(("", "*Attention required*"))
        lines.append(
            f"{len(estate.pending_lines)} SKU matches require confirmation before pricing."
        )
    else:
        lines.extend(("", "All uploaded SKUs matched the maintained pricebook."))
        if include_migration_review:
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
            "Reply naturally with every choice, for example:",
            "“For L1 choose option 1, and for L2 choose option 2.”",
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
        ("dynamics 365", "Dynamics 365"),
        ("power bi", "Power BI"),
        ("power apps", "Power Platform"),
        ("power automate", "Power Platform"),
        ("dataverse", "Power Platform / Dataverse"),
        ("copilot", "Copilot"),
        ("defender", "Microsoft Defender"),
        ("teams", "Microsoft Teams"),
        ("azure", "Azure"),
        ("microsoft 365", "Microsoft 365"),
        ("office 365", "Office 365"),
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
    include_migration_review: bool = True,
) -> list[str]:
    flags: list[str] = []
    deadline = line.renewal_date or line.expiration_date
    if deadline is None:
        flags.append("Date not supplied")
    elif deadline < as_of:
        flags.append("Renewal/expiry overdue")
    elif deadline <= as_of + timedelta(days=near_expiry_days):
        flags.append(f"Due within {near_expiry_days} days")
    if include_migration_review:
        review = migration_review_status(line)
        if review in {"SKU match required", "Seller decision required"}:
            flags.append(review)
    elif line.match_method == "unresolved":
        flags.append("SKU match required")
    return flags


def render_estate_pdf(
    estate: LicenseEstate,
    *,
    as_of: date | None = None,
    near_expiry_days: int = 90,
    include_migration_review: bool = True,
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
            include_migration_review=include_migration_review,
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
            "Migration review" if include_migration_review else "Match status",
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
                Paragraph(
                    escape(
                        migration_review_status(line)
                        if include_migration_review
                        else (
                            "SKU match required"
                            if line.match_method == "unresolved"
                            else "Matched"
                        )
                    ),
                    styles["BodyText"],
                ),
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
        story.append(
            Paragraph(
                "No near-expiry, missing-date, or match flags."
                if not include_migration_review
                else "No near-expiry, missing-date, match, or migration-review flags.",
                styles["Normal"],
            )
        )

    document.build(story)
    return output.getvalue()


def format_scenario(
    scenario: CommercialScenario,
    currency: str = "INR",
) -> str:
    lines = [
        f"*{scenario.scenario_type.label} — Revision {scenario.revision}*",
        f"Term: {scenario.term_duration}",
        f"Billing: {scenario.billing_plan}",
        f"Segment: {scenario.segment}",
        f"Promotion pricing: {'Applied' if scenario.promo_eligible else 'Not applied'}",
        "",
        "*Proposed licence configuration*",
    ]
    for item in scenario.lines:
        lines.extend(("", f"*{item.line_id} — {item.sku_title}*"))
        lines.append(f"Action: {_scenario_action(item)}")
        if item.existing_quantity == item.proposed_quantity:
            lines.append(f"Quantity: {item.proposed_quantity:,}")
        else:
            lines.append(
                f"Existing: {item.existing_quantity:,} | "
                f"Proposed: {item.proposed_quantity:,}"
            )
        if item.disposition.value in {"migrate", "included", "remove"}:
            lines.append("Pricing: Not applicable to this source line")
        elif item.price_unavailable:
            lines.append(
                "Price: Eligibility confirmation required"
                if _promotion_eligibility_required(item)
                else "Price: Unavailable — this is not a free product"
            )
        elif item.unit_price == 0:
            lines.append(
                f"Unit price: {format_money(item.unit_price, currency)} "
                "(confirmed no-charge)"
            )
            lines.append(f"Line total: {format_money(item.extended_price, currency)}")
        else:
            lines.append(f"Unit price: {format_money(item.unit_price, currency)}")
            lines.append(f"Line total: {format_money(item.extended_price, currency)}")
        lines.append(
            f"Renewal/expiration: {_date(item.renewal_date or item.expiration_date)}"
        )
        if item.note and item.disposition.value in {
            "migrate",
            "included",
            "remove",
            "needs_decision",
        }:
            lines.append(f"Note: {item.note}")

    exceptions: list[str] = []
    for item in scenario.lines:
        if item.decision_required:
            exceptions.append(f"{item.line_id}: seller decision required")
        if item.price_unavailable:
            exceptions.append(
                f"{item.line_id}: promotion eligibility confirmation required"
                if _promotion_eligibility_required(item)
                else f"{item.line_id}: price unavailable"
            )
        elif item.unit_price == 0 and item.disposition.value not in {
            "migrate",
            "included",
            "remove",
        }:
            exceptions.append(f"{item.line_id}: no-charge price")
    discount_amount = (
        scenario.subtotal * scenario.discount_percentage / Decimal("100")
    )
    lines.extend(
        [
            "",
            "*Commercial summary*",
            f"Subtotal: {format_money(scenario.subtotal, currency)}",
            f"Discount: {scenario.discount_percentage:,.2f}% "
            f"(-{format_money(discount_amount, currency)})",
            f"Adjustment: {format_money(scenario.adjustment_amount, currency)}",
            f"*Final annual total: {format_money(scenario.total_value, currency)}*",
        ]
    )
    if exceptions:
        lines.extend(("", "*Line notes*"))
        lines.extend(f"- {value}" for value in exceptions)
    if scenario.assumptions:
        lines.extend(("", "*Assumptions*"))
        lines.extend(f"- {value}" for value in scenario.assumptions)
    if scenario.unresolved_decisions:
        lines.extend(("", "*Seller decisions required*"))
        lines.extend(f"- {value}" for value in scenario.unresolved_decisions)
    lines.extend(("", "Reply with the next change, or choose an action below."))
    return "\n".join(lines)


def format_comparison(
    comparison: CommercialComparison,
    currency: str = "INR",
) -> str:
    lines = [
        "*Annual commercial comparison*",
        f"Currency: {currency}",
        "All options use a one-year term and annual billing.",
        "",
        f"*Recommended option: {comparison.recommended_scenario.label}*",
        comparison.recommendation_rationale,
    ]
    for row in comparison.rows:
        lines.extend(
            (
                "",
                f"*{row.scenario_type.label}*",
                f"Annual total: *{format_money(row.total_cost, currency)}*",
                "Difference: "
                + _comparison_difference(
                    row.difference_from_renew_as_is,
                    currency,
                ),
                f"Base licences: {format_money(row.base_licences, currency)}",
                f"Copilot: {format_money(row.copilot, currency)}",
                "Additional/retained licences: "
                f"{format_money(row.additional_or_retained, currency)}",
            )
        )
    lines.extend(
        [
            "",
            "No add-on bundle entitlement is assumed; retained add-ons remain "
            "separately priced unless the seller explicitly changes them.",
        ]
    )
    return "\n".join(lines)


def _scenario_action(item) -> str:
    if item.line_id == "BASE":
        return "Add selected target suite"
    if item.line_id == "COPILOT":
        return "Add Copilot separately"
    return {
        "retain": "Retain separately",
        "migrate": "Replace with selected target suite",
        "included": "Represented by selected target suite",
        "remove": "Remove",
        "needs_decision": "Seller decision required",
        "add": "Add",
    }[item.disposition.value]


def _comparison_difference(value: Decimal, currency: str) -> str:
    if value == 0:
        return "Same as Renew As-Is"
    if value < 0:
        return f"{format_money(abs(value), currency)} lower than Renew As-Is"
    return f"{format_money(value, currency)} higher than Renew As-Is"


def render_proposal_pdf(
    estate: LicenseEstate,
    scenario: CommercialScenario,
    *,
    currency: str = "INR",
) -> bytes:
    """Render the active renewal proposal without requiring bundle scenarios."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="SP/SSP Licensing Renewal Proposal",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ProposalTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17365D"),
    )
    detail_style = ParagraphStyle(
        "ProposalDetail",
        parent=styles["BodyText"],
        fontSize=6,
        leading=7,
        spaceAfter=0,
        spaceBefore=0,
    )
    story: list[object] = [
        Paragraph("SP/SSP Licensing Renewal Proposal", title_style),
        Spacer(1, 3 * mm),
        Paragraph(f"Source file: {escape(estate.source_file)}", styles["Normal"]),
        Paragraph(f"Rate-card version: {escape(estate.rate_card_version)}", styles["Normal"]),
        Paragraph(
            f"Revision: {scenario.revision} | Status: {scenario.status.value} | "
            f"Term: {escape(scenario.term_duration)} | "
            f"Billing: {escape(scenario.billing_plan)} | "
            f"Segment: {escape(scenario.segment)}",
            styles["Normal"],
        ),
        Spacer(1, 5 * mm),
        Paragraph("Proposed licence configuration", styles["Heading2"]),
    ]

    detail: list[list[object]] = [[
        "Line",
        "Product",
        "Action",
        "Existing",
        "Proposed",
        "Licence term",
        "Billing plan",
        "Renewal / expiration",
        "Unit price",
        "Extended",
        "Price status / replacement note",
    ]]
    for line in scenario.lines:
        if line.disposition.value in {"migrate", "included", "remove"}:
            price_status = "Not applicable"
        elif line.price_unavailable:
            price_status = (
                "ELIGIBILITY REQUIRED"
                if _promotion_eligibility_required(line)
                else "PRICE UNAVAILABLE"
            )
        elif line.unit_price == 0:
            price_status = "No-charge price"
        else:
            price_status = "Priced"
        note = f"{price_status}. {line.note}" if line.note else price_status
        detail.append([
            line.line_id,
            Paragraph(escape(line.sku_title), detail_style),
            line.disposition.value,
            str(line.existing_quantity),
            str(line.proposed_quantity),
            line.term_duration,
            line.billing_plan,
            _date(line.renewal_date or line.expiration_date),
            format_money(line.unit_price, currency),
            format_money(line.extended_price, currency),
            Paragraph(escape(note), detail_style),
        ])
    story.extend([
        _table(
            detail,
            [
                12 * mm,
                48 * mm,
                18 * mm,
                15 * mm,
                16 * mm,
                19 * mm,
                21 * mm,
                27 * mm,
                25 * mm,
                27 * mm,
                49 * mm,
            ],
            font_size=6,
            cell_padding=2,
        ),
        Spacer(1, 5 * mm),
        Paragraph("Commercial summary", styles["Heading2"]),
    ])
    discount_amount = scenario.subtotal * scenario.discount_percentage / Decimal("100")
    story.append(
        _table(
            [
                ["Commercial field", "Value"],
                ["Subtotal", format_money(scenario.subtotal, currency)],
                ["Discount percentage", f"{scenario.discount_percentage:,.2f}%"],
                ["Discount amount", format_money(discount_amount, currency)],
                ["Adjustment amount", format_money(scenario.adjustment_amount, currency)],
                ["Final total", format_money(scenario.total_value, currency)],
            ],
            [60 * mm, 60 * mm],
        )
    )

    story.extend((Spacer(1, 4 * mm), Paragraph("Unresolved decisions", styles["Heading3"])))
    if scenario.unresolved_decisions:
        for decision in scenario.unresolved_decisions:
            story.append(Paragraph(f"- {escape(decision)}", styles["Normal"]))
    else:
        story.append(Paragraph("None", styles["Normal"]))

    if scenario.comments or scenario.assumptions:
        story.append(Paragraph("Comments and assumptions", styles["Heading3"]))
        for value in [*scenario.comments, *scenario.assumptions]:
            story.append(Paragraph(f"- {escape(value)}", styles["Normal"]))

    document.build(story)
    return output.getvalue()


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
    detail_cell_style = ParagraphStyle(
        "DetailCell",
        parent=styles["BodyText"],
        fontSize=6,
        leading=7,
        spaceAfter=0,
        spaceBefore=0,
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

    comparison_data = [[
        "Scenario",
        "Base",
        "Copilot",
        "Additional / Retained",
        "Annual total",
        "Difference vs Renew As-Is",
    ]]
    for row in comparison.rows:
        comparison_data.append(
            [
                row.scenario_type.label,
                format_money(row.base_licences, currency),
                format_money(row.copilot, currency),
                format_money(row.additional_or_retained, currency),
                format_money(row.total_cost, currency),
                format_money(row.difference_from_renew_as_is, currency),
            ]
        )
    story.extend(
        [
            Paragraph("Scenario comparison", styles["Heading2"]),
            _table(
                comparison_data,
                [42 * mm, 35 * mm, 35 * mm, 48 * mm, 42 * mm, 42 * mm],
            ),
        ]
    )

    for scenario in scenarios:
        story.extend((PageBreak(), Paragraph(scenario.scenario_type.label, title_style)))
        detail = [[
            "SKU",
            "Action",
            "Existing",
            "Proposed",
            "Licence term",
            "Billing plan",
            "Renewal / expiration",
            "Unit price",
            "Extended",
            "Price status",
            "Replacement / target note",
        ]]
        for line in scenario.lines:
            if line.disposition.value in {"migrate", "included", "remove"}:
                price_status = "Not applicable"
            elif line.price_unavailable:
                price_status = (
                    "ELIGIBILITY REQUIRED"
                    if _promotion_eligibility_required(line)
                    else "PRICE UNAVAILABLE"
                )
            elif line.unit_price == 0:
                price_status = "No-charge price"
            else:
                price_status = "Priced"
            detail.append(
                [
                    Paragraph(escape(line.sku_title), detail_cell_style),
                    line.disposition.value,
                    str(line.existing_quantity),
                    str(line.proposed_quantity),
                    line.term_duration,
                    line.billing_plan,
                    _date(line.renewal_date or line.expiration_date),
                    format_money(line.unit_price, currency),
                    format_money(line.extended_price, currency),
                    Paragraph(escape(price_status), detail_cell_style),
                    Paragraph(escape(line.note or "-"), detail_cell_style),
                ]
            )
        discount_amount = (
            scenario.subtotal * scenario.discount_percentage / Decimal("100")
        )
        financial_summary = [
            ["Commercial field", "Value"],
            ["Subtotal", format_money(scenario.subtotal, currency)],
            ["Discount percentage", f"{scenario.discount_percentage:,.2f}%"],
            ["Discount amount", format_money(discount_amount, currency)],
            ["Adjustment amount", format_money(scenario.adjustment_amount, currency)],
            ["Final total", format_money(scenario.total_value, currency)],
        ]
        story.extend(
            [
                Spacer(1, 4 * mm),
                _table(
                    detail,
                    [
                        37 * mm,
                        17 * mm,
                        13 * mm,
                        14 * mm,
                        18 * mm,
                        20 * mm,
                        25 * mm,
                        23 * mm,
                        25 * mm,
                        27 * mm,
                        54 * mm,
                    ],
                    font_size=6,
                    cell_padding=2,
                ),
                Spacer(1, 4 * mm),
                Paragraph("Scenario commercial summary", styles["Heading3"]),
                _table(
                    financial_summary,
                    [55 * mm, 55 * mm],
                ),
                Spacer(1, 3 * mm),
                Paragraph("Unresolved decisions", styles["Heading3"]),
            ]
        )
        if scenario.unresolved_decisions:
            for decision in scenario.unresolved_decisions:
                story.append(Paragraph(f"• {escape(decision)}", styles["Normal"]))
        else:
            story.append(Paragraph("None", styles["Normal"]))
        if scenario.comments or scenario.assumptions:
            story.append(Paragraph("Comments and assumptions", styles["Heading3"]))
            for comment in [*scenario.comments, *scenario.assumptions]:
                story.append(Paragraph(f"• {comment}", styles["Normal"]))

    document.build(story)
    return output.getvalue()


def _table(
    data: list[list[object]],
    widths: list[float],
    *,
    font_size: int = 8,
    cell_padding: int = 4,
):
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
                ("FONTSIZE", (0, 0), (-1, -1), font_size),
                ("LEFTPADDING", (0, 0), (-1, -1), cell_padding),
                ("RIGHTPADDING", (0, 0), (-1, -1), cell_padding),
            ]
        )
    )
    return table


def _date(value: date | None) -> str:
    return value.isoformat() if value else "Not supplied"


def _promotion_eligibility_required(line: ScenarioLine) -> bool:
    return line.price_unavailable and "promotion eligibility confirmation required" in (
        line.note or ""
    ).casefold()
