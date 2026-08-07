from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from io import BytesIO
from math import ceil

from PIL import Image, ImageDraw, ImageFont

from .models import CommercialComparison, CommercialScenario, LicenseEstate, ScenarioLine
from .renderer import product_family


CANVAS_WIDTH = 1080
MARGIN = 48
TABLE_WIDTH = CANVAS_WIDTH - (2 * MARGIN)

NAVY = "#17365D"
BLUE = "#2F6FED"
PALE_BLUE = "#EAF2FF"
BACKGROUND = "#F4F7FB"
WHITE = "#FFFFFF"
TEXT = "#172033"
MUTED = "#5B677A"
BORDER = "#C9D5E5"
ALT_ROW = "#F8FAFD"
POSITIVE = "#18794E"
PALE_GREEN = "#E5F5EC"
WARNING = "#A15C00"


@dataclass(frozen=True)
class TableBlock:
    title: str
    headers: list[str]
    rows: list[list[str]]
    widths: list[int]
    alignments: list[str]
    highlight_rows: set[int] = field(default_factory=set)


def render_estate_table_images(
    estate: LicenseEstate,
    *,
    title: str = "Customer licence estate",
) -> list[bytes]:
    ordered = sorted(
        estate.lines,
        key=lambda line: (product_family(line.display_title), line.display_title.casefold()),
    )
    page_size = 8
    page_count = max(1, ceil(len(ordered) / page_size))
    images: list[bytes] = []
    for page_index in range(page_count):
        page_lines = ordered[page_index * page_size : (page_index + 1) * page_size]
        rows = [
            [
                line.line_id,
                product_family(line.display_title),
                line.display_title,
                str(line.total_licenses),
                line.term_duration or "-",
                _date(line.renewal_date or line.expiration_date),
            ]
            for line in page_lines
        ]
        pending = len(estate.pending_lines)
        callout = [
            f"Renewal quantity: {estate.total_renewal_quantity:,}",
            (
                "All SKU matches confirmed"
                if pending == 0
                else f"SKU confirmations required: {pending}"
            ),
        ]
        images.append(
            _render_report(
                title=title,
                subtitle=(
                    f"{len(estate.lines)} SKU lines | "
                    f"{sum(line.total_licenses for line in estate.lines):,} licences | "
                    f"Page {page_index + 1} of {page_count}"
                ),
                blocks=[
                    TableBlock(
                        title="Licences grouped by product family",
                        headers=[
                            "ID",
                            "Family",
                            "Product",
                            "Qty",
                            "Term",
                            "Renewal / expiry",
                        ],
                        rows=rows,
                        widths=[72, 150, 330, 90, 100, 242],
                        alignments=[
                            "center",
                            "left",
                            "left",
                            "right",
                            "left",
                            "center",
                        ],
                    )
                ],
                callout_title="Estate summary",
                callout_lines=callout,
            )
        )
    return images


