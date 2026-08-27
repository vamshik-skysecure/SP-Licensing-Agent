import hashlib
import hmac
import json
import logging
import unittest
from types import SimpleNamespace

import httpx
from fastapi import BackgroundTasks, HTTPException, Request
from pydantic import SecretStr

from app.api.whatsapp.handler import receive_webhook
from app.config.logging import RedactingFormatter, redact_log_text
from app.core.dispatch import _dispatch_units
from app.core.whatsapp import WhatsAppAPIError, WhatsAppClient
from app.schema.whatsapp import TextContent, WhatsAppTextMessage, WhatsAppWebhookPayload


def _signed_request(body: bytes, secret: str, *, valid: bool = True) -> Request:
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not valid:
        digest = "0" * 64
    messages = iter(
        [{"type": "http.request", "body": body, "more_body": False}]
    )

    async def receive() -> dict[str, object]:
        return next(messages)

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/whatsapp/webhook",
            "headers": [
                (b"x-hub-signature-256", f"sha256={digest}".encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        },
        receive,
    )


def _message_body(phone_number_id: str | None) -> bytes:
    value: dict[str, object] = {
        "messages": [
            {
                "id": "wamid.ingress-security-1",
                "from": "919111111111",
                "timestamp": "1700000001",
                "type": "text",
                "text": {"body": "Office 365 E1, 10 licences"},
            }
        ]
    }
    if phone_number_id is not None:
        value["metadata"] = {
            "display_phone_number": "+1 555 000 0000",
            "phone_number_id": phone_number_id,
        }
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [{"changes": [{"value": value}]}],
        },
        separators=(",", ":"),
    ).encode()


def _mixed_phone_body(configured_phone_id: str, foreign_phone_id: str) -> bytes:
    def change(phone_id: str, message_id: str, sender: str) -> dict[str, object]:
        return {
            "value": {
                "metadata": {
                    "display_phone_number": "+1 555 000 0000",
                    "phone_number_id": phone_id,
                },
                "messages": [
                    {
                        "id": message_id,
                        "from": sender,
                        "timestamp": "1700000001",
                        "type": "text",
                        "text": {"body": "Office 365 E1, 10 licences"},
                    }
                ],
            }
        }

    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        change(
                            foreign_phone_id,
                            "wamid.ingress-security-foreign",
                            "919222222222",
                        ),
                        change(
                            configured_phone_id,
                            "wamid.ingress-security-owned",
                            "919111111111",
                        ),
                    ]
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.payloads: list[WhatsAppWebhookPayload] = []

    async def dispatch(
        self,
        raw_body: bytes,
        webhook: WhatsAppWebhookPayload,
        background_tasks: BackgroundTasks,
    ) -> None:
        del raw_body, background_tasks
        self.payloads.append(webhook)


class WhatsAppIngressBindingTests(unittest.IsolatedAsyncioTestCase):
    secret = "webhook-secret-for-tests"
    configured_phone_id = "1164810520058946"

    def _settings(self) -> SimpleNamespace:
        return SimpleNamespace(
            max_webhook_bytes=1024 * 1024,
            whatsapp_app_secret=SecretStr(self.secret),
            whatsapp_phone_number_id=self.configured_phone_id,
        )

    async def test_signed_message_for_configured_phone_is_dispatched(self) -> None:
        body = _message_body(self.configured_phone_id)
        dispatcher = _RecordingDispatcher()

        response = await receive_webhook(
            _signed_request(body, self.secret),
            BackgroundTasks(),
            self._settings(),  # type: ignore[arg-type]
            dispatcher,  # type: ignore[arg-type]
        )

        self.assertEqual(response, {"status": "ok"})
        self.assertEqual(len(dispatcher.payloads), 1)

    async def test_signed_message_for_other_phone_is_acknowledged_but_not_dispatched(
        self,
    ) -> None:
        body = _message_body("9999999999999999")
        dispatcher = _RecordingDispatcher()

        response = await receive_webhook(
            _signed_request(body, self.secret),
            BackgroundTasks(),
            self._settings(),  # type: ignore[arg-type]
            dispatcher,  # type: ignore[arg-type]
        )

        self.assertEqual(response, {"status": "ignored"})
        self.assertEqual(dispatcher.payloads, [])

    async def test_message_without_target_metadata_is_not_dispatched(self) -> None:
        body = _message_body(None)
        dispatcher = _RecordingDispatcher()

        response = await receive_webhook(
            _signed_request(body, self.secret),
            BackgroundTasks(),
            self._settings(),  # type: ignore[arg-type]
            dispatcher,  # type: ignore[arg-type]
        )

        self.assertEqual(response, {"status": "ignored"})
        self.assertEqual(dispatcher.payloads, [])

    async def test_mixed_phone_batch_dispatches_only_configured_phone_messages(
        self,
    ) -> None:
        body = _mixed_phone_body(
            self.configured_phone_id,
            "9999999999999999",
        )
        dispatcher = _RecordingDispatcher()

        response = await receive_webhook(
            _signed_request(body, self.secret),
            BackgroundTasks(),
            self._settings(),  # type: ignore[arg-type]
            dispatcher,  # type: ignore[arg-type]
        )

        self.assertEqual(response, {"status": "ok"})
        self.assertEqual(len(dispatcher.payloads), 1)
        dispatched = dispatcher.payloads[0]
        messages = [
            message
            for entry in dispatched.entry
            for change in entry.changes
            for message in change.value.messages
        ]
        self.assertEqual([message.id for message in messages], [
            "wamid.ingress-security-owned"
        ])
        metadata = dispatched.entry[0].changes[0].value.metadata
        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.phone_number_id, self.configured_phone_id)

    async def test_invalid_hmac_is_rejected_before_dispatch(self) -> None:
        body = _message_body(self.configured_phone_id)
        dispatcher = _RecordingDispatcher()

        with self.assertRaises(HTTPException) as raised:
            await receive_webhook(
                _signed_request(body, self.secret, valid=False),
                BackgroundTasks(),
                self._settings(),  # type: ignore[arg-type]
                dispatcher,  # type: ignore[arg-type]
            )

        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(dispatcher.payloads, [])

    def test_target_metadata_survives_durable_dispatch_split(self) -> None:
        webhook = WhatsAppWebhookPayload.model_validate_json(
            _message_body(self.configured_phone_id)
        )

        (unit,) = _dispatch_units(webhook)
        persisted = WhatsAppWebhookPayload.model_validate_json(unit.body)
        metadata = persisted.entry[0].changes[0].value.metadata

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.phone_number_id, self.configured_phone_id)


