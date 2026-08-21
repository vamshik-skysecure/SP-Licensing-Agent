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
        "*Captured licensing requirement*",
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
                    f"Term/billing: {item.term_duration or '-'} / "
                    f"{item.billing_plan or '-'}",
                    f"Renewal/expiration: {_date(date_value)}",
                )
            )
    if estate.pending_lines:
        lines.extend(("", "*Attention required*"))
        lines.append(
            f"{len(estate.pending_lines)} SKU matches require confirmation before pricing."
        )
    else:
        lines.extend(("", "All captured SKUs matched the approved catalogue."))
        if include_migration_review:
            lines.append("Confirm the requirement to continue.")
    return "\n".join(lines).strip()


def format_pending_matches(estate: LicenseEstate) -> str:
    lines = ["*Let’s confirm the exact Microsoft product*"]
    for pending in estate.pending_lines:
        lines.extend(
            (
                "",
                f"*{pending.line_id} — {pending.source_product_title}*",
                sku_clarification_question(
                    pending.source_product_title,
                    pending.candidates,
                ),
            )
        )
        for index, candidate in enumerate(pending.candidates, start=1):
            lines.append(f"{index}. {format_sku_candidate(candidate)}")
    if any(line.candidates for line in estate.pending_lines):
        lines.extend(
            (
                "",
                "Choose the matching option, reply with its number, or send the complete "
                "product name. If you are unsure, say so and I will help you narrow it down. "
                "Pricing will remain paused until you approve the exact product.",
            )
        )
    return "\n".join(lines)


def format_sku_candidate(candidate: object) -> str:
    """Display the exact title plus its Microsoft catalogue identity."""

    title = str(getattr(candidate, "sku_title", "")).strip()
    product_id = str(getattr(candidate, "product_id", "")).strip()
    sku_id = str(getattr(candidate, "sku_id", "")).strip()
    if product_id and sku_id:
        return f"{title} · Product ID: {product_id} · SKU ID: {sku_id}"
    if product_id:
        return f"{title} · Product ID: {product_id}"
    if sku_id:
        return f"{title} · SKU ID: {sku_id}"
    return title


def sku_clarification_question(source_title: str, candidates: list[object]) -> str:
    """Build a concise seller question without exposing matching internals."""

    if not candidates:
        return (
            f"I could not identify a reliable match for “{source_title}”. "
            "Send the product name shown on the invoice or a screenshot, and I will help "
            "identify it."
        )
    titles = [str(getattr(item, "sku_title", "")) for item in candidates]
    if len(titles) == 1:
        return (
            f"I found one close catalogue match for “{source_title}”, but I will not "
            "select it without your approval. Is this the product you mean?"
        )
    families = {product_family(title) for title in titles if title}
    if len(families) > 1:
        return (
            f"Several Microsoft product families use “{source_title}”. "
            "Which product family and plan do you mean?"
        )
    return f"I found more than one possible match for “{source_title}”. Which one do you mean?"


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
        ("azure", "Azure"),
        ("microsoft 365", "Microsoft 365"),
        ("office 365", "Office 365"),
        # Suite names must win over the "no Teams" qualifier. Otherwise Office
        # 365 E1 (no Teams) is incorrectly presented as a Teams-family product.
        ("teams", "Microsoft Teams"),
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
    report_title: str = "Customer Licence Estate",
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
        title=report_title,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "EstateTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17365D"),
    )
    story: list[object] = [
        Paragraph(escape(report_title), title_style),
        Spacer(1, 3 * mm),
        Paragraph(f"Report date: {report_date.isoformat()}", styles["Normal"]),
        Paragraph(
            f"Near-expiry review window: {near_expiry_days} days",
            styles["Normal"],
        ),
        Spacer(1, 4 * mm),
    ]
    if estate.seller_details:
        story.extend(
            [
                Paragraph("Seller-provided proposal details", styles["Heading2"]),
                _table(
                    [
                        ["Detail", "Value"],
                        *[
                            [
                                Paragraph(escape(item.label), styles["BodyText"]),
                                Paragraph(escape(item.value), styles["BodyText"]),
                            ]
                            for item in estate.seller_details
                        ],
                    ],
                    [55 * mm, 175 * mm],
                ),
                Spacer(1, 4 * mm),
            ]
        )
    story.extend(
        [
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
    )

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
            "Term / billing",
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
                f"{line.term_duration or '-'} / {line.billing_plan or '-'}",
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
                [
                    12 * mm,
                    32 * mm,
                    52 * mm,
                    13 * mm,
                    13 * mm,
                    13 * mm,
                    15 * mm,
                    31 * mm,
                    28 * mm,
                    43 * mm,
                ],
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
                f"*{row.scenario_type.label} · Revision {row.revision}*",
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
        Paragraph(
            f"Status: {scenario.status.value} | Term: {escape(scenario.term_duration)} | "
            f"Billing: {escape(scenario.billing_plan)} | "
            f"Segment: {escape(scenario.segment)}",
            styles["Normal"],
        ),
        Spacer(1, 5 * mm),
    ]
    if estate.seller_details:
        story.extend(
            [
                Paragraph("Proposal details", styles["Heading2"]),
                _table(
                    [
                        ["Detail", "Value"],
                        *[[item.label, item.value] for item in estate.seller_details],
                    ],
                    [55 * mm, 175 * mm],
                ),
                Spacer(1, 4 * mm),
            ]
        )
    story.append(Paragraph("Proposed licence configuration", styles["Heading2"]))

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