def render_scenario_table_images(
    scenario: CommercialScenario,
    currency: str = "INR",
) -> list[bytes]:
    page_size = 6
    page_count = max(1, ceil(len(scenario.lines) / page_size))
    images: list[bytes] = []
    discount_amount = (
        scenario.subtotal * scenario.discount_percentage / Decimal("100")
    )
    for page_index in range(page_count):
        page_lines = scenario.lines[
            page_index * page_size : (page_index + 1) * page_size
        ]
        configuration_rows = [
            [
                line.line_id,
                line.sku_title,
                _action(line),
                (
                    str(line.proposed_quantity)
                    if line.existing_quantity == line.proposed_quantity
                    else f"{line.existing_quantity} / {line.proposed_quantity}"
                ),
            ]
            for line in page_lines
        ]
        pricing_rows = [
            [
                line.line_id,
                _unit_price(line, currency),
                _line_total(line, currency),
                _date(line.renewal_date or line.expiration_date),
            ]
            for line in page_lines
        ]
        callout = []
        if page_index == page_count - 1:
            callout = [
                f"Subtotal: {_money(scenario.subtotal, currency)}",
                (
                    f"Discount: {scenario.discount_percentage:,.2f}% "
                    f"(-{_money(discount_amount, currency)})"
                ),
                f"Adjustment: {_money(scenario.adjustment_amount, currency)}",
                f"Final annual total: {_money(scenario.total_value, currency)}",
            ]
        images.append(
            _render_report(
                title=f"{scenario.scenario_type.label} proposal",
                subtitle=(
                    f"Revision {scenario.revision} | {scenario.term_duration} | "
                    f"{scenario.billing_plan} | Page {page_index + 1} of {page_count}"
                ),
                blocks=[
                    TableBlock(
                        title="Proposed licence configuration",
                        headers=["ID", "Product", "Action", "Existing / proposed"],
                        rows=configuration_rows,
                        widths=[80, 440, 240, 224],
                        alignments=["center", "left", "left", "center"],
                    ),
                    TableBlock(
                        title=f"Annual pricing ({currency})",
                        headers=["ID", "Unit price", "Line total", "Renewal / expiry"],
                        rows=pricing_rows,
                        widths=[80, 260, 300, 344],
                        alignments=["center", "right", "right", "center"],
                    ),
                ],
                callout_title="Commercial summary" if callout else None,
                callout_lines=callout,
                emphasis_line_index=3 if callout else None,
            )
        )
    return images


def render_simple_pricing_table_images(
    scenario: CommercialScenario,
    *,
    title: str,
    currency: str = "INR",
    pricing_source: str = "Applicable annual licence price",
) -> list[bytes]:
    """Mobile-first v1 price table with no internal discount or margin fields."""

    page_size = 6
    page_count = max(1, ceil(len(scenario.lines) / page_size))
    images: list[bytes] = []
    unavailable = sum(line.price_unavailable for line in scenario.lines)
    for page_index in range(page_count):
        page_lines = scenario.lines[
            page_index * page_size : (page_index + 1) * page_size
        ]
        rows = [
            [
                line.line_id,
                line.sku_title,
                str(line.proposed_quantity),
                line.term_duration,
                _unit_price(line, currency, include_currency=False),
                _line_total(line, currency, include_currency=False),
            ]
            for line in page_lines
        ]
        callout: list[str] = []
        if page_index == page_count - 1:
            callout = [
                f"Overall requirement value: {_money(scenario.total_value, currency)}",
                f"Pricing basis: {pricing_source}",
            ]
            if unavailable:
                callout.append(
                    f"Warning: {unavailable} line(s) have no applicable price and are excluded."
                )
        images.append(
            _render_report(
                title=title,
                subtitle=(
                    f"{len(scenario.lines)} SKU lines | Amounts in {currency} | "
                    f"Page {page_index + 1} of {page_count}"
                ),
                blocks=[
                    TableBlock(
                        title="Commercial requirement",
                        headers=["ID", "SKU", "Qty", "Term", "Unit price", "Total"],
                        rows=rows,
                        widths=[72, 352, 90, 100, 170, 200],
                        alignments=["center", "left", "right", "left", "right", "right"],
                    )
                ],
                callout_title="Final annual value" if callout else None,
                callout_lines=callout,
                emphasis_line_index=0 if callout else None,
            )
        )
    return images


