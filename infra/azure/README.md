# Existing Azure deployment

The manager-approved deployment replaces Phase 1 in the existing resources:

- resource group: `rg-skysecure-microsoft-pricing-agent-dev`;
- Linux B1 plan: `asp-skysecure-microsoft-pricing-agent-dev`;
- Web App: `skysecure-microsoft-pricing-agent-dev`;
- Storage Account: `stskysecureprice48eb`;
- Blob containers: `pricing-workbooks` and `licensing-workflows`.

No second App Service plan, Web App, Service Bus, Key Vault, Cosmos DB, Log Analytics
workspace, or Application Insights resource is created. Durable webhook ingress uses a
leased queue prefix inside the existing `licensing-workflows` Blob container.

`foundation.bicep` updates only the existing Web App runtime configuration. Always verify
an automatic App Service backup is restorable before applying it, then run a what-if:

```azurecli
az deployment group what-if \
  --resource-group rg-skysecure-microsoft-pricing-agent-dev \
  --template-file infra/azure/foundation.bicep \
  --parameters @infra/azure/production.parameters.json
```

The output must show a change only to:

```text
Microsoft.Web/sites/skysecure-microsoft-pricing-agent-dev/config/web
```

After review:

```azurecli
az deployment group create \
  --name ssp-licensing-existing-app-config \
  --resource-group rg-skysecure-microsoft-pricing-agent-dev \
  --template-file infra/azure/foundation.bicep \
  --parameters @infra/azure/production.parameters.json
```

`appsettings.example.json` is a checklist, not a secret-bearing deployment file. Enter
OpenAI and Meta values directly in App Service > Environment variables and never commit
the completed values. Follow the production runbook before replacing application code.
