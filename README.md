# SkySecure Microsoft Licensing Advisor

Production-oriented WhatsApp workflow for capturing a seller's Microsoft CSP licensing requirement, confirming it, pricing it as submitted, and optionally modelling SKU or quantity changes. The default `simple_pricing` mode implements the CEO-approved three-step first release and uses `Price on Marketplace` from the FYD `Final Output Sheet`.

The active workflow is self-contained. It does not call the legacy Phase 1 pricing-agent API.

## Runtime flow

1. Meta sends a signed WhatsApp webhook.
2. Production ingress writes the webhook to Azure Service Bus; local development uses an in-process dispatcher.
3. The application accepts natural-language text, voice notes, screenshots/images, or common spreadsheet/document uploads and normalizes them into one strict requirement schema.
4. The `Final Output Sheet` is loaded from Microsoft SKU V5.0 locally or from the configured Blob workbook.
5. The official OpenAI Python SDK and Responses API provide structured intent routing and bounded multimodal extraction; the Audio Transcriptions API handles voice notes.
6. SKU matching, price selection, edits, and totals run deterministically from that catalogue.
7. Normalized state and proposal revisions are persisted as versioned JSON in Azure Blob Storage in production.
8. WhatsApp receives portrait PNG tables rendered locally for mobile, plus locally rendered confirmation and commercial PDFs.

OpenAI extracts unstructured seller inputs and proposes a typed intent. It cannot select
rate-card rows, calculate totals, approve fuzzy SKU matches, or directly change workflow
state. Standard CSV/XLSX layouts are parsed deterministically without an OpenAI call.

## Agent response behavior

- After capture, it sends a true portrait table as an inline WhatsApp PNG, grouped by family,
  with full wrapped product names, quantities, terms, billing, and expiry/renewal dates. Long requirements are
  paginated into multiple images. A customer-ready estate PDF provides the detailed record.
- It requires explicit seller validation of the captured SKU names, quantities, terms, and
  dates before it performs any pricing. The seller can add, replace, remove, or change a line
  while this gate is open; fuzzy add/replace matches require an explicit candidate choice.
- After confirmation, it displays SKU, quantity, billing term, Marketplace unit price, line
  total, and overall value. It intentionally omits 0% discount, distributor discount, margin,
  adjustment, promotion-selection questions, and internal calculations.
- Scenario and comparison responses use the same mobile-first PNG table format. If image
  rendering or delivery fails, the service falls back to responsive full-name text cards;
  it never falls back to a fixed-width ASCII table.
- The confirmed as-is configuration is stored as an immutable session snapshot. Each accepted
  seller change recalculates a separate revised configuration immediately and shows current,
  revised, and commercial difference without overwriting the baseline.
- A request such as `Remove L3`, `Change L2 to 50 licences`, or `Add 10 Power BI Pro licences` applies one validated edit and returns
  the fully recalculated proposal immediately.
- A fuzzy `/add` or `/replace` match creates a persisted confirmation request and makes no
  commercial change. The seller must choose an exact ProductId/SkuId with `/confirm-sku N`
  or the WhatsApp list before the proposal is mutated.
- An ambiguous request asks one concise clarification question and makes no commercial change.
- Natural-language add, replace, remove, quantity, target-suite, comparison, confirmation,
  and finalization requests map to deterministic operations. Seller-facing promotion,
  discount, margin, and adjustment controls are disabled in `simple_pricing` mode.
- `Finalize the proposal` validates unresolved prices/decisions and opens a separate final
  seller-validation step. The proposal is marked final and its PDF is issued only after the
  seller explicitly confirms the displayed configuration and commercial totals.
- `Compare the current and revised configuration` sends a mobile comparison and PDF with the
  as-is value, revised value, difference, replacement/addition notes, and pricing source.
- `scenario_comparison` remains an optional future mode for business-approved migration
  rules; it is not used by the default workflow.
- Promotion, term, billing, and segment changes reprice through the same deterministic Final Output Sheet lookup. Currency conversion rejects
  unsupported conversion because the workbook contains no currency or FX table.
- A blank or zero `Price on Marketplace` is displayed and serialized as
  `price_unavailable` and is excluded from the total; it is never silently presented as a
  free product.
- An unknown bundle relationship is never invented. In the default mode, the add-on remains
  separately licensed and priced; only an explicit seller edit can remove or replace it.
- In optional comparison mode, `config/migration_seed.json` contains human-editable, title-pattern suggestions derived
  from real pricebook families. Rows record official, third-party, or unverified
  provenance, but every seed remains `approved: false`; unapproved rows are displayed as
  suggestions and never auto-applied.
- `Price on Marketplace` is the sole seller price basis in the current release. Partner Best
  Offer, margin, and promotional workbook fields are not used or displayed. The upcoming
  business rule sheet is intentionally not simulated: generic recommendation requests ask
  for the source line, required capability/target SKU, and user count instead of inventing
  eligibility or entitlement logic.
- The workbook has no entitlement or migration-map columns. Therefore, the default mode
  retains all non-core add-ons, and ME7 never claims Copilot is bundled unless that fact is
  added to an authoritative future workbook schema.

See [Production architecture](docs/PRODUCTION_ARCHITECTURE.md).
Use [V1 Simple Pricing UAT](docs/uat/V1_SIMPLE_PRICING_UAT.md) for the current seller journey.

## Local development

Use the single storage switch in `.env`:

```dotenv
STORAGE_MODE=local
WORKFLOW_MODE=simple_pricing
RATE_CARD_LOCAL_PATH=docs/microsoft_sku_v5.xlsx
RATE_CARD_SHEET_NAME=Final Output Sheet
AI_INTENT_BACKEND=openai
REQUIREMENT_CAPTURE_BACKEND=openai
OPENAI_MODEL=gpt-5.6-luna
OPENAI_TRANSCRIPTION_MODEL=gpt-transcribe
MAX_AUDIO_SECONDS=300
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
- OpenAI multimodal requirement capture is configured.

The pricebook and workflow state use one Azure Storage account, normally in separate
private containers. Prefer Managed Identity for Blob and Service Bus. Connection strings
exist only as local-development fallbacks; store the OpenAI API key in Key Vault.
