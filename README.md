# SP/SSP Licensing Agent

Production-oriented WhatsApp workflow for analysing a customer's Microsoft licence estate, preparing Renew As-Is / ME3 + Copilot / ME5 + Copilot / ME7 scenarios, applying seller edits, and returning a commercial comparison PDF.

The active workflow is self-contained. It does not call the legacy Phase 1 pricing-agent API.

## Runtime flow

1. Meta sends a signed WhatsApp webhook.
2. Production ingress writes the webhook to Azure Service Bus; local development uses an in-process dispatcher.
3. The application downloads the customer file into memory and validates it.
4. The Outcome Sheet is loaded from the configured Excel workbook or Blob Storage.
5. The official OpenAI Python SDK and Responses API convert free-form seller language into one validated intent.
6. SKU matching, safe migration classification, pricing, edits, and totals run deterministically from that catalogue.
7. Normalized state and proposal revisions are persisted as versioned JSON in Azure Blob Storage in production.
8. WhatsApp receives the estate summary, scenario controls, and comparison PDF.

OpenAI is a language adapter only. It cannot select rate-card rows, calculate totals,
or directly change workflow state. Commands and buttons use the same validated operations,
so local/UAT testing does not require a real WhatsApp number.

## Agent response behavior

- After upload, it sends a customer-ready PDF table grouped by product family, with
  expiry/renewal alerts and migration-review flags.
- A request such as `Prepare ME5 for 150 users with 40 Copilot seats` produces the priced ME5
  scenario, revision number, line-level actions, assumptions, unresolved decisions, and total.
- A request such as `Remove L3` or `Change Copilot to 55` applies one validated edit and returns
  the fully recalculated proposal immediately.
- An ambiguous request asks one concise clarification question and makes no commercial change.
- `/compare` auto-builds any missing scenario, returns all four side-by-side totals,
  selects a recommended option with a one-line rationale, and sends a customer-ready PDF.
- `/promo`, `/discount`, `/adjust`, `/term`, `/billing`, and `/segment` reprice a
  scenario through the same deterministic Outcome Sheet lookup. `/currency` rejects
  unsupported conversion because the workbook contains no currency or FX table.
- A blank quote is displayed and serialized as `price_unavailable`; a genuine numeric
  zero is displayed separately as a no-charge product.
- An unknown migration is never invented: the licence remains priced and is marked
  `needs_decision` until an authorized seller resolves it.
- E3, E5, E7, and standalone Copilot identities are resolved by exact product title from
  the current Outcome Sheet; no product ID, SKU ID, or price is maintained elsewhere.
- The workbook has no entitlement or migration-map columns. Therefore, non-core add-ons
  remain `needs_decision`, and ME7 never claims Copilot is bundled unless that fact is
  added to an authoritative future workbook schema.

See [Production architecture](docs/PRODUCTION_ARCHITECTURE.md).

## Local development

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

`ENVIRONMENT=production` refuses to start unless:

- the rate card uses Azure Blob Storage;
- workflow state uses Azure Blob Storage with conditional ETag writes;
- webhook dispatch uses Azure Service Bus; and
- a WhatsApp seller allowlist is configured; and
- OpenAI structured intent routing is configured.

The Outcome Sheet and workflow state use one Azure Storage account, normally in separate
private containers. Prefer Managed Identity for Blob and Service Bus. Connection strings
exist only as local-development fallbacks; store the OpenAI API key in Key Vault.
