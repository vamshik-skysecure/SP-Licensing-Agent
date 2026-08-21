import hashlib
import hmac
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from pydantic import ValidationError

from app.api.dependencies import get_settings, get_webhook_dispatcher
from app.config import Settings, get_logger
from app.core.dispatch import WebhookDispatcher
from app.schema.whatsapp import WhatsAppWebhookPayload

router = APIRouter()
logger = get_logger(__name__)


@router.get("/webhook", response_class=Response)
async def verify_webhook(
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
    settings: Settings = Depends(get_settings),
) -> Response:
    logger.info("Webhook verification requested mode=%s", hub_mode)
    if (
        hub_mode != "subscribe"
        or hub_verify_token != settings.whatsapp_webhook_verify_token
        or hub_challenge is None
    ):
        logger.warning("Webhook verification rejected")
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Webhook verification failed.",
        )
    logger.info("Webhook verification completed")
    return Response(content=hub_challenge, media_type="text/plain")


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    dispatcher: Annotated[WebhookDispatcher, Depends(get_webhook_dispatcher)],
) -> dict[str, str]:
    logger.info("Webhook request received")
    raw_body = await _read_bounded_body(request, settings.max_webhook_bytes)
    _validate_signature(raw_body, request.headers.get("X-Hub-Signature-256"), settings)
    logger.info("Webhook signature validated bytes=%d", len(raw_body))

    try:
        webhook = WhatsAppWebhookPayload.model_validate_json(raw_body)
    except ValidationError as error:
        logger.warning("Webhook payload validation failed")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid WhatsApp webhook payload.",
        ) from error

    if webhook.object != "whatsapp_business_account":
        logger.info("Webhook ignored object=%s", webhook.object)
        return {"status": "ignored"}

    logger.info("Webhook payload accepted entries=%d", len(webhook.entry))
    await dispatcher.dispatch(raw_body, webhook, background_tasks)
    logger.info("Webhook processing dispatched")
    return {"status": "ok"}


async def _read_bounded_body(request: Request, max_bytes: int) -> bytes:
    """Read the signed Meta payload without permitting unbounded request buffering."""

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = -1
        if declared_length > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook payload is too large.",
            )

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Webhook payload is too large.",
            )
        body.extend(chunk)
    return bytes(body)


def _validate_signature(raw_body: bytes, signature: str | None, settings: Settings) -> None:
    expected_signature = "sha256=" + hmac.new(
        settings.whatsapp_app_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if signature is None or not hmac.compare_digest(signature, expected_signature):
        logger.warning("Webhook signature validation failed")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid WhatsApp webhook signature.",
        )
