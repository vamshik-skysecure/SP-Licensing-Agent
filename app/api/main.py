from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import lifespan
from app.api.router import api_router
from app.config import get_logger

logger = get_logger(__name__)

app = FastAPI(
    title="SkySecure Microsoft Licensing Advisor",
    description="Signed WhatsApp workflow for auditable Microsoft licensing capture and pricing.",
    version="1.0.0",
    lifespan=lifespan,
)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def service_home() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>SkySecure Microsoft Licensing Advisor</title>
</head>
<body>
  <main>
    <h1>SkySecure Microsoft Licensing Advisor</h1>
    <p>The service is online. Seller interactions are handled through the configured WhatsApp Business number.</p>
    <p><a href="/health/live">Service status</a> | <a href="/privacy-policy">Privacy notice</a></p>
  </main>
</body>
</html>"""


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
async def readiness(request: Request) -> dict[str, str | int]:
    settings = request.app.state.settings
    whatsapp_client = request.app.state.whatsapp_client
    dispatcher = request.app.state.webhook_dispatcher
    if not whatsapp_client.credentials_valid:
        logger.warning("Readiness check failed component=whatsapp_credentials")
        raise HTTPException(status_code=503, detail="Service dependencies are not ready.")
    if not dispatcher.is_running:
        logger.warning("Readiness check failed component=webhook_dispatcher")
        raise HTTPException(status_code=503, detail="Service dependencies are not ready.")
    try:
        await request.app.state.workflow_store.check_health()
        catalog = await request.app.state.rate_cards.get()
        request.app.state.scenario_engine.validate_catalog(
            catalog,
            term_duration=settings.default_term_duration,
            billing_plan=settings.default_billing_plan,
            segment=settings.default_customer_segment,
        )
    except Exception as error:
        logger.warning(
            "Readiness dependency check failed error_type=%s",
            type(error).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="Service dependencies are not ready.",
        ) from None
    return {
        "status": "ready",
        "runtime_profile": settings.effective_runtime_profile,
        "rate_card_version": catalog.version,
        "price_rows": len(catalog.items),
        "workflow_store": request.app.state.settings.workflow_store_backend,
        "workflow_store_status": "ready",
        "dispatch": request.app.state.settings.message_dispatch_backend,
        "dispatcher_status": "running",
        "whatsapp_credentials": "valid",
    }


@app.get("/privacy-policy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_policy() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Microsoft Licensing Advisor Privacy Notice</title>
</head>
<body>
  <main>
    <h1>Microsoft Licensing Advisor Privacy Notice</h1>
    <p>Effective date: August 8, 2026</p>
    <p>The Microsoft Licensing Advisor processes WhatsApp messages and files you submit to capture licensing requirements and prepare pricing proposals.</p>
    <h2>Information We Process</h2>
    <p>We process your WhatsApp phone number, message content, voice notes, images, and documents you submit. These inputs may contain customer licensing information.</p>
    <h2>How We Use Information</h2>
    <p>We use this information to capture and confirm licensing requirements, interpret seller-directed changes, calculate prices from the maintained catalogue, and generate proposal outputs.</p>
    <h2>Service Providers and AI Processing</h2>
    <p>Messages and attachments are transported through Meta's WhatsApp Cloud API. Seller text and a bounded workflow summary may be processed by the OpenAI API for structured intent routing. Voice notes are processed for transcription. Images, PDFs, Word documents, and spreadsheet layouts that the deterministic parser cannot map may be processed by the OpenAI API for structured requirement extraction. Standard supported spreadsheet layouts are parsed locally. The maintained pricing workbook is never sent to OpenAI, and all SKU matching, price selection, calculations, and workflow mutations are performed deterministically by this application.</p>
    <h2>Retention</h2>
    <p>Submitted source files are processed transiently and are not retained as source files by the application. Signed inbound webhook work is held temporarily in the durable workflow queue for delivery and failure recovery. Normalized licensing records, seller decisions, proposal revisions, and processed-message identifiers become unavailable to the conversation after the configured session period. Physical deletion of expired session and queue records is governed by the Azure Storage lifecycle policy configured by the service owner. Application logging is designed not to record raw message bodies, uploaded file contents, pricing rows, phone numbers, access tokens, or API keys.</p>
    <h2>Your Choices</h2>
    <p>You may stop using the bot at any time. To request deletion of data associated with a request, contact the business that provided this bot.</p>
    <h2>Changes</h2>
    <p>We may update this policy when the bot's data practices change.</p>
  </main>
</body>
</html>"""


app.include_router(api_router, prefix="/api")
