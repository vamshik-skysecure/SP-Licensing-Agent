# Microsoft SKU V5.0 Pricebook Audit

Audit date: 2026-08-05

Workbook: `docs/microsoft_sku_v5.xlsx`

SHA-256: `2ea28fe7ac27280144fd161a450a71ed2dd4775c5e4d7cbe259c68aa7f1cc74f`

## Workbook structure

| Sheet | Rows | Columns | Runtime role |
|---|---:|---:|---|
| Manual Input | 5,005 | 33 | Business-maintained commercial inputs |
| Rule Master | 5,005 | 33 | Formula-generated rule/guidance layer |
| Final Output Sheet | 4,031 | 21 | Authoritative runtime pricebook (header + 4,030 rows) |

The application reads the cached formula results from `Final Output Sheet` using
`openpyxl` with `data_only=True`. It does not execute Excel formulas at runtime.

## Runtime column decisions

| Final Output field | Runtime treatment |
|---|---|
| ProductId + SkuId | Primary SKU identity when both are non-placeholder values |
| SkuTitle | Exact-title fallback and customer display name |
| Contract Type / TermDuration / BillingPlan / Segment | Commercial variant selection |
| ERP / Catalogue Price | Audit metadata |
| Promo Name / Promo % / Customer Eligibility / New-to-Microsoft Required | Parsed audit metadata; inactive in V1 |
| Minimum / Maximum Seats | Parsed eligibility metadata |
| Geography | Parsed commercial metadata |
| Distributor / Partner landing and cost columns | Parsed audit metadata |
| Partner Best Offer | Parsed audit metadata; not used or displayed in V1 |
| Price on Marketplace | Sole deterministic V1 unit-price basis |

V1 does not ask promotion-eligibility questions and does not select a promotional or
partner-best price. Blank/zero Marketplace prices are explicitly marked unavailable and
excluded from totals.

## Observed data profile

| Check | Count |
|---|---:|
| Parsed final-output rows | 4,030 |
| Unique ProductId + SkuId pairs | 1,161 |
| Unique ProductId + SkuId + title identities | 1,302 |
| Promotion-labelled rows | 139 |
| Standard rows | 3,891 |
| Positive `Price on Marketplace` rows | 3,776 |
| Zero `Price on Marketplace` rows | 254 |
| Rows with placeholder ProductId/SkuId `0/0` | 233 |
| Rows with `TermDuration=0` | 233 |
| Rows with `BillingPlan=None` | 58 |
| Rows with blank Maximum Seats | 4,030 |
| Duplicate exact product/title/term/billing/segment selectors | 0 |

The 233 `0/0` rows represent many unrelated perpetual/server products. The matcher
therefore treats `0` as a placeholder and falls back to exact title; it never treats
`0/0` as a unique SKU identity. Genuine ProductId+SkuId pairs remain authoritative.

Zero prices remain visibly flagged through `price_unavailable` unless a row is
explicitly established as a genuine no-charge offer. They are not silently assumed free.

## Current workflow decision

`WORKFLOW_MODE=simple_pricing` is the default. The seller confirms the captured requirement
before the application calculates its annual Marketplace cost. The confirmed configuration
becomes the Renew As-Is baseline; seller-directed SKU/quantity changes create a revised
configuration and a current-versus-revised comparison.

No promotion, discount, margin, partner-price, automatic migration, or bundle-entitlement
rule is applied in this mode. A generic recommendation request asks the seller for the
source line, required capability/target, and user count instead of inventing a licensing
decision.

`upgrade_comparison` and `scenario_comparison` remain inactive optional modes for future
business-approved releases.

## Business follow-ups

- Confirm whether any of the 254 zero-Marketplace-price rows are intentionally free;
  otherwise supply prices. V1 treats every one as unavailable meanwhile.
- Populate Maximum Seats if seat-cap enforcement is required.
- Confirm how perpetual `TermDuration=0` and `BillingPlan=None` rows should be presented
  when mixed with subscription renewals.
- Supply and approve the separate commercial rule sheet before promotions, eligibility,
  best-price selection, or automated entitlement recommendations are introduced.
