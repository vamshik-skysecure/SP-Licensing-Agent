# Production Architecture Decision

## Decision

The draft PRD's domain boundaries are retained, but the seven-agent supervisor design is replaced by a deterministic persisted workflow.

This workflow is commercial software: SKU identity, quantities, approved distributor prices, and totals must be reproducible and auditable. The official OpenAI Python SDK provides structured natural-language intent, multimodal requirement extraction, and bounded voice transcription, but the model is not an authority for SKU matching, price selection, calculations, or commercial state changes. Deterministic command handlers remain available as operational fallbacks.

The linked `ssp-v2-boilerplate` repository was assessed and is not used as a code base. Its own guide identifies it as an early scaffold with empty prompts/tools, an invalid model wrapper, no graph, no WhatsApp route, no tests, and no production wiring. Its data-contract and acceptance-criteria ideas informed this implementation.

## Target topology

```text
WhatsApp Cloud API
        |
        v
FastAPI webhook -- signature + explicit access mode + size/type checks
        |
        v
Azure Blob webhook inbox -- persistence, leases, retries, dead-letter prefix
        |
        v
OpenAI structured capture/intent adapters
  |-- Responses API: text, files, and screenshots
  +-- Audio Transcriptions API: bounded voice notes
        |
        v
Deterministic licensing workflow
   |               |              |
   v               v              v
Analysis      Scenario/pricing   Output renderers
              engine             |-- portrait PNG tables
                                 +-- detailed PDFs
   |               |
   +---------------+
              |
       Azure Blob JSON state (ETag concurrency)

Azure Blob Storage -> maintained V6 Distributor workbook -> Outcome Sheet cache
```

## Why this differs from the draft PRD

- A handoff-only LLM supervisor is unnecessary for a four-option state machine.
- Seven agents create more model calls, latency, failure paths, prompt/version management, and trace volume without improving deterministic calculations.
- The edit operation must recalculate and persist atomically. That belongs in one application transaction, not a conversation between agents.
- The existing workflow Blob container provides durable webhook ingress because FastAPI background tasks are not durable across process restarts or deployments. This satisfies the manager's no-new-service constraint.
- Managed Identity is the production default for Azure data services.
- The rate-card workbook is read directly, so the business can update the Outcome Sheet without coordinating changes with hidden Phase 1 code.

The intent adapter uses a strict JSON Schema and receives only the seller message plus a
bounded workflow summary. The capture adapter receives an individual seller-supplied file,
image, or transcript only when deterministic spreadsheet parsing cannot handle the input.
Neither adapter receives the pricing workbook. Both outputs are validated, matched against
the deterministic catalogue, and shown to the seller for confirmation before pricing.

LangGraph can still be introduced later if genuine long-running human interrupts require it. If introduced, use a deterministic `StateGraph` and an appropriate durable checkpointer; do not move pricing or migration decisions into model nodes.

## Data ownership

### Blob Storage

- `RUNTIME_PROFILE=local_demo` selects the complete local demonstration stack as one
  safe unit: development environment, checked-in workbook, in-memory sessions, direct
  webhook handling, and OpenAI-assisted capture/routing. `RUNTIME_PROFILE=production`
  selects the Azure Blob and durable-ingress stack. Azure App Service settings and the
  process-scoped local launcher keep the two profiles isolated.
- `STORAGE_MODE=local` selects the checked-in workbook plus in-memory workflow state
  for local testing. `STORAGE_MODE=azure_blob` selects Blob for both pricing and
  workflow sessions. These lower-level settings remain available for backward-compatible
  custom configurations when `RUNTIME_PROFILE` is unset.

- One maintained `.xlsx` workbook in the private `pricing-workbooks` container, with
  the reviewed V6 Distributor version published as `active/Microsoft_SKU_V6.0_Distributor.xlsx`. The older
  `Microsoft_SKU_Active.xlsx` remains unchanged for Phase 1 rollback.
- Current local workbook: `docs/microsoft_sku_v6_distributor.xlsx`.
- Configured worksheet: `Outcome Sheet` (the final workbook sheet).
- Application caches the parsed catalog for five minutes and records the Blob ETag/content digest as the rate-card version.
- A separate private workflow container in the same Storage account holds one JSON blob per hashed seller thread.
- Blob ETags and `If-Match` conditional writes provide optimistic concurrency and prevent lost updates.
- The same container holds a leased `webhook-queue` inbox. Signed requests are persisted before acknowledgement, processed sequentially on the single B1 instance, retried, and moved to a dead-letter prefix after repeated failures.
- Sessions expire after five minutes of inactivity in both Blob and local-memory modes.
  The next seller message atomically replaces expired state, starts a fresh requirement,
  and receives an expiry notice. A prefix-scoped lifecycle rule deletes old session blobs.
