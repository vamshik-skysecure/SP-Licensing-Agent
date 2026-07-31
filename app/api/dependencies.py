from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request

from app.api.whatsapp.service import WhatsAppWebhookService
from app.config import Settings, configure_logging, get_logger
from app.core.agent.main import PricingAgentClient
from app.core.whatsapp import WhatsAppAPIError, WhatsAppClient

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    configure_logging(settings.log_level)
    logger.info("Application startup started")
    whatsapp_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0),
        transport=httpx.AsyncHTTPTransport(retries=settings.whatsapp_connect_retries),
    )
    pricing_http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(settings.pricing_agent_timeout_seconds)
    )

    app.state.settings = settings
    app.state.whatsapp_client = WhatsAppClient(
        http_client=whatsapp_http_client,
        access_token=settings.whatsapp_access_token,
        phone_number_id=settings.whatsapp_phone_number_id,
        base_url=settings.whatsapp_uri,
        api_version=settings.whatsapp_uri_version,
    )
    logger.info(
        "WhatsApp endpoint configured host=%s api_version=%s connect_retries=%d",
        urlsplit(settings.whatsapp_uri).hostname,
        settings.whatsapp_uri_version,
        settings.whatsapp_connect_retries,
    )
    try:
        await app.state.whatsapp_client.validate_credentials()
    except WhatsAppAPIError as error:
        if error.status_code is not None:
            logger.critical(
                "WhatsApp credentials are invalid: status=%s response=%s. "
                "Replace WHATSAPP_ACCESS_TOKEN and restart the application.",
                error.status_code,
                error.response_body,
            )
        else:
            logger.error("WhatsApp credential validation could not reach Meta: %r", error.__cause__)
    app.state.pricing_agent_client = PricingAgentClient(
        http_client=pricing_http_client,
        base_url=settings.pricing_agent_base_url,
        api_key=settings.pricing_agent_api_key,
    )
    app.state.whatsapp_webhook_service = WhatsAppWebhookService(
        whatsapp_client=app.state.whatsapp_client,
        pricing_agent_client=app.state.pricing_agent_client,
    )
    logger.info("Application dependencies initialized")

    try:
        logger.info("Application startup completed")
        yield
    finally:
        logger.info("Application shutdown started")
        await whatsapp_http_client.aclose()
        await pricing_http_client.aclose()
        logger.info("Application shutdown completed")


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_whatsapp_webhook_service(request: Request) -> WhatsAppWebhookService:
    return request.app.state.whatsapp_webhook_service
