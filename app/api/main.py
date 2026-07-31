from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from app.api.dependencies import lifespan
from app.api.router import api_router

app = FastAPI(lifespan=lifespan)


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


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
    <p>Messages are handled through Meta's WhatsApp Cloud API. Documents and quote requests are sent to the pricing service needed to generate your requested analysis or quote.</p>
    <h2>Retention</h2>
    <p>Submitted documents are processed in memory and discarded after analysis. Generated quote results are retained in memory for up to 30 minutes so you can navigate interactive pricing options, then automatically deleted.</p>
    <h2>Your Choices</h2>
    <p>You may stop using the bot at any time. To request deletion of data associated with a request, contact the business that provided this bot.</p>
    <h2>Changes</h2>
    <p>We may update this policy when the bot's data practices change.</p>
  </main>
</body>
</html>"""


app.include_router(api_router, prefix="/api")
