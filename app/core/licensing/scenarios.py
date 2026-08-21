from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, TypeAlias
from uuid import uuid4

from .migration_rules import MigrationSeedCatalog
from .models import (
    CommercialComparison,
    CommercialScenario,
    ComparisonRow,
    LicenseEstate,
    MigrationDisposition,
    RateCardItem,
    ScenarioLine,
    ScenarioStatus,
    ScenarioType,
)
from .rate_card import RateCardCatalog, normalize_product_title


CENT = Decimal("0.01")


class ScenarioError(ValueError):
    pass


PriceBasis: TypeAlias = Literal[
    "partner_best_offer",
    "marketplace",
    "distributor_expected",
]


def price_unavailability_message(
    item: RateCardItem,
    promo_eligible: bool,
    price_basis: PriceBasis = "partner_best_offer",
) -> str:
    if price_basis in {"marketplace", "distributor_expected"}:
        return "The current licence price is blank; the line is not included in the total."
    if (
        not promo_eligible
        and item.initial_quote_with_promo_available
        and not item.initial_quote_without_promo_available
    ):
        return (
            "Promotion eligibility confirmation required: the pricing data has a "
            "promotional quote but no standard quote."
        )
    return "The selected quote is blank; price is unavailable."


def price_unavailability_decision(
    line_id: str,
    item: RateCardItem,
    promo_eligible: bool,
    price_basis: PriceBasis = "partner_best_offer",
) -> str:
    if price_basis in {"marketplace", "distributor_expected"}:
        return f"{line_id}: the current licence price is unavailable."
    if (
        not promo_eligible
        and item.initial_quote_with_promo_available
        and not item.initial_quote_without_promo_available
    ):
        return f"{line_id}: confirm promotion eligibility before pricing."
    return f"{line_id}: price is unavailable in the current pricing data."


@dataclass(frozen=True)
class SkuSelector:
    sku_title: str
    product_id: str | None = None
    sku_id: str | None = None


@dataclass(frozen=True)
class ScenarioDefinition:
    base_title: str
    includes_standalone_copilot: bool = False


# These names are the four options required by the use case. Product IDs, SKU IDs,
# prices, and commercial variants are deliberately resolved from the Outcome Sheet.
SCENARIO_DEFINITIONS: dict[ScenarioType, ScenarioDefinition] = {
    ScenarioType.ME3_COPILOT: ScenarioDefinition(
        base_title="Microsoft 365 E3",
        includes_standalone_copilot=True,
    ),
    ScenarioType.ME5_COPILOT: ScenarioDefinition(
        base_title="Microsoft 365 E5 without Audio Conferencing",
        includes_standalone_copilot=True,
    ),
    ScenarioType.ME7: ScenarioDefinition(
        base_title="Microsoft 365 E7 without Audio Conferencing",
    ),
}
COPILOT_TITLE = "Microsoft 365 Copilot"


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


