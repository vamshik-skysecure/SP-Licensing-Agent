# Local CEO demo mode

This mode runs the same V1 licensing workflow locally while leaving the deployed Azure
App Service and its Blob data unchanged.

## Runtime profiles

| Profile | Pricing source | Sessions | Webhook handling | Intended use |
|---|---|---|---|---|
| `local_demo` | `docs/microsoft_sku_v6_distributor.xlsx` | In memory | Direct | ngrok + Meta test number |
| `production` | Azure Blob | Azure Blob | Durable Blob inbox | Azure App Service |

`RUNTIME_PROFILE` overrides the related low-level backend settings as a group. If it is
unset, the older individual environment settings remain supported.

## Before the demo

Keep real secrets only in the ignored `.env` file. For local demo mode it must contain:

- `OPENAI_API_KEY`
- credentials for a Meta test app/number that is safe to point at ngrok;
- `WHATSAPP_SELLER_ALLOWLIST` containing the presenter's WhatsApp number;
- `WORKFLOW_MODE=simple_pricing`.

The local V6 workbook uses the final `Outcome Sheet` and the
`Expectec Disti Price to Skysecure` column (`SIMPLE_PRICE_BASIS=distributor_expected`).
It is the same commercial basis configured for the deployed workflow.

Do not redirect the active company Marketplace webhook to ngrok. Use a Meta test app or a
dedicated test number whose callback can be changed without affecting another service.

## Terminal 1: start the application

```powershell
.\scripts\start_local_demo.cmd
```

The command wrapper uses a process-only PowerShell execution-policy bypass for this project
script. It does not modify the user or machine execution policy. The launcher sets
`RUNTIME_PROFILE=local_demo` only for its own process, prints a secret-free configuration
summary, and starts the application on port 8000.

To use another local port:

```powershell
.\scripts\start_local_demo.cmd -Port 8010
```

Verify:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health/ready
```

Expected profile/backends:

```text
runtime_profile: local_demo
workflow_store: memory
dispatch: direct
```

## Terminal 2: expose the application

```powershell
ngrok http 8000
```

Copy the HTTPS forwarding URL and append:

```text
/api/whatsapp/webhook
```

For example:

```text
https://<current-ngrok-host>/api/whatsapp/webhook
```

Enter that callback in the Meta **test app**, use the value already stored in
`WHATSAPP_WEBHOOK_VERIFY_TOKEN`, click **Verify and save**, and subscribe to `messages`.

## After the demo

Stop ngrok and the local Python process with `Ctrl+C`. Local sessions are intentionally
lost when the application stops. Azure continues running with its own App Service settings;
no production toggle or redeployment is required.