- Raw customer uploads are never persisted. Only normalized estate and proposal state are stored.

### Excel-only commercial authority

- The final `Outcome Sheet` is the licensing catalogue and pricing source.
- `Expectec Disti Price to Skysecure` is the sole price basis for `WORKFLOW_MODE=simple_pricing`, the
  current default. ERP, UnitPrice, Partner Best Offer, margin, and promotion columns are
  not used or exposed in this workflow.
- The three-step state machine is capture/reconfirm, show the as-is cost, then optionally
  revise SKUs/quantities and compare the revised value with the immutable as-is snapshot.
- Seller-directed ME3, ME5, ME7, Copilot, add, replace, remove, and quantity changes are
  priceable without asserting that an add-on is bundled. Generic recommendation requests
  ask for a capability/target and user count until the authoritative rule sheet exists.
- Promotion and best-eligible-price logic is deliberately inactive until the separately
  maintained business rule sheet is approved and integrated.
- `WORKFLOW_MODE=upgrade_comparison` remains available for the prior four-option workflow.
- `WORKFLOW_MODE=scenario_comparison` is reserved for a future approved bundling dataset.
- The required workflow options identify E3, E5, E7, and standalone Copilot by exact title;
  their ProductId, SkuId, term, billing plan, and prices are resolved from the current sheet.
- `config/migration_seed.json` is a separate, human-maintained advisory layer. Each row
  records provenance as `microsoft_official`, `third_party_sourced`, or
  `heuristic_unverified`, with URL/date metadata required for sourced rows. All checked-in
  rows remain `approved=false`; provenance improves review context but cannot change a
  disposition. An authorized business reviewer can promote an individual row to explicit
  configuration by setting `approved=true`. No model-generated mapping is accepted at runtime.
- Exact E3/E5/E7 core-suite rows can safely feed the target base quantity. In comparison
  mode, every other existing SKU is retained and priced. The seller can explicitly remove,
  replace, or reclassify it; no inferred mapping blocks the comparison.
- The current sheet contains no field proving that ME7 includes Copilot, so the application
  does not make that claim or silently price it as bundled.

## Production security

- Verify `X-Hub-Signature-256` before parsing or queuing a webhook.
- Keep Meta `X-Hub-Signature-256` verification mandatory for every inbound request.
- Seller access is fail-closed. This deployment uses `WHATSAPP_ALLOW_ALL_SELLERS=false`
  with an explicit E.164 seller allowlist; other senders are ignored without entering the workflow.
- Use App Service/Container App Managed Identity with these data-plane roles:
  - Storage Blob Data Reader scoped to the pricing container
  - Storage Blob Data Contributor scoped to the workflow container
- Under the approved no-new-resource constraint, store the Meta app secret, access token, and OpenAI API key as encrypted App Service settings. Access is limited by Azure RBAC, but this is less isolated than Key Vault and is an accepted constraint.
- Do not log raw webhook bodies, uploaded bytes, full normalized estates, access tokens, or rate-card contents.
- Apply storage firewall/private endpoints according to the organization's network policy.

## Reliability and operations

- Monitor the Blob webhook pending/dead-letter prefixes and validate restart recovery and duplicate delivery during UAT.
- Configure App Service health checks against `/health/ready`.
- Keep one application instance on the existing B1 plan; the Blob inbox intentionally processes sequentially. Reassess the dispatch design before any future scale-out.
- Alert on webhook signature failures, queue age, processing failures, rate-card refresh failures, Blob conditional-write conflicts, and outbound Meta failures.
- Back up and version the rate-card Blob.
- Configure and verify a lifecycle deletion rule scoped to the workflow-container/session prefix.

## Workbook boundary

No further reference-licensing dataset is required for as-is distributor pricing. A seller
can provide each opportunity as text, voice, image, spreadsheet, Word document, or PDF.
The application deliberately does not automate entitlement-level or promotion decisions
that the workbook/rule sheet cannot support; it asks for a seller-directed target instead.

If the business later wants automatic add-on entitlement decisions or an assertion that ME7
includes Copilot, those facts must first be added as maintained columns or a maintained sheet
inside the same workbook. Until then, seller review is the auditable production behavior.
