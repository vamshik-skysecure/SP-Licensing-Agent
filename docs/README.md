# Documentation and governed data assets

- `microsoft_sku_v5.xlsx` is the checked-in development copy of the current V1 catalogue. Production reads `pricing-workbooks/active/Microsoft_SKU_V5.0.xlsx` from Azure Blob Storage. The older `Microsoft_SKU_Active.xlsx` remains untouched for Phase 1 rollback.
- `client_upload_sheet.csv` is a synthetic Phase 2 regression fixture retained for compatibility testing.
- `PRD-SP-SSP-Licensing-Agent.pdf` is the original product requirements source.
- `PRICEBOOK_V5_AUDIT.md` records the current workbook data-quality boundary.
- `PRODUCTION_ARCHITECTURE.md` describes the implemented architecture.
- `PRODUCTION_DEPLOYMENT_RUNBOOK.md` is the Azure deployment and cutover authority.
- `uat/` contains synthetic UAT inputs, scripts, and reviewer evidence templates.
- `archive/` contains clearly labelled historical material that is not a current product or deployment authority.

The older Outcome Sheet workbook is stored under `tests/fixtures/legacy_outcome_sheet.xlsx` and is test-only. It must never be configured as the production rate card.