def render_simple_comparison_table_images(
    current: CommercialScenario,
    revised: CommercialScenario,
    *,
    currency: str = "INR",
    pricing_source: str = "Applicable annual licence price",
) -> list[bytes]:
    difference = revised.total_value - current.total_value
    changes = _configuration_changes(current, revised)
    rows = [
        ["Confirmed Renew As-Is", _money(current.total_value, currency)],
        ["Selected proposal", _money(revised.total_value, currency)],
        ["Commercial difference", _difference(difference, currency)],
    ]
    change_rows = [[str(index), change] for index, change in enumerate(changes, start=1)]
    return [
        _render_report(
            title="Renew As-Is vs selected proposal",
            subtitle=f"Annual licensing values | Amounts in {currency}",
            blocks=[
                TableBlock(
                    title="Commercial value",
                    headers=["Configuration", "Value"],
                    rows=rows,
                    widths=[500, 484],
                    alignments=["left", "right"],
                ),
                TableBlock(
                    title="What changed",
                    headers=["#", "Replacement / addition / quantity change"],
                    rows=change_rows,
                    widths=[70, 914],
                    alignments=["center", "left"],
                ),
            ],
            callout_title="Pricing basis",
            callout_lines=[
                f"Basis: {pricing_source}",
                "Exact line pricing is recalculated deterministically after each accepted change.",
                "Promotion and eligibility rule-sheet logic is not applied until business approval.",
            ],
        )
    ]


def _configuration_changes(
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
            continue
        if (old.product_id, old.sku_id) != (new.product_id, new.sku_id):
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
    return changes or ["No SKU or quantity changes; both proposal values are equal."]


def render_comparison_table_images(
    comparison: CommercialComparison,
    currency: str = "INR",
) -> list[bytes]:
    totals = [
        [
            _comparison_option(row),
            _money(row.total_cost, currency),
            _difference(row.difference_from_renew_as_is, currency),
        ]
        for row in comparison.rows
    ]
    components = [
        [
            _comparison_option(row),
            _amount(row.base_licences),
            _amount(row.copilot),
            _amount(row.additional_or_retained),
        ]
        for row in comparison.rows
    ]
    recommended_indexes = {
        index
        for index, row in enumerate(comparison.rows)
        if row.scenario_type == comparison.recommended_scenario
    }
    recommended_row = next(
        row
        for row in comparison.rows
        if row.scenario_type == comparison.recommended_scenario
    )
    return [
        _render_report(
            title="Annual commercial comparison",
            subtitle=f"One-year term | Annual billing | Amounts in {currency}",
            blocks=[
                TableBlock(
                    title="Annual totals",
                    headers=["Option", "Annual total", "Difference vs Renew As-Is"],
                    rows=totals,
                    widths=[230, 320, 434],
                    alignments=["left", "right", "left"],
                    highlight_rows=recommended_indexes,
                ),
                TableBlock(
                    title=f"Cost components ({currency})",
                    headers=["Option", "Base", "Copilot", "Additional / retained"],
                    rows=components,
                    widths=[190, 255, 220, 319],
                    alignments=["left", "right", "right", "right"],
                    highlight_rows=recommended_indexes,
                ),
            ],
            callout_title=f"Recommended: {comparison.recommended_scenario.label}",
            callout_lines=[
                f"Recommended annual value: {_money(recommended_row.total_cost, currency)}",
                comparison.recommendation_rationale,
                "No add-on bundle entitlement was assumed.",
            ],
            emphasis_line_index=0,
        )
    ]


def _render_report(
    *,
    title: str,
    subtitle: str,
    blocks: list[TableBlock],
    callout_title: str | None,
    callout_lines: list[str],
    emphasis_line_index: int | None = None,
) -> bytes:
    regular = _font(28)
    header_font = _font(26, bold=True)
    section_font = _font(31, bold=True)
    title_font = _font(43, bold=True)
    subtitle_font = _font(25)
    callout_title_font = _font(29, bold=True)
    callout_value_font = _font(34, bold=True)
    dummy = Image.new("RGB", (CANVAS_WIDTH, 200), WHITE)
    measure = ImageDraw.Draw(dummy)

    prepared: list[tuple[TableBlock, list[list[list[str]]], list[int]]] = []
    content_height = 182
    for block in blocks:
        wrapped, heights = _prepare_table(
            measure,
            block,
            regular=regular,
            header_font=header_font,
        )
        prepared.append((block, wrapped, heights))
        content_height += 58 + sum(heights) + 30

    callout_wrapped: list[tuple[str, ImageFont.ImageFont]] = []
    if callout_title and callout_lines:
        for index, value in enumerate(callout_lines):
            font = callout_value_font if index == emphasis_line_index else regular
            callout_wrapped.extend(
                (line, font) for line in _wrap(measure, value, font, TABLE_WIDTH - 52)
            )
        content_height += 66 + sum(
            _line_height(measure, font) for _, font in callout_wrapped
        ) + 48
    content_height += 42

    image = Image.new("RGB", (CANVAS_WIDTH, max(content_height, 560)), BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, CANVAS_WIDTH, 150), fill=NAVY)
    draw.text((MARGIN, 32), title, font=title_font, fill=WHITE)
    draw.text((MARGIN, 94), subtitle, font=subtitle_font, fill="#DDE8F8")

    y = 182
    for block, wrapped, heights in prepared:
        draw.text((MARGIN, y), block.title, font=section_font, fill=NAVY)
        y += 48
        y = _draw_table(
            draw,
            y,
            block,
            wrapped,
            heights,
            regular=regular,
            header_font=header_font,
        )
        y += 30

    if callout_title and callout_lines:
        box_height = 66 + sum(
            _line_height(draw, font) for _, font in callout_wrapped
        ) + 22
        emphasized = emphasis_line_index is not None
        draw.rounded_rectangle(
            (MARGIN, y, CANVAS_WIDTH - MARGIN, y + box_height),
            radius=18,
            fill=PALE_GREEN if emphasized else PALE_BLUE,
            outline="#9BCFB5" if emphasized else "#B8CEF2",
            width=3 if emphasized else 2,
        )
        draw.text(
            (MARGIN + 24, y + 18),
            callout_title,
            font=callout_title_font,
            fill=POSITIVE if emphasized else NAVY,
        )
        text_y = y + 62
        for line, font in callout_wrapped:
            draw.text((MARGIN + 24, text_y), line, font=font, fill=TEXT)
            text_y += _line_height(draw, font)

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()


