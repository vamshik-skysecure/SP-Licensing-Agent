# SkySecure Microsoft Licensing Advisor

Production-oriented WhatsApp workflow for analysing a customer's Microsoft licence estate, preparing an editable renewal proposal, applying seller changes in natural language, and returning customer-ready estate and comparison PDFs. The default `upgrade_comparison` mode compares Renew As-Is with annual ME3, ME5, and ME7 options without inferring any add-on bundle entitlement.

The active workflow is self-contained. It does not call the legacy Phase 1 pricing-agent API.

## Runtime flow

1. Meta sends a signed WhatsApp webhook.
2. Production ingress writes the webhook to Azure Service Bus; local development uses an in-process dispatcher.
3. The application downloads the customer file into memory and validates it.
4. The `Final Output Sheet` is loaded from Microsoft SKU V5.0 locally or from the configured Blob workbook.
5. The official OpenAI Python SDK and Responses API convert free-form seller language into one validated intent.
6. SKU matching, price selection, edits, and totals run deterministically from that catalogue.
7. Normalized state and proposal revisions are persisted as versioned JSON in Azure Blob Storage in production.
8. WhatsApp receives portrait PNG tables rendered for mobile, plus estate and final proposal PDFs.

OpenAI is a language adapter only. It cannot select rate-card rows, calculate totals,
or directly change workflow state. The validated command handlers remain available as
operational fallbacks but are not presented as the primary seller experience.

## Agent response behavior

- After upload, it sends a true portrait table as an inline WhatsApp PNG, grouped by family,
  with full wrapped product names, quantities, and expiry/renewal dates. Long estates are
  paginated into multiple images. A customer-ready estate PDF provides the detailed record.
- After preparing Renew As-Is, it requires explicit seller validation of the SKU matches,
  quantities, renewal dates, pricing, and annual total. Edits and comparisons remain locked
  until that validation is recorded.
- Scenario and comparison responses use the same mobile-first PNG table format. If image
  rendering or delivery fails, the service falls back to responsive full-name text cards;
  it never falls back to a fixed-width ASCII table.
- In the default `upgrade_comparison` mode, it automatically prepares Renew As-Is and can
  build all four annual options on request. Existing non-core add-ons are retained and
  priced unchanged unless the seller explicitly edits them.
- A request such as `Remove L3`, `Change L2 to 50 licences`, or `Add 10 Power BI Pro licences` applies one validated edit and returns
  the fully recalculated proposal immediately.
- A fuzzy `/add` or `/replace` match creates a persisted confirmation request and makes no
  commercial change. The seller must choose an exact ProductId/SkuId with `/confirm-sku N`
  or the WhatsApp list before the proposal is mutated.
- An ambiguous request asks one concise clarification question and makes no commercial change.
- Natural-language promotion, discount, adjustment, term, billing, segment, currency,
  comment, add, replace, remove, quantity, confirmation, and finalization requests map to
  the same deterministic operations. Currency conversion is rejected when unsupported.
- `Finalize the proposal` validates unresolved prices/decisions and opens a separate final
  seller-validation step. The proposal is marked final and its PDF is issued only after the
  seller explicitly confirms the displayed configuration and commercial totals.
- `Compare the annual options` auto-builds missing scenarios using the active proposal's
  commercial settings and base quantity, shows the annual difference from Renew As-Is,
  selects a recommendation, and sends a comparison PDF.
- `scenario_comparison` remains an optional future mode for business-approved migration
  rules; it is not used by the default workflow.
- Promotion, term, billing, and segment changes reprice through the same deterministic Final Output Sheet lookup. Currency conversion rejects
  unsupported conversion because the workbook contains no currency or FX table.
- A blank quote, or a V5 `Partner Best Offer` of zero without an explicit free-product
  marker, is displayed and serialized as `price_unavailable`. Legacy schemas can still
  represent an explicitly available numeric zero as a no-charge product.
- An unknown bundle relationship is never invented. In the default mode, the add-on remains
  separately licensed and priced; only an explicit seller edit can remove or replace it.
- In optional comparison mode, `config/migration_seed.json` contains human-editable, title-pattern suggestions derived
  from real pricebook families. Rows record official, third-party, or unverified
  provenance, but every seed remains `approved: false`; unapproved rows are displayed as
  suggestions and never auto-applied.
- `Partner Best Offer` is the direct seller quote from Microsoft SKU V5.0. Promo-labelled
  rows are available only after the seller confirms eligibility; they are never silently
  treated as standard prices. `Price on Marketplace` is retained as workbook metadata and
  is not substituted for the direct offer.
- The workbook has no entitlement or migration-map columns. Therefore, the default mode
  retains all non-core add-ons, and ME7 never claims Copilot is bundled unless that fact is
  added to an authoritative future workbook schema.

See [Production architecture](docs/PRODUCTION_ARCHITECTURE.md).

## Local development

Use the single storage switch in `.env`:

```dotenv
STORAGE_MODE=local
WORKFLOW_MODE=upgrade_comparison
RATE_CARD_LOCAL_PATH=docs/microsoft_sku_v5.xlsx
RATE_CARD_SHEET_NAME=Final Output Sheet
```

`local` reads `RATE_CARD_LOCAL_PATH` and keeps workflow sessions in memory. Set
`STORAGE_MODE=azure_blob` to use the configured Blob workbook and Blob workflow
container. `STORAGE_MODE` overrides the lower-level `RATE_CARD_BACKEND` and
`WORKFLOW_STORE_BACKEND` values; omit it only for backward-compatible mixed setups.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --reload
```

Set `WHATSAPP_VALIDATE_CREDENTIALS_ON_STARTUP=false` when running API-only tests without Meta access.

Run verification:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

## Production gates

Production should set `STORAGE_MODE=azure_blob`.

Before switching modes, publish the reviewed V5 workbook to
`pricing-workbooks/active/Microsoft_SKU_Active.xlsx` and confirm its
`Final Output Sheet` checksum/version. This implementation does not modify that Blob during
local UAT.

`ENVIRONMENT=production` refuses to start unless:

- the rate card uses Azure Blob Storage;
- workflow state uses Azure Blob Storage with conditional ETag writes;
- webhook dispatch uses Azure Service Bus; and
- a WhatsApp seller allowlist is configured; and
- OpenAI structured intent routing is configured.

The pricebook and workflow state use one Azure Storage account, normally in separate
private containers. Prefer Managed Identity for Blob and Service Bus. Connection strings
exist only as local-development fallbacks; store the OpenAI API key in Key Vault.