class _FailingHttpClient:
    async def post(self, *_: object, **__: object) -> object:
        request = httpx.Request(
            "POST",
            "https://graph.facebook.com/v26.0/123/messages"
            "?access_token=NETWORK_QUERY_SENTINEL",
        )
        raise httpx.ConnectError("connection failed", request=request)


class LogRedactionTests(unittest.IsolatedAsyncioTestCase):
    def test_rendered_log_redacts_queries_and_common_credential_forms(self) -> None:
        original = (
            "GET https://example.test/path?access_token=QUERY_SENTINEL "
            "Authorization: Bearer BEARER_SENTINEL "
            "WHATSAPP_ACCESS_TOKEN=WHATSAPP_SENTINEL "
            "OPENAI_API_KEY=API_KEY_SENTINEL "
            + "sk-"
            + "proj-OPENAI_SENTINEL_123456"
        )

        redacted = redact_log_text(original)

        for sentinel in (
            "QUERY_SENTINEL",
            "BEARER_SENTINEL",
            "WHATSAPP_SENTINEL",
            "API_KEY_SENTINEL",
            "OPENAI_SENTINEL",
        ):
            self.assertNotIn(sentinel, redacted)
        self.assertIn("https://example.test/path?[REDACTED]", redacted)

    def test_formatter_redacts_sensitive_exception_text_after_formatting(self) -> None:
        formatter = RedactingFormatter("%(levelname)s %(message)s")
        record = logging.LogRecord(
            "test",
            logging.ERROR,
            __file__,
            1,
            "request failed: %s",
            ("https://example.test/resource?sig=SIGNED_QUERY_SENTINEL",),
            None,
        )

        rendered = formatter.format(record)

        self.assertNotIn("SIGNED_QUERY_SENTINEL", rendered)
        self.assertIn("https://example.test/resource?[REDACTED]", rendered)

    async def test_network_exception_log_does_not_render_sensitive_request_url(
        self,
    ) -> None:
        client = WhatsAppClient(
            http_client=_FailingHttpClient(),  # type: ignore[arg-type]
            access_token="ACCESS_TOKEN_SENTINEL",
            phone_number_id="123",
            api_version="v26.0",
        )

        with self.assertLogs(
            "ssp_licensing_agent.app.core.whatsapp",
            level="ERROR",
        ) as captured:
            with self.assertRaises(WhatsAppAPIError) as raised:
                await client.send_message(
                    WhatsAppTextMessage(
                        to="919111111111",
                        text=TextContent(body="test"),
                    )
                )

        output = "\n".join(captured.output)
        self.assertIn("error_type=ConnectError", output)
        self.assertNotIn("NETWORK_QUERY_SENTINEL", output)
        self.assertNotIn("ACCESS_TOKEN_SENTINEL", output)
        self.assertTrue(raised.exception.__suppress_context__)


if __name__ == "__main__":
    unittest.main()
