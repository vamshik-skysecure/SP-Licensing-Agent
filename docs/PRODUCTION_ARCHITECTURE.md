# Production Architecture Decision

## Decision

The draft PRD's domain boundaries are retained, but the seven-agent supervisor design is replaced by a deterministic persisted workflow.

This workflow is commercial software: SKU identity, migration action, quantities, prices, discounts, and totals must be reproducible and auditable. The official OpenAI Python SDK and Responses API translate natural language into one validated command, but the model is not an authority for commercial state changes. Buttons and explicit commands remain available as deterministic fallbacks.

The linked `ssp-v2-boilerplate` repository was assessed and is not used as a code base. Its own guide identifies it as an early scaffold with empty prompts/tools, an invalid model wrapper, no graph, no WhatsApp route, no tests, and no production wiring. Its data-contract and acceptance-criteria ideas informed this implementation.

## Target topology

```text
WhatsApp Cloud API
        |
        v
FastAPI webhook -- signature + allowlist + size/type checks
        |
        v
Azure Service Bus queue -- duplicate detection, retries, DLQ
        |
        v
OpenAI Responses API intent adapter (free-form messages only)
        |
        v
Deterministic licensing workflow
   |               |              |
   v               v              v
Analysis      Scenario/pricing   PDF renderer
              engine
   |               |
   +---------------+
              |
       Azure Blob JSON state (ETag concurrency)

Azure Blob Storage -> maintained Excel workbook -> Outcome Sheet cache
```

## Why this differs from the draft PRD

- A handoff-only LLM supervisor is unnecessary for a four-option state machine.
- Seven agents create more model calls, latency, failure paths, prompt/version management, and trace volume without improving deterministic calculations.
- The edit operation must recalculate and persist atomically. That belongs in one application transaction, not a conversation between agents.
- Service Bus is added because FastAPI background tasks are not durable across process restarts or deployments.
- Managed Identity is the production default for Azure data services.
- The rate-card workbook is read directly, so the business can update the Outcome Sheet without coordinating changes with hidden Phase 1 code.

The intent adapter uses a strict JSON Schema and receives only the seller message plus a
bounded workflow summary. It never receives the workbook or uploaded file bytes. Its output
is validated again before the same deterministic operations used by buttons and commands run.

LangGraph can still be introduced later if genuine long-running human interrupts require it. If introduced, use a deterministic `StateGraph` and an appropriate durable checkpointer; do not move pricing or migration decisions into model nodes.

## Data ownership

### Blob Storage

- One maintained `.xlsx` workbook in a private pricing container.
- Configured worksheet: `Outcome Sheet`.
- Application caches the parsed catalog for five minutes and records the Blob ETag/content digest as the rate-card version.
- A separate private workflow container in the same Storage account holds one JSON blob per hashed seller thread.
- Blob ETags and `If-Match` conditional writes provide optimistic concurrency and prevent lost updates.
- Expired sessions are ignored by the application; a prefix-scoped lifecycle rule deletes old session blobs.
- Raw customer uploads are never persisted. Only normalized estate and proposal state are stored.

### Excel-only commercial authority

- `Outcome Sheet` is the only licensing catalogue and pricing source.
- The required workflow options identify E3, E5, E7, and standalone Copilot by exact title;
  their ProductId, SkuId, term, billing plan, and prices are resolved from the current sheet.
- No separate migration JSON, database, hidden Phase 1 service, or model-generated licensing
  knowledge is used.
- Exact E3/E5/E7 core-suite rows can safely feed the target base quantity. Every other
  existing SKU defaults to `needs_decision`, remains priced, and requires an explicit seller
  action before finalization because the workbook contains no entitlement mappings.
- The current sheet contains no field proving that ME7 includes Copilot, so the application
  does not make that claim or silently price it as bundled.

## Production security

- Verify `X-Hub-Signature-256` before parsing or queuing a webhook.
- Enforce the configured seller phone-number allowlist.
- Use App Service/Container App Managed Identity with these data-plane roles:
  - Storage Blob Data Contributor scoped to the pricing and workflow containers
  - Azure Service Bus Data Sender/Receiver
- Store the Meta app secret, access token, and OpenAI API key as Key Vault references.
- Do not log raw webhook bodies, uploaded bytes, full normalized estates, access tokens, or rate-card contents.
- Apply storage firewall/private endpoints according to the organization's network policy.

## Reliability and operations

- Configure Service Bus Standard or Premium with duplicate detection, Peek-Lock receive mode, dead lettering, and an alert on DLQ depth.
- Configure App Service health checks against `/health/ready`.
- Enable two or more application instances only with Blob workflow persistence and Service Bus enabled.
- Alert on webhook signature failures, queue age, processing failures, rate-card refresh failures, Blob conditional-write conflicts, and outbound Meta failures.
- Back up and version the rate-card Blob.
- Configure and verify a lifecycle deletion rule scoped to the workflow-container/session prefix.

## Workbook boundary

No further reference-licensing dataset is required. The customer licence upload is still
required per opportunity because it supplies the customer's current quantities and renewal
dates. The application deliberately does not automate entitlement-level decisions that the
workbook cannot support: it preserves and prices those licences and presents the seller with
the required retain, migrate, include, or remove controls.

If the business later wants automatic add-on entitlement decisions or an assertion that ME7
includes Copilot, those facts must first be added as maintained columns or a maintained sheet
inside the same workbook. Until then, seller review is the auditable production behavior.