class ScenarioEngine:
    def __init__(
        self,
        migration_seeds: MigrationSeedCatalog | None = None,
        *,
        apply_bundle_rules: bool = True,
        price_basis: PriceBasis = "partner_best_offer",
    ) -> None:
        self._migration_seeds = migration_seeds
        self._apply_bundle_rules = apply_bundle_rules
        self._price_basis = price_basis

    def validate_catalog(
        self,
        catalog: RateCardCatalog,
        *,
        term_duration: str,
        billing_plan: str,
        segment: str,
    ) -> None:
        """Fail readiness when the workbook cannot price every required option."""
        titles = {
            definition.base_title for definition in SCENARIO_DEFINITIONS.values()
        }
        titles.add(COPILOT_TITLE)
        for title in sorted(titles):
            selector = self._exact_selector(catalog, title)
            item = self._select_price(
                catalog,
                selector,
                term_duration,
                billing_plan,
                segment,
            )
            if not self._price_available(item):
                raise ScenarioError(
                    f"The required current price is blank for {title!r}."
                )

    def build(
        self,
        *,
        estate: LicenseEstate,
        scenario_type: ScenarioType,
        catalog: RateCardCatalog,
        term_duration: str,
        billing_plan: str,
        segment: str,
        promo_eligible: bool = False,
        base_quantity: int | None = None,
        copilot_quantity: int | None = None,
    ) -> CommercialScenario:
        if estate.pending_lines:
            raise ScenarioError("Confirm all SKU matches before building a scenario.")
        if scenario_type == ScenarioType.RENEW_AS_IS:
            return self._build_renew(
                estate,
                catalog,
                term_duration,
                billing_plan,
                segment,
                promo_eligible,
            )
        return self._build_target(
            estate,
            scenario_type,
            catalog,
            term_duration,
            billing_plan,
            segment,
            promo_eligible,
            base_quantity,
            copilot_quantity,
        )

    def edit_quantity(
        self,
        scenario: CommercialScenario,
        line_id: str,
        quantity: int,
    ) -> CommercialScenario:
        if quantity < 0:
            raise ScenarioError("Quantity cannot be negative.")
        found = False
        lines: list[ScenarioLine] = []
        copilot_quantity = scenario.copilot_quantity
        for line in scenario.lines:
            if line.line_id != line_id:
                lines.append(line)
                continue
            found = True
            updated = line.model_copy(
                update={
                    "proposed_quantity": quantity,
                    "extended_price": money(line.unit_price * quantity),
                    "decision_required": line.price_unavailable,
                }
            )
            if line.category == "copilot":
                copilot_quantity = quantity
            lines.append(updated)
        if not found:
            raise ScenarioError(f"Scenario line {line_id!r} was not found.")
        return self._recalculate(
            scenario,
            lines,
            copilot_quantity=copilot_quantity,
        )

    def set_discount(
        self,
        scenario: CommercialScenario,
        percentage: Decimal,
    ) -> CommercialScenario:
        if percentage < 0 or percentage > 100:
            raise ScenarioError("Discount percentage must be between 0 and 100.")
        updated = scenario.model_copy(
            update={"discount_percentage": percentage}
        )
        return self._recalculate(updated, list(updated.lines))

    def set_adjustment(
        self,
        scenario: CommercialScenario,
        amount: Decimal,
    ) -> CommercialScenario:
        updated = scenario.model_copy(update={"adjustment_amount": money(amount)})
        return self._recalculate(updated, list(updated.lines))

    def reconfigure_pricing(
        self,
        scenario: CommercialScenario,
        catalog: RateCardCatalog,
        *,
        term_duration: str | None = None,
        billing_plan: str | None = None,
        segment: str | None = None,
        promo_eligible: bool | None = None,
    ) -> CommercialScenario:
        target_term = (term_duration or scenario.term_duration).strip()
        target_billing = (billing_plan or scenario.billing_plan).strip()
        target_segment = (segment or scenario.segment).strip()
        target_promo = (
            scenario.promo_eligible if promo_eligible is None else promo_eligible
        )
        if not target_term or not target_billing or not target_segment:
            raise ScenarioError("Term, billing plan, and segment cannot be empty.")
        if segment is not None and segment.casefold() != scenario.segment.casefold():
            available_segments = {
                item.segment.casefold()
                for item in catalog.items
                if item.segment
            }
            if not available_segments:
                raise ScenarioError(
                    "The current pricing data has no Segment column; segment cannot be changed."
                )
            if target_segment.casefold() not in available_segments:
                choices = ", ".join(sorted(available_segments))
                raise ScenarioError(
                    f"Segment {target_segment!r} is unavailable. Available: {choices}."
                )

        lines: list[ScenarioLine] = []
        for line in scenario.lines:
            item = self._select_price(
                catalog,
                SkuSelector(
                    product_id=line.product_id,
                    sku_id=line.sku_id,
                    sku_title=line.sku_title,
                ),
                target_term,
                target_billing,
                target_segment,
            )
            unit, price_unavailable = self._unit_price(item, target_promo)
            unavailable_note = price_unavailability_message(
                item, target_promo, self._price_basis
            )
            existing_note = (line.note or "").replace(
                "Selected Outcome Sheet quote is blank; price is unavailable.",
                "",
            ).replace(
                "The selected quote is blank; price is unavailable.",
                "",
            ).replace(
                "Promotion eligibility confirmation required: the pricing data has a "
                "promotional quote but no standard quote.",
                "",
            ).strip()
            note = (
                f"{existing_note} {unavailable_note}".strip()
                if price_unavailable
                else (existing_note or None)
            )
            lines.append(
                line.model_copy(
                    update={
                        "unit_price": unit,
                        "extended_price": money(unit * line.proposed_quantity),
                        "price_unavailable": price_unavailable,
                        "decision_required": (
                            (line.decision_required and not line.price_unavailable)
                            or price_unavailable
                        ),
                        "term_duration": item.term_duration,
                        "billing_plan": item.billing_plan,
                        "note": note,
                    }
                )
            )
        updated = scenario.model_copy(
            update={
                "term_duration": target_term,
                "billing_plan": target_billing,
                "segment": target_segment,
                "promo_eligible": target_promo,
                "lines": lines,
            }
        )
        return self._recalculate(updated, lines)

    def set_disposition(
        self,
        scenario: CommercialScenario,
        line_id: str,
        disposition: MigrationDisposition,
    ) -> CommercialScenario:
        found = False
        lines: list[ScenarioLine] = []
        for line in scenario.lines:
            if line.line_id != line_id:
                lines.append(line)
                continue
            found = True
            proposed = line.proposed_quantity
            if disposition in {
                MigrationDisposition.REMOVE,
                MigrationDisposition.MIGRATE,
                MigrationDisposition.INCLUDED,
            }:
                proposed = 0
            elif proposed == 0:
                proposed = line.existing_quantity
            decision_required = (
                line.price_unavailable
                and disposition
                in {MigrationDisposition.RETAIN, MigrationDisposition.ADD}
            )
            lines.append(
                line.model_copy(
                    update={
                        "disposition": disposition,
                        "proposed_quantity": proposed,
                        "extended_price": money(line.unit_price * proposed),
                        "decision_required": decision_required,
                        "note": (
                            "Seller override; price unavailable"
                            if decision_required
                            else "Seller override"
                        ),
                    }
                )
            )
        if not found:
            raise ScenarioError(f"Scenario line {line_id!r} was not found.")
        return self._recalculate(scenario, lines)

    def add_sku(
        self,
        scenario: CommercialScenario,
        *,
        product_query: str,
        quantity: int,
        catalog: RateCardCatalog,
        selector: SkuSelector | None = None,
    ) -> CommercialScenario:
        if quantity <= 0:
            raise ScenarioError("Added SKU quantity must be greater than zero.")
        selector = selector or SkuSelector(sku_title=product_query)
        item = self._select_price(
            catalog,
            selector,
            scenario.term_duration,
            scenario.billing_plan,
            scenario.segment,
        )
        unit, price_unavailable = self._unit_price(item, scenario.promo_eligible)
        line = ScenarioLine(
            line_id=f"A{len(scenario.lines) + 1}",
            sku_title=item.sku_title,
            product_id=item.product_id,
            sku_id=item.sku_id,
            existing_quantity=0,
            proposed_quantity=quantity,
            unit_price=unit,
            extended_price=money(unit * quantity),
            price_unavailable=price_unavailable,
            term_duration=item.term_duration,
            billing_plan=item.billing_plan,
            category="additional",
            disposition=MigrationDisposition.ADD,
            decision_required=price_unavailable,
            note=(
                f"Added by seller. {price_unavailability_message(item, scenario.promo_eligible, self._price_basis)}"
                if price_unavailable
                else "Added by seller"
            ),
        )
        return self._recalculate(scenario, [*scenario.lines, line])

    def remove_sku(
        self,
        scenario: CommercialScenario,
        line_id: str,
    ) -> CommercialScenario:
        return self.set_disposition(
            scenario,
            line_id,
            MigrationDisposition.REMOVE,
        )

    def replace_sku(
        self,
        scenario: CommercialScenario,
        *,
        line_id: str,
        product_query: str,
        quantity: int,
        catalog: RateCardCatalog,
        selector: SkuSelector | None = None,
    ) -> CommercialScenario:
        removed = self.set_disposition(
            scenario,
            line_id,
            MigrationDisposition.REMOVE,
        )
        replaced = self.add_sku(
            removed,
            product_query=product_query,
            quantity=quantity,
            catalog=catalog,
            selector=selector,
        )
        return replaced.model_copy(
            update={
                "lines": [
                    line.model_copy(
                        update={"note": f"Replaced by seller with {product_query}"}
                    )
                    if line.line_id == line_id
                    else line
                    for line in replaced.lines
                ]
            }
        )

    def add_comment(
        self,
        scenario: CommercialScenario,
        comment: str,
    ) -> CommercialScenario:
        cleaned = comment.strip()
        if not cleaned:
            raise ScenarioError("Comment cannot be empty.")
        if len(cleaned) > 1_000:
            raise ScenarioError("Seller comments are limited to 1,000 characters.")
        if len(scenario.comments) >= 20:
            raise ScenarioError(
                "This proposal already contains the maximum of 20 seller comments."
            )
        return scenario.model_copy(
            update={
                "comments": [*scenario.comments, cleaned],
                "revision": scenario.revision + 1,
                "updated_at": datetime.now(UTC),
            }
        )

    def finalize(self, scenario: CommercialScenario) -> CommercialScenario:
        pending = [line.line_id for line in scenario.lines if line.decision_required]
        if pending:
            raise ScenarioError(
                "Resolve seller decisions or unavailable prices before finalizing: "
                + ", ".join(pending)
            )
        return scenario.model_copy(
            update={
                "status": ScenarioStatus.FINAL,
                "unresolved_decisions": [],
                "revision": scenario.revision + 1,
                "updated_at": datetime.now(UTC),
            }
        )

    def comparison(
        self,
        thread_id: str,
        scenarios: list[CommercialScenario],
    ) -> CommercialComparison:
        if not scenarios:
            raise ScenarioError("At least one scenario is required for comparison.")
        rows: list[ComparisonRow] = []
        renewal_total = next(
            (
                scenario.total_value
                for scenario in scenarios
                if scenario.scenario_type == ScenarioType.RENEW_AS_IS
            ),
            scenarios[0].total_value,
        )
        for scenario in scenarios:
            base = sum(
                (line.extended_price for line in scenario.lines if line.category == "base"),
                Decimal("0"),
            )
            copilot = sum(
                (
                    line.extended_price
                    for line in scenario.lines
                    if line.category == "copilot"
                ),
                Decimal("0"),
            )
            additional = scenario.subtotal - base - copilot
            rows.append(
                ComparisonRow(
                    scenario_type=scenario.scenario_type,
                    revision=scenario.revision,
                    base_licences=money(base),
                    copilot=money(copilot),
                    additional_or_retained=money(additional),
                    total_cost=scenario.total_value,
                    difference_from_renew_as_is=money(
                        scenario.total_value - renewal_total
                    ),
                )
            )
        scenario_order = {scenario: index for index, scenario in enumerate(ScenarioType)}
        recommended = min(
            scenarios,
            key=lambda scenario: (
                sum(line.decision_required for line in scenario.lines),
                scenario.total_value,
                scenario_order[scenario.scenario_type],
            ),
        )
        decision_count = sum(
            line.decision_required for line in recommended.lines
        )
        if decision_count == 0:
            rationale = (
                f"{recommended.scenario_type.label} has no unresolved seller decisions and "
                "the lowest total among equally review-ready options."
            )
        else:
            rationale = (
                f"{recommended.scenario_type.label} has the fewest unresolved seller "
                f"decisions ({decision_count}) and the lowest total at that review level."
            )
        return CommercialComparison(
            thread_id=thread_id,
            rows=rows,
            recommended_scenario=recommended.scenario_type,
            recommendation_rationale=rationale,
        )

    def _build_renew(
        self,
        estate: LicenseEstate,
        catalog: RateCardCatalog,
        term_duration: str,
        billing_plan: str,
        segment: str,
        promo_eligible: bool,
    ) -> CommercialScenario:
        lines: list[ScenarioLine] = []
        unresolved: list[str] = []
        base_keys = self._core_suite_keys(catalog)
        copilot_key: tuple[str, str] | None = None
        try:
            copilot_selector = self._exact_selector(catalog, COPILOT_TITLE)
            copilot_key = self._identity_key(
                copilot_selector.product_id,
                copilot_selector.sku_id,
            )
        except ScenarioError:
            pass
        for source in estate.lines:
            line_term = source.term_duration or term_duration
            line_billing = source.billing_plan or billing_plan
            try:
                item = self._select_price(
                    catalog,
                    SkuSelector(
                        product_id=source.product_id,
                        sku_id=source.sku_id,
                        sku_title=source.display_title,
                    ),
                    line_term,
                    line_billing,
                    segment,
                )
                unit, price_unavailable = self._unit_price(item, promo_eligible)
                note = (
                    price_unavailability_message(item, promo_eligible, self._price_basis)
                    if price_unavailable
                    else None
                )
                if price_unavailable:
                    unresolved.append(
                        price_unavailability_decision(
                            source.line_id,
                            item,
                            promo_eligible,
                            self._price_basis,
                        )
                    )
            except ScenarioError as error:
                item = None
                unit = Decimal("0")
                price_unavailable = True
                note = str(error)
                unresolved.append(f"{source.line_id}: {error}")
            source_key = self._identity_key(source.product_id, source.sku_id)
            if source_key in base_keys:
                category = "base"
            elif copilot_key is not None and source_key == copilot_key:
                category = "copilot"
            else:
                category = "additional"
            lines.append(
                ScenarioLine(
                    line_id=source.line_id,
                    source_line_id=source.line_id,
                    product_id=source.product_id,
                    sku_id=source.sku_id,
                    sku_title=source.display_title,
                    existing_quantity=source.renewal_quantity,
                    proposed_quantity=source.renewal_quantity,
                    unit_price=unit,
                    extended_price=money(unit * source.renewal_quantity),
                    price_unavailable=price_unavailable,
                    term_duration=item.term_duration if item else line_term,
                    billing_plan=item.billing_plan if item else line_billing,
                    expiration_date=source.expiration_date,
                    renewal_date=source.renewal_date,
                    category=category,
                    disposition=MigrationDisposition.RETAIN,
                    decision_required=price_unavailable,
                    note=note,
                )
            )
        return self._new_scenario(
            estate,
            ScenarioType.RENEW_AS_IS,
            lines,
            term_duration,
            billing_plan,
            segment,
            promo_eligible,
            0,
            unresolved,
            [],
        )

    def _build_target(
        self,
        estate: LicenseEstate,
        scenario_type: ScenarioType,
        catalog: RateCardCatalog,
        term_duration: str,
        billing_plan: str,
        segment: str,
        promo_eligible: bool,
        base_quantity: int | None,
        copilot_quantity: int | None,
    ) -> CommercialScenario:
        definition = SCENARIO_DEFINITIONS.get(scenario_type)
        if definition is None:
            raise ScenarioError(f"No definition exists for {scenario_type.label}.")

        target_selector = self._exact_selector(catalog, definition.base_title)
        target_key = self._identity_key(
            target_selector.product_id,
            target_selector.sku_id,
        )
        core_suite_keys = self._core_suite_keys(catalog)

        copilot_selector = (
            self._exact_selector(catalog, COPILOT_TITLE)
            if definition.includes_standalone_copilot
            else None
        )
        copilot_key = (
            self._identity_key(copilot_selector.product_id, copilot_selector.sku_id)
            if copilot_selector
            else None
        )

        lines: list[ScenarioLine] = []
        unresolved: list[str] = []
        assumptions: list[str] = []
        if not self._apply_bundle_rules:
            assumptions.append(
                "No add-on bundle entitlement was inferred. Every non-core SKU is "
                "retained and priced unless the seller explicitly changes it."
            )
        base_sources: list[int] = []
        copilot_sources: list[int] = []
        for source in estate.lines:
            source_key = self._identity_key(source.product_id, source.sku_id)
            if source_key in core_suite_keys:
                base_sources.append(source.renewal_quantity)
                if source_key == target_key:
                    disposition = MigrationDisposition.INCLUDED
                    note = "Already the selected target suite; represented by the target line."
                else:
                    disposition = MigrationDisposition.MIGRATE
                    note = f"Core-suite licence migrates to {definition.base_title}."
            elif copilot_key is not None and source_key == copilot_key:
                copilot_sources.append(source.renewal_quantity)
                disposition = MigrationDisposition.INCLUDED
                note = "Existing standalone Copilot is represented by the Copilot target line."
            else:
                if not self._apply_bundle_rules:
                    disposition = MigrationDisposition.RETAIN
                    note = (
                        "Retained unchanged; no add-on bundle entitlement assumption "
                        "was applied."
                    )
                else:
                    suggestion = (
                        self._migration_seeds.match(source.display_title, scenario_type)
                        if self._migration_seeds is not None
                        else None
                    )
                    if suggestion is None:
                        disposition = MigrationDisposition.NEEDS_DECISION
                        note = (
                            "The current licensing data contains no entitlement or migration mapping for "
                            "this SKU; retained and priced until the seller decides."
                        )
                    elif suggestion.approved:
                        disposition = suggestion.disposition
                        verification = (
                            f" verified={suggestion.verified_date.isoformat()};"
                            f" source_url={suggestion.source_url};"
                            if suggestion.verified_date is not None
                            and suggestion.source_url is not None
                            else ""
                        )
                        target_note = (
                            f" Replacement target: {definition.base_title}."
                            if disposition
                            in {
                                MigrationDisposition.MIGRATE,
                                MigrationDisposition.INCLUDED,
                                MigrationDisposition.REMOVE,
                            }
                            else ""
                        )
                        note = (
                            f"Approved migration seed {suggestion.rule_id} applies "
                            f"{disposition.value}; source={suggestion.source}."
                            f"{verification}{target_note} {suggestion.rationale}"
                        ).strip()
                    else:
                        disposition = MigrationDisposition.NEEDS_DECISION
                        verification = (
                            f" verified={suggestion.verified_date.isoformat()};"
                            f" source_url={suggestion.source_url};"
                            if suggestion.verified_date is not None
                            and suggestion.source_url is not None
                            else ""
                        )
                        note = (
                            f"Suggested default only: {suggestion.disposition.value} from "
                            f"{suggestion.rule_id}; source={suggestion.source};"
                            f"{verification} approved=false. "
                            "No migration action was auto-applied. "
                            f"{suggestion.rationale}"
                        )
            price_existing = disposition in {
                MigrationDisposition.RETAIN,
                MigrationDisposition.NEEDS_DECISION,
            }
            item = None
            unit = Decimal("0")
            price_unavailable = False
            if price_existing:
                try:
                    item = self._select_price(
                        catalog,
                        SkuSelector(
                            product_id=source.product_id,
                            sku_id=source.sku_id,
                            sku_title=source.display_title,
                        ),
                        term_duration,
                        billing_plan,
                        segment,
                    )
                    unit, price_unavailable = self._unit_price(item, promo_eligible)
                    if price_unavailable:
                        note = (
                            f"{note or ''} "
                            f"{price_unavailability_message(item, promo_eligible, self._price_basis)}"
                        ).strip()
                        unresolved.append(
                            price_unavailability_decision(
                                source.line_id,
                                item,
                                promo_eligible,
                                self._price_basis,
                            )
                        )
                except ScenarioError as error:
                    price_unavailable = True
                    note = f"{note or ''} {error}".strip()
                    unresolved.append(f"{source.line_id}: {error}")
            decision_required = disposition == MigrationDisposition.NEEDS_DECISION or (
                price_existing and price_unavailable
            )
            if disposition == MigrationDisposition.NEEDS_DECISION:
                unresolved.append(
                    f"{source.line_id}: decide whether to migrate, retain, or remove."
                )
            proposed = source.renewal_quantity if price_existing else 0
            lines.append(
                ScenarioLine(
                    line_id=source.line_id,
                    source_line_id=source.line_id,
                    product_id=source.product_id,
                    sku_id=source.sku_id,
                    sku_title=source.display_title,
                    existing_quantity=source.renewal_quantity,
                    proposed_quantity=proposed,
                    unit_price=unit,
                    extended_price=money(unit * proposed),
                    price_unavailable=price_unavailable,
                    term_duration=item.term_duration if item else term_duration,
                    billing_plan=item.billing_plan if item else billing_plan,
                    expiration_date=source.expiration_date,
                    renewal_date=source.renewal_date,
                    category="additional",
                    disposition=disposition,
                    decision_required=decision_required,
                    note=note,
                )
            )

        base_requires_confirmation = False
        if base_quantity is None:
            if base_sources:
                base_quantity = max(base_sources)
                if len(base_sources) > 1:
                    base_requires_confirmation = True
                    assumptions.append(
                        "Multiple core-suite rows exist, so the proposed base quantity uses "
                        "the largest renewal quantity to avoid silently double-counting seats."
                    )
                    unresolved.append("Confirm the proposed base-suite quantity.")
            else:
                base_quantity = max(
                    (line.renewal_quantity for line in estate.lines), default=0
                )
                base_requires_confirmation = True
                assumptions.append(
                    "Base-suite quantity defaults to the largest renewal quantity because no "
                    "core E3/E5/E7 source line identified the base-seat population."
                )
                unresolved.append("Confirm the proposed base-suite quantity.")

        base_item = self._select_price(
            catalog,
            target_selector,
            term_duration,
            billing_plan,
            segment,
        )
        base_unit, base_price_unavailable = self._unit_price(
            base_item,
            promo_eligible,
        )
        if base_price_unavailable:
            base_requires_confirmation = True
            unresolved.append(
                price_unavailability_decision(
                    "BASE", base_item, promo_eligible, self._price_basis
                )
            )
        lines.append(
            ScenarioLine(
                line_id="BASE",
                product_id=base_item.product_id,
                sku_id=base_item.sku_id,
                sku_title=base_item.sku_title,
                existing_quantity=0,
                proposed_quantity=base_quantity,
                unit_price=base_unit,
                extended_price=money(base_unit * base_quantity),
                price_unavailable=base_price_unavailable,
                term_duration=base_item.term_duration,
                billing_plan=base_item.billing_plan,
                category="base",
                disposition=MigrationDisposition.ADD,
                decision_required=base_requires_confirmation,
                note=(
                    f"Target suite. {price_unavailability_message(base_item, promo_eligible, self._price_basis)}"
                    if base_price_unavailable
                    else "Target suite"
                ),
            )
        )

        resolved_copilot_quantity = 0
        if copilot_selector is not None:
            copilot_requires_confirmation = False
            if copilot_quantity is not None:
                resolved_copilot_quantity = copilot_quantity
            elif copilot_sources:
                resolved_copilot_quantity = max(copilot_sources)
                if len(copilot_sources) > 1:
                    copilot_requires_confirmation = True
                    assumptions.append(
                        "Multiple standalone Copilot rows exist, so the proposed Copilot "
                        "quantity uses the largest renewal quantity."
                    )
                    unresolved.append("Confirm the proposed Copilot quantity.")
            else:
                resolved_copilot_quantity = (
                    base_quantity if self._apply_bundle_rules else 0
                )
                assumptions.append(
                    "No existing standalone Copilot line was found; Copilot "
                    + (
                        "defaults to the base-suite quantity"
                        if self._apply_bundle_rules
                        else "defaults to zero"
                    )
                    + " and remains independently editable."
                )
            copilot_item = self._select_price(
                catalog,
                copilot_selector,
                term_duration,
                billing_plan,
                segment,
            )
            copilot_unit, copilot_price_unavailable = self._unit_price(
                copilot_item,
                promo_eligible,
            )
            if copilot_price_unavailable:
                copilot_requires_confirmation = True
                unresolved.append(
                    price_unavailability_decision(
                        "COPILOT",
                        copilot_item,
                        promo_eligible,
                        self._price_basis,
                    )
                )
            lines.append(
                ScenarioLine(
                    line_id="COPILOT",
                    product_id=copilot_item.product_id,
                    sku_id=copilot_item.sku_id,
                    sku_title=copilot_item.sku_title,
                    existing_quantity=0,
                    proposed_quantity=resolved_copilot_quantity,
                    unit_price=copilot_unit,
                    extended_price=money(copilot_unit * resolved_copilot_quantity),
                    price_unavailable=copilot_price_unavailable,
                    term_duration=copilot_item.term_duration,
                    billing_plan=copilot_item.billing_plan,
                    category="copilot",
                    disposition=MigrationDisposition.ADD,
                    decision_required=copilot_requires_confirmation,
                    note=(
                        "Copilot quantity is independently editable. "
                        + price_unavailability_message(
                            copilot_item, promo_eligible, self._price_basis
                        )
                        if copilot_price_unavailable
                        else "Copilot quantity is independently editable."
                    ),
                )
            )
        else:
            assumptions.append(
                "The current licensing data has no field proving that Copilot is included with ME7; "
                "Copilot is therefore not included or priced automatically."
            )

        return self._new_scenario(
            estate,
            scenario_type,
            lines,
            term_duration,
            billing_plan,
            segment,
            promo_eligible,
            resolved_copilot_quantity,
            unresolved,
            assumptions,
        )

    @staticmethod
    def _identity_key(
        product_id: str | None,
        sku_id: str | None,
    ) -> tuple[str, str]:
        return ((product_id or "").casefold(), (sku_id or "").casefold())

    def _core_suite_keys(self, catalog: RateCardCatalog) -> set[tuple[str, str]]:
        keys: set[tuple[str, str]] = set()
        for definition in SCENARIO_DEFINITIONS.values():
            try:
                selector = self._exact_selector(catalog, definition.base_title)
            except ScenarioError:
                # Only exact, unambiguous identities can be categorized automatically.
                continue
            keys.add(self._identity_key(selector.product_id, selector.sku_id))
        return keys

    @staticmethod
    def _exact_selector(catalog: RateCardCatalog, title: str) -> SkuSelector:
        normalized_title = normalize_product_title(title)
        matches = [
            identity
            for identity in catalog.identities
            if normalize_product_title(identity.sku_title) == normalized_title
        ]
        if not matches:
            raise ScenarioError(
                f"The approved catalogue does not contain the required exact product {title!r}."
            )
        if len(matches) > 1:
            identities = ", ".join(
                f"{item.product_id}/{item.sku_id}" for item in matches
            )
            raise ScenarioError(
                f"The approved catalogue contains multiple identities for {title!r}: "
                f"{identities}."
            )
        match = matches[0]
        return SkuSelector(
            sku_title=match.sku_title,
            product_id=match.product_id,
            sku_id=match.sku_id,
        )

    def _select_price(
        self,
        catalog: RateCardCatalog,
        selector: SkuSelector,
        term_duration: str,
        billing_plan: str,
        segment: str,
    ) -> RateCardItem:
        product_id = selector.product_id
        sku_id = selector.sku_id
        if not product_id or not sku_id:
            candidates = catalog.candidates(selector.sku_title, limit=3)
            if not candidates or candidates[0].confidence < 90:
                raise ScenarioError(f"No rate-card SKU matched {selector.sku_title!r}.")
            if len(candidates) > 1 and abs(
                candidates[0].confidence - candidates[1].confidence
            ) < 0.001:
                titles = ", ".join(item.sku_title for item in candidates)
                raise ScenarioError(
                    f"Multiple SKUs match {selector.sku_title!r}: {titles}."
                )
            product_id = candidates[0].product_id
            sku_id = candidates[0].sku_id
        rows = catalog.price_rows(
            product_id=product_id,
            sku_id=sku_id,
            sku_title=selector.sku_title,
            term_duration=term_duration,
            billing_plan=billing_plan,
            segment=segment,
        )
        if not rows:
            raise ScenarioError(
                f"No {term_duration}/{billing_plan} price exists for {selector.sku_title}."
            )
        if self._price_basis == "marketplace":
            prices = {row.marketplace_price for row in rows}
        elif self._price_basis == "distributor_expected":
            prices = {row.distributor_price for row in rows}
        else:
            prices = {
                (
                    row.initial_quote_with_promo,
                    row.initial_quote_without_promo,
                    row.initial_quote_with_promo_available,
                    row.initial_quote_without_promo_available,
                )
                for row in rows
            }
        if len(prices) > 1:
            raise ScenarioError(
                f"Multiple commercial prices exist for {selector.sku_title}; "
                "select a more specific SKU/segment."
            )
        return rows[0]

    def _unit_price(
        self,
        item: RateCardItem,
        promo_eligible: bool,
    ) -> tuple[Decimal, bool]:
        if self._price_basis == "marketplace":
            if item.marketplace_price > 0:
                return money(item.marketplace_price), False
            return Decimal("0.00"), True
        if self._price_basis == "distributor_expected":
            if item.distributor_price > 0:
                return money(item.distributor_price), False
            return Decimal("0.00"), True
        if promo_eligible and item.initial_quote_with_promo_available:
            return money(item.initial_quote_with_promo), False
        if item.initial_quote_without_promo_available:
            return money(item.initial_quote_without_promo), False
        return Decimal("0.00"), True

    def _price_available(self, item: RateCardItem) -> bool:
        if self._price_basis == "marketplace":
            return item.marketplace_price > 0
        if self._price_basis == "distributor_expected":
            return item.distributor_price > 0
        return (
            item.initial_quote_with_promo_available
            or item.initial_quote_without_promo_available
        )

    def _new_scenario(
        self,
        estate: LicenseEstate,
        scenario_type: ScenarioType,
        lines: list[ScenarioLine],
        term_duration: str,
        billing_plan: str,
        segment: str,
        promo_eligible: bool,
        copilot_quantity: int,
        unresolved: list[str],
        assumptions: list[str],
    ) -> CommercialScenario:
        subtotal = money(sum((line.extended_price for line in lines), Decimal("0")))
        now = datetime.now(UTC)
        return CommercialScenario(
            id=str(uuid4()),
            thread_id=estate.thread_id,
            scenario_type=scenario_type,
            status=ScenarioStatus.NEEDS_REVIEW if unresolved else ScenarioStatus.READY,
            term_duration=term_duration,
            billing_plan=billing_plan,
            segment=segment,
            promo_eligible=promo_eligible,
            copilot_quantity=copilot_quantity,
            lines=lines,
            subtotal=subtotal,
            total_value=subtotal,
            unresolved_decisions=list(dict.fromkeys(unresolved)),
            assumptions=assumptions,
            created_at=now,
            updated_at=now,
        )

    def _recalculate(
        self,
        scenario: CommercialScenario,
        lines: list[ScenarioLine],
        *,
        copilot_quantity: int | None = None,
    ) -> CommercialScenario:
        subtotal = money(sum((line.extended_price for line in lines), Decimal("0")))
        discount = money(subtotal * scenario.discount_percentage / Decimal("100"))
        total = money(subtotal - discount + scenario.adjustment_amount)
        if total < 0:
            raise ScenarioError("Discount and adjustment cannot produce a negative total.")
        pending = [
            f"{line.line_id}: seller decision required"
            for line in lines
            if line.decision_required
        ]
        return scenario.model_copy(
            update={
                "lines": lines,
                "subtotal": subtotal,
                "total_value": total,
                "copilot_quantity": (
                    scenario.copilot_quantity
                    if copilot_quantity is None
                    else copilot_quantity
                ),
                "unresolved_decisions": pending,
                "status": ScenarioStatus.NEEDS_REVIEW if pending else ScenarioStatus.READY,
                "revision": scenario.revision + 1,
                "updated_at": datetime.now(UTC),
            }
        )
