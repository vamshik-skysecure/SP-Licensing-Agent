from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class MatchedTenantLicense(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_title: str
    classification: str

    total_licenses: int = Field(ge=0)
    expired_licenses: int = Field(ge=0)
    active_licenses: int = Field(ge=0)
    assigned_licenses: int = Field(ge=0)
    available_licenses: int = Field(ge=0)

    utilisation_percentage: Decimal = Field(ge=0)
    commercially_eligible: bool
    status_message: str


class TenantSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_products_detected: int = Field(ge=0)
    commercial_products_detected: int = Field(ge=0)
    excluded_products_detected: int = Field(ge=0)

    total_active_commercial_licenses: int = Field(ge=0)
    total_assigned_commercial_licenses: int = Field(ge=0)


class TenantAnalysisResponse(TenantSummary):
    model_config = ConfigDict(extra="forbid")

    source_file: str
    licenses: list[MatchedTenantLicense]


class QuoteSelectionOption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product_id: str
    sku_id: str | int
    sku_title: str
    term_duration: str
    billing_plan: str


class QuoteSelectionRequiredDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal["QUOTE_SELECTION_REQUIRED"]
    message: str
    available_options: list[QuoteSelectionOption]


class QuoteSelectionRequiredResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    detail: QuoteSelectionRequiredDetail


class FinalQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote_number: int = Field(ge=1)
    quote_status: str

    product_id: str
    sku_id: str | int
    sku_title: str

    term_duration: str
    billing_plan: str

    promo_percentage: Decimal = Field(ge=0, le=100)
    source_row_number: int = Field(ge=1)

    target_quantity: int = Field(ge=0)
    existing_quantity: int = Field(ge=0)
    non_promo_quantity: int = Field(ge=0)
    promo_quantity: int = Field(ge=0)

    initial_quote_without_promo: Decimal = Field(ge=0)
    initial_quote_with_promo: Decimal = Field(ge=0)
    non_promo_amount: Decimal = Field(ge=0)
    promo_amount: Decimal = Field(ge=0)
    blended_unit_price: Decimal = Field(ge=0)
    total_quote_amount: Decimal = Field(ge=0)


class AnalyzeAndQuoteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_file: str
    product_requested: str
    tenant_match_status: str

    matched_tenant_licenses: list[MatchedTenantLicense]
    tenant_summary: TenantSummary
    final_quote: FinalQuote
