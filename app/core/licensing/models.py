from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


Money = Annotated[Decimal, Field(decimal_places=6)]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ScenarioType(StrEnum):
    RENEW_AS_IS = "renew_as_is"
    ME3_COPILOT = "me3_copilot"
    ME5_COPILOT = "me5_copilot"
    ME7 = "me7"

    @property
    def label(self) -> str:
        return {
            self.RENEW_AS_IS: "Renew As-Is",
            self.ME3_COPILOT: "ME3 + Copilot",
            self.ME5_COPILOT: "ME5 + Copilot",
            self.ME7: "ME7",
        }[self]


class MigrationDisposition(StrEnum):
    RETAIN = "retain"
    MIGRATE = "migrate"
    INCLUDED = "included"
    REMOVE = "remove"
    NEEDS_DECISION = "needs_decision"
    ADD = "add"


class EstateStatus(StrEnum):
    READY = "ready"
    AWAITING_MATCH_CONFIRMATION = "awaiting_match_confirmation"


class ScenarioStatus(StrEnum):
    READY = "ready"
    NEEDS_REVIEW = "needs_review"
    FINAL = "final"


class RateCardItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    sku_id: str
    sku_title: str
    term_duration: str
    billing_plan: str
    segment: str | None = None
    erp_price: Money
    catalogue_price: Money
    promo_percentage: Money = Decimal("0")
    net_to_ms: Money = Decimal("0")
    partner_price_with_promo: Money = Decimal("0")
    partner_price_without_promo: Money = Decimal("0")
    initial_quote_with_promo: Money
    initial_quote_without_promo: Money
    initial_quote_with_promo_available: bool = True
    initial_quote_without_promo_available: bool = True
    source_row_number: int = Field(ge=2)


class SkuMatchCandidate(BaseModel):
    product_id: str
    sku_id: str
    sku_title: str
    confidence: float = Field(ge=0, le=100)


class ParsedLicenseRow(BaseModel):
    row_number: int = Field(ge=2)
    product_title: str
    product_id: str | None = None
    sku_id: str | None = None
    total_licenses: int = Field(ge=0)
    expired_licenses: int = Field(ge=0)
    assigned_licenses: int = Field(ge=0)
    renewal_quantity: int = Field(ge=0)
    expiration_date: date | None = None
    renewal_date: date | None = None


class NormalizedLicenseLine(BaseModel):
    line_id: str
    row_number: int = Field(ge=2)
    source_product_title: str
    product_id: str | None = None
    sku_id: str | None = None
    sku_title: str | None = None
    total_licenses: int = Field(ge=0)
    expired_licenses: int = Field(ge=0)
    assigned_licenses: int = Field(ge=0)
    renewal_quantity: int = Field(ge=0)
    expiration_date: date | None = None
    renewal_date: date | None = None
    match_confidence: float | None = Field(default=None, ge=0, le=100)
    match_method: Literal["exact", "fuzzy", "seller_confirmed", "unresolved"]
    candidates: list[SkuMatchCandidate] = Field(default_factory=list)

    @property
    def display_title(self) -> str:
        return self.sku_title or self.source_product_title


class LicenseEstate(BaseModel):
    id: str
    thread_id: str
    source_file: str
    status: EstateStatus
    lines: list[NormalizedLicenseLine]
    rate_card_version: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @property
    def pending_lines(self) -> list[NormalizedLicenseLine]:
        return [line for line in self.lines if line.match_method == "unresolved"]

    @property
    def total_renewal_quantity(self) -> int:
        return sum(line.renewal_quantity for line in self.lines)


class ScenarioLine(BaseModel):
    line_id: str
    source_line_id: str | None = None
    product_id: str | None = None
    sku_id: str | None = None
    sku_title: str
    existing_quantity: int = Field(ge=0)
    proposed_quantity: int = Field(ge=0)
    unit_price: Money = Decimal("0")
    extended_price: Money = Decimal("0")
    price_unavailable: bool = False
    term_duration: str
    billing_plan: str
    category: Literal["base", "copilot", "additional"] = "additional"
    expiration_date: date | None = None
    renewal_date: date | None = None
    disposition: MigrationDisposition
    decision_required: bool = False
    note: str | None = None


class CommercialScenario(BaseModel):
    id: str
    thread_id: str
    scenario_type: ScenarioType
    status: ScenarioStatus
    revision: int = Field(default=1, ge=1)
    term_duration: str
    billing_plan: str
    segment: str
    promo_eligible: bool = False
    copilot_quantity: int = Field(default=0, ge=0)
    lines: list[ScenarioLine]
    subtotal: Money
    discount_percentage: Money = Decimal("0")
    adjustment_amount: Money = Decimal("0")
    total_value: Money
    comments: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    unresolved_decisions: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PendingSkuChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    action: Literal["add", "replace"]
    scenario_type: ScenarioType
    scenario_revision: int = Field(ge=1)
    product_query: str
    quantity: int = Field(gt=0)
    source_line_id: str | None = None
    candidates: list[SkuMatchCandidate] = Field(min_length=1, max_length=3)
    created_at: datetime = Field(default_factory=utc_now)


class SkuChangeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["applied", "confirmation_required"]
    scenario: CommercialScenario | None = None
    confirmation: PendingSkuChange | None = None


class WorkflowStage(StrEnum):
    AWAITING_UPLOAD = "awaiting_upload"
    AWAITING_MATCH_CONFIRMATION = "awaiting_match_confirmation"
    AWAITING_SCENARIO = "awaiting_scenario"
    REVIEWING_SCENARIO = "reviewing_scenario"


class WorkflowSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    thread_id: str
    sender: str
    stage: WorkflowStage = WorkflowStage.AWAITING_UPLOAD
    estate: LicenseEstate | None = None
    scenarios: dict[ScenarioType, CommercialScenario] = Field(default_factory=dict)
    active_scenario: ScenarioType | None = None
    pending_sku_change: PendingSkuChange | None = None
    processed_message_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ComparisonRow(BaseModel):
    scenario_type: ScenarioType
    base_licences: Money
    copilot: Money
    additional_or_retained: Money
    total_cost: Money


class CommercialComparison(BaseModel):
    thread_id: str
    rows: list[ComparisonRow]
    recommended_scenario: ScenarioType
    recommendation_rationale: str
    generated_at: datetime = Field(default_factory=utc_now)
