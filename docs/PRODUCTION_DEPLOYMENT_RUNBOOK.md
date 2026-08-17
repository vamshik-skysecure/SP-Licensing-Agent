# SSP Licensing Agent in-place production deployment

## 1. Approved deployment decision

The manager has directed this release to replace Phase 1 in the existing Azure resources so
that no second fixed-cost platform is created.

| Resource | Verified state | Treatment |
|---|---|---|
| `rg-skysecure-microsoft-pricing-agent-dev` | Existing resource group in Central India | Reuse. |
| `asp-skysecure-microsoft-pricing-agent-dev` | Linux B1, one instance, no deployment slots | Reuse without changing tier. |
| `skysecure-microsoft-pricing-agent-dev` | Existing Phase 1 Web App | Back up, then replace in place. |
| `stskysecureprice48eb` | Existing Storage Account | Reuse for pricing, sessions, and durable webhook ingress. |
| `pricing-workbooks` | Existing container | Preserve earlier workbooks and add `active/Microsoft_SKU_V6.0_Distributor.xlsx`. |
| `licensing-workflows` | Existing container | Store sessions and persisted webhook work. |

No second App Service plan, Web App, Service Bus, Key Vault, Cosmos DB, Log Analytics
workspace, or Application Insights resource is part of this deployment.

Read-only verification on 2026-08-08 found Blob versioning and Blob soft delete disabled.
Therefore the Phase 1 and V5 workbooks must not be overwritten; V6 uses a separate blob name.

## 2. Architecture within the existing resources

The existing Web App runs one Python 3.12 Uvicorn process. The application uses:

- the standard OpenAI Python SDK for natural-language routing and multimodal extraction;
- deterministic SKU matching, pricing, calculations, state transitions, and PDF/image rendering;
- the maintained workbook in `pricing-workbooks` as the pricing source;
- ETag-protected JSON sessions under `licensing-workflows/sessions/`;
- a durable webhook inbox under `licensing-workflows/webhook-queue/pending/`;
- Blob leases to prevent overlapping processing;
- sequential processing on the single B1 instance to preserve received-message order;
- automatic retry and `licensing-workflows/webhook-queue/dead-letter/` after five failures.

The webhook returns success only after the signed Meta message has been written to Blob.
This prevents a B1 restart from silently losing an already acknowledged message without
introducing a separately billed queue service.

## 3. Accepted platform limitations

- B1 has no deployment slots, so the replacement requires a controlled maintenance window.
- The plan showed approximately 75% memory utilization at the time of the supplied screenshot.
  Monitor memory during UAT; the manager has accepted reuse rather than a separate plan.
- A single B1 instance has lower throughput and availability than an isolated multi-instance
  production plan. Blob persistence protects work, but does not add compute redundancy.
- Without Key Vault, OpenAI and Meta secrets are stored as encrypted App Service application
  settings and are visible to sufficiently privileged Azure operators.
- Without Application Insights, initial operations rely on App Service logs and Azure metrics.

These are explicit cost-constrained design decisions, not claims that the limitations do not exist.

## 4. Mandatory rollback gate

Do not replace Phase 1 until a restorable App Service backup is confirmed.

1. Open `skysecure-microsoft-pricing-agent-dev` in Azure Portal.
2. Select **Backups**.
3. Confirm a recent successful automatic backup exists. Basic tier supports automatic backup
   and restore of the production slot.
4. Record the backup timestamp and take a screenshot.
5. Open the backup entry and confirm that **Restore** is available, but do not restore it.
6. Record the current startup command, Python version, environment-variable names, and Meta
   callback URL for rollback evidence. Never paste secret values into tickets or documents.
7. Preserve `pricing-workbooks/active/Microsoft_SKU_Active.xlsx` unchanged. It is the older
   Phase 1 workbook and is part of the rollback boundary.
8. Upload the tested `docs/microsoft_sku_v6_distributor.xlsx` as the new blob
   `pricing-workbooks/active/Microsoft_SKU_V6.0_Distributor.xlsx` without overwriting earlier blobs.
9. Record the V6 Blob ETag, last-modified time, size, and SHA-256. Its expected local SHA-256 is
   `0da7ef261d987722e4a8c4af3d6a8fa46ecef5f96f2d01c77ceb2023df8ee86e`.

Stop if no successful App Service backup is available.

## 5. Security and access gate

The existing Web App's system-assigned managed identity is enabled with principal ID:

```text
7e4a362c-2698-44e8-93b8-f091ecdb5648
```

An Owner, User Access Administrator, or Role Based Access Control Administrator must verify or
assign these no-cost data-plane roles to that identity:

| Scope | Role |
|---|---|
| `stskysecureprice48eb/pricing-workbooks` | Storage Blob Data Reader |
| `stskysecureprice48eb/licensing-workflows` | Storage Blob Data Contributor |

The inherited Contributor role cannot create role assignments. Do not use the Phase 1 Storage
Account key in production unless the security owner explicitly approves the documented fallback:
`RATE_CARD_STORAGE_CONNECTION_STRING` plus `ALLOW_CONNECTION_STRINGS_IN_PRODUCTION=true`.

The OpenAI key was previously pasted into project chat and must be treated as disclosed. The CEO
has approved reuse for this release. Record that exception. Enter the key only in the App Service
environment-variable editor; never put it in Git, Bicep parameters, deployment output, or logs.

