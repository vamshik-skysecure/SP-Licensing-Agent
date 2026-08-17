# Microsoft SKU V6.0 Distributor Pricebook Audit

## Authoritative input

| Property | Verified value |
|---|---|
| Workbook | `Microsoft SKU V6.0 - Distributor.xlsx` |
| SHA-256 | `0da7ef261d987722e4a8c4af3d6a8fa46ecef5f96f2d01c77ceb2023df8ee86e` |
| Final worksheet | `Outcome Sheet` |
| Header row | 2 |
| Pricing column | `Expectec Disti Price to Skysecure` |
| Raw data rows | 4,030 |

The source workbook currently spells the price header as `Expectec`. The parser accepts that
exact header and the corrected `Expected` spelling so a future header correction is safe.

## Runtime boundary

| Classification | Count |
|---|---:|
| Rows loaded | 4,030 |
| Rows displayed and matched by product name because ProductId/SkuId are blank | 297 |
| Rows with a zero distributor price | 254 |
| Conflicting annual price rows among identifiable SKUs | 0 |

All 297 rows without ProductId/SkuId remain in the catalogue and are shown by their complete
product name only. The application does not manufacture identifiers. When name, term, and billing
resolve to one maintained row, that row can be priced. When the workbook contains multiple
same-name rows with different prices and no identifier or qualifier to distinguish them, the line
is flagged for review instead of silently selecting a price.

Zero distributor-price rows are marked `price_unavailable`, excluded from totals, and block final
proposal approval until an applicable maintained price is supplied.

## Required annual scenario validation

The workbook contains exactly one `P1Y` / `Annual` row with a positive approved distributor price
for each required core product:

- Microsoft 365 E3
- Microsoft 365 E5 without Audio Conferencing
- Microsoft 365 E7 without Audio Conferencing
- Microsoft 365 Copilot

The current application does not use ERP Price or UnitPrice for seller pricing. It does not expose
the internal distributor column name in customer-facing messages or PDFs; outputs use the neutral
description `Applicable annual licence price`.