def render_simple_commercial_pdf(
    estate: LicenseEstate,
    current: CommercialScenario,
    revised: CommercialScenario | None = None,
    *,
    currency: str = "INR",
    pricing_source: str = "Applicable annual licence price",
) -> bytes:
    """Customer-facing v1 PDF that intentionally omits internal commercial fields."""

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer

    output = io.BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=10 * mm,
        leftMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
        title="Microsoft Licensing Renewal Proposal",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "SimpleCommercialTitle",
        parent=styles["Title"],
        alignment=TA_CENTER,
        textColor=colors.HexColor("#17365D"),
    )
    cell_style = ParagraphStyle(
        "SimpleCommercialCell",
        parent=styles["BodyText"],
        fontSize=7,
        leading=8,
    )
    story: list[object] = [
        Paragraph("Microsoft Licensing Renewal Proposal", title_style),
        Spacer(1, 3 * mm),
        Paragraph(
            "This proposal reflects the complete requirement confirmed by the seller before "
            "pricing. Only supplied requirement details and matched annual licence data are "
            "included.",
            styles["Normal"],
        ),
        Paragraph(f"Commercial basis: {escape(pricing_source)}", styles["Normal"]),
        Spacer(1, 5 * mm),
        Paragraph("Executive summary", styles["Heading2"]),
        _table(
            [
                ["Confirmed SKU lines", "Confirmed licence quantity", "Renew As-Is annual value"],
                [
                    str(len(current.lines)),
                    f"{sum(line.proposed_quantity for line in current.lines):,}",
                    format_money(current.total_value, currency),
                ],
            ],
            [55 * mm, 65 * mm, 70 * mm],
        ),
        Spacer(1, 5 * mm),
    ]
    if estate.seller_details:
        story.extend(
            [
                Paragraph("Proposal details", styles["Heading2"]),
                _table(
                    [
                        ["Detail", "Value"],
                        *[
                            [
                                Paragraph(escape(item.label), styles["BodyText"]),
                                Paragraph(escape(item.value), styles["BodyText"]),
                            ]
                            for item in estate.seller_details
                        ],
                    ],
                    [55 * mm, 175 * mm],
                ),
                Spacer(1, 4 * mm),
            ]
        )

    def add_configuration(title: str, scenario: CommercialScenario) -> None:
        story.append(Paragraph(title, styles["Heading2"]))
        data: list[list[object]] = [[
            "Line",
            "SKU",
            "Quantity",
            "Term / billing",
            "Renewal / expiration",
            "Unit price",
            "Total price",
            "Price status",
        ]]
        for line in scenario.lines:
            status = (
                "PRICE UNAVAILABLE - excluded from total"
                if line.price_unavailable
                else "Priced"
            )
            data.append([
                line.line_id,
                Paragraph(escape(line.sku_title), cell_style),
                str(line.proposed_quantity),
                f"{line.term_duration} / {line.billing_plan}",
                _date(line.renewal_date or line.expiration_date),
                format_money(line.unit_price, currency),
                format_money(line.extended_price, currency),
                Paragraph(escape(status), cell_style),
            ])
        story.extend([
            _table(
                data,
                [13 * mm, 70 * mm, 18 * mm, 34 * mm, 30 * mm, 28 * mm, 30 * mm, 50 * mm],
                font_size=7,
                cell_padding=3,
            ),
            Spacer(1, 3 * mm),
            _table(
                [["Overall requirement value", format_money(scenario.total_value, currency)]],
                [75 * mm, 55 * mm],
            ),
        ])
        if scenario.unresolved_decisions:
            story.append(Paragraph("Pricing warnings", styles["Heading3"]))
            for decision in scenario.unresolved_decisions:
                story.append(Paragraph(f"- {escape(decision)}", styles["Normal"]))
        if scenario.comments:
            story.append(Paragraph("Seller-provided proposal notes", styles["Heading3"]))
            for comment in scenario.comments:
                story.append(Paragraph(f"- {escape(comment)}", styles["Normal"]))

    add_configuration("Confirmed Renew As-Is configuration", current)
    if revised is not None:
        story.extend([PageBreak()])
        add_configuration("Seller-requested revised configuration", revised)
        difference = revised.total_value - current.total_value
        story.extend([
            Spacer(1, 4 * mm),
            Paragraph("Commercial comparison", styles["Heading2"]),
            _table(
                [
                    ["Configuration", "Value"],
                    ["Confirmed Renew As-Is", format_money(current.total_value, currency)],
                    [
                        "Seller-requested revised",
                        format_money(revised.total_value, currency),
                    ],
                    ["Difference", _signed_money(difference, currency)],
                ],
                [80 * mm, 60 * mm],
            ),
            Spacer(1, 4 * mm),
            Paragraph("Replacement / addition details", styles["Heading3"]),
        ])
        for change in _simple_configuration_changes(current, revised):
            story.append(Paragraph(f"- {escape(change)}", styles["Normal"]))

    document.build(story)
    return output.getvalue()


