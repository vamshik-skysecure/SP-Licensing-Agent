# SkySecure Microsoft Licensing Advisor UAT Pack

## Purpose

This pack verifies the current annual upgrade-comparison workflow with synthetic data
before any customer file is used. It covers upload, mobile portrait-table visualization,
promotion eligibility, seller edits, immediate repricing, all four annual options,
annual cost differences, and estate/final PDF delivery.

`docs/client_upload_sheet.csv` is retained as the Phase 2 regression fixture. It
contains five add-ons but no renewal dates or core suite, so it is not the primary
business-demonstration file. Use `synthetic_enterprise_estate.csv` for complete UAT.

## Data-handling rule

- Use only the supplied synthetic files during sandbox testing.
- Do not upload customer names, tenant IDs, contracts, or real licence exports.
- Keep the seller allowlist limited to named UAT participants.
- Do not enable OpenAI until the previously exposed API key has been rotated.
- Do not claim any add-on is bundled into ME3, ME5, or ME7. The application must retain
  and price every non-core add-on unless the seller explicitly changes it.

## Files

| File | Purpose |
|---|---|
| `synthetic_enterprise_estate.csv` | Canonical synthetic WhatsApp upload |
| `synthetic_enterprise_estate.xlsx` | Formatted Excel version of the same estate |
| `customer_license_upload_template.csv` | Blank integration template |
| `migration_seed_business_review.csv` | Row-by-row licensing approval worksheet |
| `UAT_EXECUTION_RECORD.md` | Tester evidence and sign-off record |

## Upload contract

Required columns:

- `Product Title`
- `Total Licenses`
- `Expired Licenses`
- `Assigned Licenses`

Strongly recommended columns:

- `ProductId` and `SkuId` for exact identity matching
- `Renewal Date` or `Expiration Date` in ISO `YYYY-MM-DD` format

`Renewal quantity = Total Licenses - Expired Licenses`. ProductId and SkuId must
come from the active Final Output Sheet and must not be guessed.

## Local test configuration

```dotenv
ENVIRONMENT=development
STORAGE_MODE=local
WORKFLOW_MODE=upgrade_comparison
RATE_CARD_LOCAL_PATH=docs/microsoft_sku_v5.xlsx
RATE_CARD_SHEET_NAME=Final Output Sheet
MESSAGE_DISPATCH_BACKEND=direct
AI_INTENT_BACKEND=disabled
WHATSAPP_VALIDATE_CREDENTIALS_ON_STARTUP=true
```

Restart FastAPI after changing `.env`. In-memory sessions are intentionally lost
on restart during this phase.

## Deterministic golden run

Run without WhatsApp, OpenAI, or Azure:

```powershell
.\.venv\Scripts\python.exe scripts\run_uat_golden.py
```

Evidence is written under `artifacts/uat/`.

## WhatsApp conversation script

1. Send `/help` from the allowlisted UAT phone.
2. Attach `synthetic_enterprise_estate.csv` as a document.
3. Confirm the inline portrait table image is grouped by family, shows complete wrapped
   product names without truncation, and remains legible without horizontal scrolling.
   Confirm the estate PDF shows five exact matches and renewal dates.
4. Confirm Renew As-Is is prepared automatically with provisional promotional pricing.
5. Confirm the estate, pricing, and customer promotion eligibility together by choosing
   `Confirm details` or replying `I confirm the analysis, pricing, and promotion eligibility`.
   Verify edits remain blocked before this single approval.

6. Exercise seller edits and immediate recalculation in natural language:

```text
Change L1 to 120 licences.
Apply a 5 percent discount.
Subtract INR 25,000 as a commercial adjustment.
Add the comment: Synthetic UAT only; final approval pending.
```

7. Say `Compare the annual options`.
8. Confirm Renew As-Is, ME3, ME5, and ME7 all use `P1Y` / `Annual`; each non-core add-on
   is retained with no bundle-entitlement assumption; and the inline comparison table image
   shows each option's difference from Renew As-Is.
9. Confirm `licensing-commercial-comparison.pdf` is received as a WhatsApp document.
10. Select and edit any option if required, then say `Finalize the proposal`.
11. Verify no final proposal PDF arrives yet. Review the validation summary and choose
    `Confirm & finalize` or reply `Yes, finalize this proposal`.
12. Confirm the final PDF arrives only after that approval, then record message IDs,
    timestamps, totals and PDF hashes in the execution record.

If a displayed line ID differs, use the line ID returned by the agent rather than
blindly copying this script.

## Natural-language phase

After OpenAI key rotation, set `AI_INTENT_BACKEND=openai` and restart the app. The model
only converts language into validated deterministic operations; it never calculates prices.

## Phase 1 parity boundary

The Phase 1 source code and legacy API credentials are not available, so its exact
prompt format, upload contract, session behavior and responses cannot be verified.
This UAT pack validates directly against the authoritative use case and current
Final Output Sheet. No undocumented Phase 1 behavior is assumed or copied.