def _prepare_table(
    draw: ImageDraw.ImageDraw,
    block: TableBlock,
    *,
    regular: ImageFont.ImageFont,
    header_font: ImageFont.ImageFont,
) -> tuple[list[list[list[str]]], list[int]]:
    wrapped: list[list[list[str]]] = []
    heights: list[int] = []
    for row_index, row in enumerate([block.headers, *block.rows]):
        font = header_font if row_index == 0 else regular
        row_cells = [
            _wrap(draw, str(value), font, width - 28)
            for value, width in zip(row, block.widths, strict=True)
        ]
        line_height = _line_height(draw, font)
        height = max(len(lines) for lines in row_cells) * line_height + 26
        wrapped.append(row_cells)
        heights.append(max(height, 64 if row_index == 0 else 68))
    return wrapped, heights


def _draw_table(
    draw: ImageDraw.ImageDraw,
    y: int,
    block: TableBlock,
    wrapped: list[list[list[str]]],
    heights: list[int],
    *,
    regular: ImageFont.ImageFont,
    header_font: ImageFont.ImageFont,
) -> int:
    for row_index, (cells, row_height) in enumerate(zip(wrapped, heights, strict=True)):
        x = MARGIN
        if row_index == 0:
            fill = NAVY
        elif row_index - 1 in block.highlight_rows:
            fill = PALE_GREEN
        else:
            fill = WHITE if row_index % 2 else ALT_ROW
        font = header_font if row_index == 0 else regular
        color = WHITE if row_index == 0 else TEXT
        for column_index, (lines, width) in enumerate(
            zip(cells, block.widths, strict=True)
        ):
            draw.rectangle(
                (x, y, x + width, y + row_height),
                fill=fill,
                outline=BORDER,
                width=2,
            )
            line_height = _line_height(draw, font)
            text_y = y + 13
            alignment = "center" if row_index == 0 else block.alignments[column_index]
            for line in lines:
                line_width = _text_width(draw, line, font)
                if alignment == "right":
                    text_x = x + width - 14 - line_width
                elif alignment == "center":
                    text_x = x + (width - line_width) / 2
                else:
                    text_x = x + 14
                draw.text((text_x, text_y), line, font=font, fill=color)
                text_y += line_height
            x += width
        y += row_height
    return y


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    normalized = " ".join(text.split()) or "-"
    words = normalized.split(" ")
    lines: list[str] = []
    current = ""
    for word in words:
        pieces = _split_word(draw, word, font, max_width)
        for piece in pieces:
            candidate = f"{current} {piece}".strip()
            if current and _text_width(draw, candidate, font) > max_width:
                lines.append(current)
                current = piece
            else:
                current = candidate
    if current:
        lines.append(current)
    return lines or ["-"]