def _signed_money(value: Decimal, currency: str) -> str:
    if value > 0:
        return f"+{format_money(value, currency)}"
    if value < 0:
        return f"-{format_money(abs(value), currency)}"
    return format_money(value, currency)


def _simple_configuration_changes(
    current: CommercialScenario,
    revised: CommercialScenario,
) -> list[str]:
    current_by_source = {
        (line.source_line_id or line.line_id): line for line in current.lines
    }
    revised_by_source = {
        (line.source_line_id or line.line_id): line for line in revised.lines
    }
    changes: list[str] = []
    consumed_additions: set[str] = set()
    for key, old in current_by_source.items():
        new = revised_by_source.get(key)
        if new is None or new.proposed_quantity == 0:
            replacement_prefix = "Replaced by seller with "
            if new is not None and (new.note or "").startswith(replacement_prefix):
                target_title = (new.note or "")[len(replacement_prefix) :]
                added = next(
                    (
                        (added_key, item)
                        for added_key, item in revised_by_source.items()
                        if added_key not in current_by_source
                        and item.sku_title.casefold() == target_title.casefold()
                        and item.proposed_quantity > 0
                    ),
                    None,
                )
                if added is not None:
                    consumed_additions.add(added[0])
                    changes.append(
                        f"Replaced {old.sku_title} with {added[1].sku_title} "
                        f"({added[1].proposed_quantity} licences)."
                    )
                    continue
            changes.append(f"Removed {old.sku_title} ({old.proposed_quantity} licences).")
        elif (old.product_id, old.sku_id) != (new.product_id, new.sku_id):
            changes.append(
                f"Replaced {old.sku_title} with {new.sku_title} "
                f"({new.proposed_quantity} licences)."
            )
        elif old.proposed_quantity != new.proposed_quantity:
            changes.append(
                f"Changed {old.sku_title} quantity from {old.proposed_quantity} "
                f"to {new.proposed_quantity}."
            )
    for key, new in revised_by_source.items():
        if (
            key not in current_by_source
            and key not in consumed_additions
            and new.proposed_quantity > 0
        ):
            changes.append(f"Added {new.sku_title} ({new.proposed_quantity} licences).")
    return changes or ["No SKU or quantity changes were made."]


def render_comparison_pdf(
    estate: LicenseEstate,
    scenarios: list[CommercialScenario],
    comparison: CommercialComparison,
    *,
    currency: str = "INR",
    include_internal_commercial_fields: bool = True,
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
        title="Microsoft Licensing Annual Options Comparison",
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
        Paragraph("Microsoft Licensing Annual Options Comparison", title_style),
        Spacer(1, 5 * mm),
        Paragraph(
            f"Commercial observation: <b>{comparison.recommended_scenario.label}</b>",
            styles["Heading2"],
        ),
        Paragraph(escape(comparison.recommendation_rationale), styles["Normal"]),
        Paragraph(
            "This is a commercial comparison of seller-requested annual configurations. "
            "It is not a feature-fit, entitlement, migration, or bundling recommendation "
            "unless separately supported by official Microsoft documentation and confirmed "
            "by the seller.",
            styles["Normal"],
        ),
        Spacer(1, 5 * mm),
    ]
    if estate.seller_details:
        story.extend(
            [
                Paragraph("Proposal details", styles["Heading2"]),
                _table(
                    [
                        ["Detail", "Value"],
                        *[
                            [
                                Paragraph(escape(item.label), styles["BodyText"]),
                                Paragraph(escape(item.value), styles["BodyText"]),
                            ]
                            for item in estate.seller_details
                        ],
                    ],
                    [55 * mm, 175 * mm],
                ),
                Spacer(1, 4 * mm),
            ]
        )

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
            Paragraph("Confirmed licensing requirement", styles["Heading2"]),
            _table(estate_data, [42 * mm, 95 * mm, 20 * mm, 20 * mm, 20 * mm, 36 * mm]),
            Spacer(1, 6 * mm),
        ]
    )

    comparison_data = [[
        "Proposal",
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
        story.extend(
            (
                PageBreak(),
                Paragraph(
                    scenario.scenario_type.label,
                    title_style,
                ),
            )
        )
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
        if include_internal_commercial_fields:
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
        else:
            financial_summary = [
                ["Commercial field", "Value"],
                ["Overall annual value", format_money(scenario.total_value, currency)],
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
        visible_notes = [
            *scenario.comments,
            *(scenario.assumptions if include_internal_commercial_fields else []),
        ]
        if visible_notes:
            story.append(Paragraph("Proposal notes", styles["Heading3"]))
            for comment in visible_notes:
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
