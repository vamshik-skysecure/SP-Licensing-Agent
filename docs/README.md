# Documentation and governed data assets

- `microsoft_sku_v6_distributor.xlsx` is the checked-in development copy of the current V1 catalogue. Production reads `pricing-workbooks/active/Microsoft_SKU_V6.0_Distributor.xlsx` from Azure Blob Storage and prices from the last `Outcome Sheet` using `Expectec Disti Price to Skysecure`. All 4,030 rows are retained; the 297 rows without ProductId/SkuId are displayed by product name. The older V5 and Phase 1 workbooks remain untouched for rollback.
- `client_upload_sheet.csv` is a synthetic Phase 2 regression fixture retained for compatibility testing.
- `PRD-SP-SSP-Licensing-Agent.pdf` is the original product requirements source.
- `PRICEBOOK_V6_DISTRIBUTOR_AUDIT.md` records the current workbook data-quality boundary.
- `PRICEBOOK_V5_AUDIT.md` is retained as the historical V5 audit and rollback reference.
- `PRODUCTION_ARCHITECTURE.md` describes the implemented architecture.
- `PRODUCTION_DEPLOYMENT_RUNBOOK.md` is the Azure deployment and cutover authority.
- `uat/` contains synthetic UAT inputs, scripts, and reviewer evidence templates.
- `archive/` contains clearly labelled historical material that is not a current product or deployment authority.

The older Outcome Sheet workbook is stored under `tests/fixtures/legacy_outcome_sheet.xlsx` and is test-only. It must never be configured as the production rate card.
