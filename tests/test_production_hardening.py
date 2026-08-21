import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from azure.core.exceptions import ResourceExistsError
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from app.api.dependencies import lifespan
from app.api.main import privacy_policy, service_home
from app.api.whatsapp.handler import _read_bounded_body
from app.config import opaque_identifier
from app.core.dispatch import AzureBlobWebhookDispatcher, _dispatch_units
from app.schema.whatsapp import WhatsAppWebhookPayload


def _batched_webhook() -> WhatsAppWebhookPayload:
    return WhatsAppWebhookPayload.model_validate(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        {
                            "value": {
                                "messages": [
                                    {
                                        "id": "message-a1",
                                        "from": "919111111111",
                                        "timestamp": "1700000001",
                                        "type": "text",
                                        "text": {"body": "first"},
                                    },
                                    {
                                        "id": "message-a2",
                                        "from": "919111111111",
                                        "timestamp": "1700000002",
                                        "type": "text",
                                        "text": {"body": "second"},
                                    },
                                    {
                                        "id": "message-b1",
                                        "from": "919222222222",
                                        "timestamp": "1700000003",
                                        "type": "text",
                                        "text": {"body": "other seller"},
                                    },
                                ]
                            }
                        }
                    ]
                }
            ],
        }
    )


class _FakeBlob:
    def __init__(self, name: str, container: "_FakeContainer") -> None:
        self.name = name
        self._container = container

    async def upload_blob(self, content: bytes, **options: Any) -> None:
        if self.name in self._container.documents:
            raise ResourceExistsError("already persisted")
        self._container.documents[self.name] = bytes(content)
        self._container.metadata[self.name] = dict(options.get("metadata") or {})

    async def download_blob(self, **_: Any) -> "_FakeStream":
        return _FakeStream(self._container.documents[self.name])

    async def delete_blob(self, **_: Any) -> None:
        del self._container.documents[self.name]
        self._container.metadata.pop(self.name, None)

    async def get_blob_properties(self, **_: Any) -> "_FakeProperties":
        return _FakeProperties(self._container.metadata.get(self.name, {}))

    async def set_blob_metadata(self, metadata: dict[str, str], **_: Any) -> None:
        self._container.metadata[self.name] = dict(metadata)


class _FakeStream:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def readall(self) -> bytes:
        return self._content


class _FakeProperties:
    def __init__(self, metadata: dict[str, str]) -> None:
        self.metadata = metadata


class _FakeLease:
    async def acquire(self, **_: Any) -> None:
        return None

    async def renew(self) -> None:
        return None

    async def release(self) -> None:
        return None


class _FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[str, bytes] = {}
        self.metadata: dict[str, dict[str, str]] = {}

    def get_blob_client(self, name: str) -> _FakeBlob:
        return _FakeBlob(name, self)


class _RecordingHandler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.handled = 0

    async def handle(self, webhook: WhatsAppWebhookPayload) -> None:
        del webhook
        self.handled += 1
        if self.fail:
            raise RuntimeError("synthetic processing failure")


class DurableDispatchTests(unittest.TestCase):
    def test_batch_is_split_and_ordered_by_opaque_seller_session(self) -> None:
        units = _dispatch_units(_batched_webhook())

        self.assertEqual(len(units), 3)
        self.assertEqual(units[0].session_id, units[1].session_id)
        self.assertNotEqual(units[0].session_id, units[2].session_id)
        for unit in units:
            payload = WhatsAppWebhookPayload.model_validate_json(unit.body)
            messages = [
                message
                for entry in payload.entry
                for change in entry.changes
                for message in change.value.messages
            ]
            self.assertEqual(len(messages), 1)

    def test_queue_identifiers_do_not_expose_phone_or_meta_message_id(self) -> None:
        units = _dispatch_units(_batched_webhook())

        for unit in units:
            self.assertNotIn("919", unit.session_id)
            self.assertNotIn("message-", unit.message_id)
            self.assertEqual(len(unit.session_id), 64)
            self.assertEqual(len(unit.message_id), 64)


class BlobInboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_persists_each_message_once_before_acknowledgement(
        self,
    ) -> None:
        container = _FakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        webhook = _batched_webhook()

        await dispatcher.dispatch(b"signed-body", webhook, BackgroundTasks())
        await dispatcher.dispatch(b"signed-body", webhook, BackgroundTasks())

        self.assertEqual(len(container.documents), 3)
        names = sorted(container.documents)
        self.assertIn("/pending/1700000001-", names[0])
        self.assertIn("/pending/1700000002-", names[1])
        self.assertIn("/pending/1700000003-", names[2])
        for content in container.documents.values():
            persisted = WhatsAppWebhookPayload.model_validate_json(content)
            messages = [
                message
                for entry in persisted.entry
                for change in entry.changes
                for message in change.value.messages
            ]
            self.assertEqual(len(messages), 1)

    async def test_successful_processing_deletes_pending_blob(self) -> None:
        container = _FakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
            lease_factory=lambda _: _FakeLease(),
        )
        await dispatcher.dispatch(
            b"signed-body",
            WhatsAppWebhookPayload.model_validate(
                {
                    "object": "whatsapp_business_account",
                    "entry": [
                        {
                            "changes": [
                                {
                                    "value": {
                                        "messages": [
                                            {
                                                "id": "success-1",
                                                "from": "919111111111",
                                                "timestamp": "1700000010",
                                                "type": "text",
                                                "text": {"body": "help"},
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    ],
                }
            ),
            BackgroundTasks(),
        )
        pending_name = next(iter(container.documents))
        handler = _RecordingHandler()
        dispatcher._handler = handler

        await dispatcher._process(pending_name)

        self.assertEqual(handler.handled, 1)
        self.assertEqual(container.documents, {})

    async def test_repeated_failure_moves_message_to_dead_letter(self) -> None:
        container = _FakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
            lease_factory=lambda _: _FakeLease(),
            max_delivery_count=1,
        )
        webhook = WhatsAppWebhookPayload.model_validate(
            {
                "object": "whatsapp_business_account",
                "entry": [
                    {
                        "changes": [
                            {
                                "value": {
                                    "messages": [
                                        {
                                            "id": "failure-1",
                                            "from": "919111111111",
                                            "timestamp": "1700000020",
                                            "type": "text",
                                            "text": {"body": "help"},
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ],
            }
        )
        await dispatcher.dispatch(b"signed-body", webhook, BackgroundTasks())
        pending_name = next(iter(container.documents))
        dispatcher._handler = _RecordingHandler(fail=True)

        await dispatcher._process(pending_name)

        self.assertNotIn(pending_name, container.documents)
        dead_letter_names = [
            name for name in container.documents if "/dead-letter/" in name
        ]
        self.assertEqual(len(dead_letter_names), 1)
        self.assertEqual(
            container.metadata[dead_letter_names[0]]["delivery_count"],
            "1",
        )


class PrivacyAndLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_privacy_notice_discloses_multimodal_openai_processing(self) -> None:
        notice = await privacy_policy()

        self.assertIn("Voice notes are processed for transcription", notice)
        self.assertIn("may be processed by the OpenAI API", notice)
        self.assertIn("pricing workbook is never sent to OpenAI", notice)
        self.assertNotIn(
            "Uploaded file bytes and the pricing workbook are not sent",
            notice,
        )
        self.assertIn("durable workflow queue", notice)
        self.assertIn("Azure Storage lifecycle policy", notice)

    async def test_root_page_reports_service_without_exposing_configuration(self) -> None:
        page = await service_home()

        self.assertIn("SkySecure Microsoft Licensing Advisor", page)
        self.assertIn("/health/live", page)
        self.assertIn("/privacy-policy", page)
        self.assertNotIn("OPENAI_API_KEY", page)

    async def test_log_identifier_is_stable_and_non_reversible(self) -> None:
        source = "wamid.sensitive-message-identifier"

        first = opaque_identifier(source)
        second = opaque_identifier(source)

        self.assertEqual(first, second)
        self.assertEqual(len(first), 12)
        self.assertNotIn("sensitive", first)

    async def test_webhook_body_is_rejected_while_streaming_past_limit(self) -> None:
        chunks = iter(
            [
                {"type": "http.request", "body": b"123", "more_body": True},
                {"type": "http.request", "body": b"456", "more_body": False},
            ]
        )

        async def receive() -> dict[str, object]:
            return next(chunks)

        request = Request({"type": "http", "headers": []}, receive)
        with self.assertRaises(HTTPException) as raised:
            await _read_bounded_body(request, 5)

        self.assertEqual(raised.exception.status_code, 413)

    async def test_webhook_body_preserves_exact_signed_bytes_within_limit(self) -> None:
        chunks = iter(
            [
                {"type": "http.request", "body": b"signed-", "more_body": True},
                {"type": "http.request", "body": b"payload", "more_body": False},
            ]
        )

        async def receive() -> dict[str, object]:
            return next(chunks)

        request = Request({"type": "http", "headers": []}, receive)

        self.assertEqual(await _read_bounded_body(request, 20), b"signed-payload")


class StartupCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_clients_are_closed_when_blob_validation_fails(self) -> None:
        class RecordingHttpClient:
            instance: "RecordingHttpClient | None" = None

            def __init__(self, *_: object, **__: object) -> None:
                self.closed = False
                RecordingHttpClient.instance = self

            async def aclose(self) -> None:
                self.closed = True

        class FailingBlobStore:
            instance: "FailingBlobStore | None" = None

            def __init__(self, **_: object) -> None:
                self.closed = False
                FailingBlobStore.instance = self

            async def connect(self) -> None:
                raise RuntimeError("synthetic RBAC failure")

            async def close(self) -> None:
                self.closed = True

        settings = SimpleNamespace(
            log_level="INFO",
            effective_runtime_profile="production",
            runtime_profile="production",
            environment="production",
            rate_card_backend="local",
            workflow_store_backend="azure_blob",
            message_dispatch_backend="azure_blob",
            whatsapp_connect_retries=0,
            whatsapp_access_token="test-token",
            whatsapp_phone_number_id="123",
            whatsapp_uri="https://graph.facebook.test",
            whatsapp_uri_version="v26.0",
            whatsapp_validate_credentials_on_startup=False,
            rate_card_local_path=Path("docs/microsoft_sku_v6_distributor.xlsx"),
            rate_card_sheet_name="Outcome Sheet",
            workflow_blob_container_name="licensing-workflows",
            workflow_blob_prefix="sessions",
            rate_card_storage_account_url="https://storage.example.invalid",
            rate_card_storage_connection_string=None,
            session_ttl_minutes=5,
        )

        with (
            patch("app.api.dependencies.Settings", return_value=settings),
            patch("app.api.dependencies.httpx.AsyncClient", RecordingHttpClient),
            patch("app.api.dependencies.AzureBlobWorkflowStore", FailingBlobStore),
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic RBAC failure"):
                async with lifespan(FastAPI()):
                    self.fail("Startup must not yield after Blob validation fails")

        assert RecordingHttpClient.instance is not None
        assert FailingBlobStore.instance is not None
        self.assertTrue(RecordingHttpClient.instance.closed)
        self.assertTrue(FailingBlobStore.instance.closed)


class ProductionAssetTests(unittest.TestCase):
    def test_golden_run_uses_current_v6_simple_pricing_contract(self) -> None:
        source = Path("scripts/run_uat_golden.py").read_text(encoding="utf-8")

        self.assertIn("microsoft_sku_v6_distributor.xlsx", source)
        self.assertIn('SHEET_NAME = "Outcome Sheet"', source)
        self.assertIn('price_basis="distributor_expected"', source)
        self.assertIn('"workflow_mode": "simple_pricing"', source)
        self.assertIn("request_requirement_validation(sender)", source)
        self.assertIn("confirm_requirement(sender)", source)
        self.assertIn("save_confirmed_as_is(sender, as_is)", source)
        self.assertNotIn("microsoft_sku_v5.xlsx", source)
        self.assertNotIn("Final Output Sheet", source)
        self.assertNotIn("promo_eligible=True", source)
        self.assertNotIn("set_discount", source)
        self.assertNotIn("set_adjustment", source)


if __name__ == "__main__":
    unittest.main()