def _split_word(
    draw: ImageDraw.ImageDraw,
    word: str,
    font: ImageFont.ImageFont,
    max_width: int,
) -> list[str]:
    if _text_width(draw, word, font) <= max_width:
        return [word]
    pieces: list[str] = []
    current = ""
    for character in word:
        candidate = current + character
        if current and _text_width(draw, candidate, font) > max_width:
            pieces.append(current)
            current = character
        else:
            current = candidate
    if current:
        pieces.append(current)
    return pieces


def _font(size: int, *, bold: bool = False) -> ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    try:
        return ImageFont.truetype(name, size=size)
    except OSError:
        return ImageFont.load_default(size=size)


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.ImageFont) -> int:
    box = draw.textbbox((0, 0), "Ag", font=font)
    return box[3] - box[1] + 9


def _text_width(
    draw: ImageDraw.ImageDraw,
    value: str,
    font: ImageFont.ImageFont,
) -> int:
    box = draw.textbbox((0, 0), value, font=font)
    return box[2] - box[0]


def _money(value: Decimal, currency: str) -> str:
    return f"{currency} {value:,.2f}"


def _comparison_option(row) -> str:
    return f"{row.scenario_type.label} · R{row.revision}"


def _amount(value: Decimal) -> str:
    return f"{value:,.2f}"


def _date(value: date | None) -> str:
    return value.isoformat() if value else "Not supplied"


def _action(line: ScenarioLine) -> str:
    if line.line_id == "BASE":
        return "Add target suite"
    if line.line_id == "COPILOT":
        return "Add separately"
    return {
        "retain": "Retain",
        "migrate": "Replace",
        "included": "Target line",
        "remove": "Remove",
        "needs_decision": "Decision required",
        "add": "Add",
    }[line.disposition.value]


def _unit_price(
    line: ScenarioLine,
    currency: str,
    *,
    include_currency: bool = True,
) -> str:
    if line.disposition.value in {"migrate", "included", "remove"}:
        return "N/A"
    if line.price_unavailable:
        return (
            "ELIGIBILITY REQUIRED"
            if _promotion_eligibility_required(line)
            else "UNAVAILABLE"
        )
    return _money(line.unit_price, currency) if include_currency else _amount(line.unit_price)


def _line_total(
    line: ScenarioLine,
    currency: str,
    *,
    include_currency: bool = True,
) -> str:
    if line.disposition.value in {"migrate", "included", "remove"}:
        return "N/A"
    if line.price_unavailable:
        return (
            "ELIGIBILITY REQUIRED"
            if _promotion_eligibility_required(line)
            else "UNAVAILABLE"
        )
    return (
        _money(line.extended_price, currency)
        if include_currency
        else _amount(line.extended_price)
    )


def _difference(value: Decimal, currency: str) -> str:
    if value == 0:
        return "Same as renewal"
    if value < 0:
        return f"{_money(abs(value), currency)} lower"
    return f"{_money(value, currency)} higher"


def _promotion_eligibility_required(line: ScenarioLine) -> bool:
    return line.price_unavailable and "promotion eligibility confirmation required" in (
        line.note or ""
    ).casefold()
