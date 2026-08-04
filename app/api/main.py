from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse

from app.api.dependencies import lifespan
from app.api.router import api_router

app = FastAPI(lifespan=lifespan)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/live", tags=["health"])
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready", tags=["health"])
async def readiness(request: Request) -> dict[str, str | int]:
    catalog = await request.app.state.rate_cards.get()
    settings = request.app.state.settings
    request.app.state.scenario_engine.validate_catalog(
        catalog,
        term_duration=settings.default_term_duration,
        billing_plan=settings.default_billing_plan,
        segment=settings.default_customer_segment,
    )
    return {
        "status": "ready",
        "rate_card_version": catalog.version,
        "price_rows": len(catalog.items),
        "workflow_store": request.app.state.settings.workflow_store_backend,
        "dispatch": request.app.state.settings.message_dispatch_backend,
    }


@app.get("/privacy-policy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_policy() -> str:
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Pricing Agent Bot Privacy Policy</title>
</head>
<body>
  <main>
    <h1>Pricing Agent Bot Privacy Policy</h1>
    <p>Effective date: July 31, 2026</p>
    <p>Pricing Agent Bot processes WhatsApp messages and documents you send to provide Microsoft licensing analysis and pricing quotes.</p>
    <h2>Information We Process</h2>
    <p>We process your WhatsApp phone number, message content, and documents you submit. Documents may contain tenant licensing information.</p>
    <h2>How We Use Information</h2>
    <p>We use this information only to respond to your request, analyze submitted tenant documents, and generate pricing quotes.</p>
    <h2>Sharing</h2>
    <p>Messages are handled through Meta's WhatsApp Cloud API. When natural-language routing is enabled, the seller's text and a limited workflow summary are processed by the OpenAI API to identify the requested action. Uploaded file bytes and the pricing workbook are not sent to the language model.</p>
    <h2>Retention</h2>
    <p>Submitted source documents are processed in memory and discarded after analysis. Normalized licence records, seller decisions, proposal revisions, and generated commercial scenarios may be retained by the providing business according to its configured retention policy.</p>
    <h2>Your Choices</h2>
    <p>You may stop using the bot at any time. To request deletion of data associated with a request, contact the business that provided this bot.</p>
    <h2>Changes</h2>
    <p>We may update this policy when the bot's data practices change.</p>
  </main>
</body>
</html>"""


app.include_router(api_router, prefix="/api")