## 6. Replace Phase 1 configuration

First run the Bicep what-if documented in `infra/azure/README.md`. It must show only the existing
Web App `config/web` changing. The desired configuration is:

- Python 3.12;
- startup command `python main.py`;
- Always On enabled;
- HTTP/2 enabled;
- HTTPS Only enabled;
- minimum TLS 1.2;
- health check `/health/ready`;
- one B1 instance;
- system-assigned managed identity enabled.

Use `infra/azure/appsettings.example.json` as the authoritative list of new settings. In Azure
Portal > App Service > Environment variables:

1. Add the complete new setting set.
2. Enter the OpenAI and Meta values directly in the portal.
3. Confirm `RUNTIME_PROFILE=production`.
4. Confirm `MESSAGE_DISPATCH_BACKEND=azure_blob`.
5. Confirm `STORAGE_MODE=azure_blob` and `ALLOW_CONNECTION_STRINGS_IN_PRODUCTION=false` when
   managed identity access is available.
6. Remove Phase 1-only environment variables only after the backup timestamp has been recorded.
7. Save a names-only screenshot of the final settings; do not expose values.

The application intentionally refuses production startup when Meta credentials, an explicit
seller access mode, Blob configuration, durable dispatch, OpenAI configuration, or startup model
validation is missing.

## 7. Controlled code replacement

1. Schedule a quiet maintenance window and tell test sellers not to submit files or edits.
2. Record the current Git commit and successful App Service backup timestamp.
3. Run the complete CI suite and tracked-secret scan on the deployment commit.
4. Build the deployment ZIP from `app/`, `config/`, `main.py`, and `requirements.txt`, with those
   entries at the root of the ZIP.
5. Deploy the ZIP to `skysecure-microsoft-pricing-agent-dev` using Kudu/ZIP deployment. ZIP
   deployment removes files left from the previous deployment and runs dependency restoration.
6. Apply the reviewed runtime configuration and new environment variables.
7. Restart the Web App once and wait for startup completion.

Do not delete the existing Web App or App Service plan. The code and configuration are replaced;
the Azure resource identity, hostname, plan, and current Meta callback URL remain the same.

## 8. Pre-message validation

Verify these endpoints using the existing hostname:

```text
GET https://skysecure-microsoft-pricing-agent-dev.azurewebsites.net/health/live
GET https://skysecure-microsoft-pricing-agent-dev.azurewebsites.net/health/ready
GET https://skysecure-microsoft-pricing-agent-dev.azurewebsites.net/privacy-policy
```

Expected readiness evidence:

```json
{
  "status": "ready",
  "rate_card_version": "<nonempty Blob ETag>",
  "price_rows": "<positive integer>",
  "workflow_store": "azure_blob",
  "dispatch": "azure_blob"
}
```

Also confirm:

- no unresolved application settings;
- the current workbook loaded from `active/Microsoft_SKU_V6.0_Distributor.xlsx`;
- no secret, phone number, raw message, workbook row, or uploaded content appears in logs;
- memory remains stable after startup.

## 9. Meta and end-to-end UAT

When the production Meta number and system-user token are available:

1. Keep the existing callback URL if it already targets this Web App; otherwise set
   `https://skysecure-microsoft-pricing-agent-dev.azurewebsites.net/api/whatsapp/webhook`.
2. Enter the same webhook verify token stored in App Service settings.
3. Verify and save, then subscribe to `messages`.
4. Set `WHATSAPP_ALLOW_ALL_SELLERS=false` and populate `WHATSAPP_SELLER_ALLOWLIST`
   with the approved E.164 seller numbers. Meta webhook signature verification remains mandatory.
5. Run text, spreadsheet, image, PDF/Word, and voice capture using synthetic data.
6. Confirm correction, seller validation, as-is pricing, optional recommendations/changes,
   revised pricing, final validation, and customer-ready PDF delivery.
7. Restart the app after persisting a test message and verify processing resumes from Blob.
8. Confirm a duplicate Meta delivery does not apply an edit twice.
9. Inspect `webhook-queue/pending` and `webhook-queue/dead-letter` for stuck work.

## 10. Rollback

If readiness or UAT fails:

1. Stop the Web App to prevent further workflow mutations.
2. Open App Service > Backups.
3. Restore the recorded pre-deployment backup over the existing production slot.
4. Restore the previous Meta callback only if it was changed.
5. Start the app and run the Phase 1 smoke test.
6. Preserve failed logs and Blob queue/session data for diagnosis; do not delete them.

## 11. Production acceptance evidence

- full automated test output and secret scan;
- pre-deployment App Service backup timestamp and restore availability;
- Bicep what-if showing only the existing Web App configuration change;
- managed-identity Blob role evidence or an approved connection-string exception;
- workbook backup, ETag, and last-modified time;
- `/health/ready` output;
- Meta signature and explicit public-access configuration evidence;
- Blob ingress restart, retry, duplicate, and dead-letter tests;
- privacy/security review;
- business UAT sign-off.

## 12. Authoritative references

- [Back up and restore an Azure App Service app](https://learn.microsoft.com/en-us/azure/app-service/manage-backup)
- [Deploy files to Azure App Service](https://learn.microsoft.com/en-us/azure/app-service/deploy-zip)
- [Azure built-in RBAC roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles)
- [Configure Python on Azure App Service](https://learn.microsoft.com/en-us/azure/app-service/configure-language-python)
