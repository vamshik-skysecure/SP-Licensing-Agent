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
| Promo Name / Promo % / Customer Eligibility / New-to-Microsoft Required | Promotion eligibility control |
| Minimum / Maximum Seats | Parsed eligibility metadata |
| Geography | Parsed commercial metadata |
| Distributor / Partner landing and cost columns | Parsed audit metadata |
| Partner Best Offer | Direct seller quote used by the agent |
| Price on Marketplace | Parsed audit metadata; not substituted for the direct quote |

Promo-labelled rows expose `Partner Best Offer` only when the seller explicitly
confirms promotion eligibility. Standard rows remain available without that confirmation.

## Observed data profile

| Check | Count |
|---|---:|
| Parsed final-output rows | 4,030 |
| Unique ProductId + SkuId pairs | 1,161 |
| Unique ProductId + SkuId + title identities | 1,302 |
| Promotion-labelled rows | 139 |
| Standard rows | 3,891 |
| Zero `Partner Best Offer` rows | 254 |
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

`WORKFLOW_MODE=upgrade_comparison` is the default. Upload automatically prepares Renew
As-Is, and the seller can request a one-year/annual comparison with ME3, ME5, and ME7.
The only automatic replacement is an existing core-suite line with the selected target
suite. Every non-core add-on is retained and priced unchanged unless the seller explicitly
edits it. No migration seed or bundle entitlement is applied in this mode.

`WORKFLOW_MODE=scenario_comparison` remains an optional future mode for explicitly approved
business migration rules.

## Business follow-ups

- Confirm whether any of the 254 zero-price rows are intentionally free; otherwise supply prices.
- Populate Maximum Seats if seat-cap enforcement is required.
- Confirm how perpetual `TermDuration=0` and `BillingPlan=None` rows should be presented
  when mixed with subscription renewals.
- Confirm whether direct quotes should continue using `Partner Best Offer` or switch to
  `Price on Marketplace` for a particular sales channel. The implementation currently
  follows the workbook wording and uses `Partner Best Offer`.
