import asyncio
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.dispatch import (
    AzureBlobWebhookDispatcher,
    DispatchUnit,
    _dispatch_units,
)
from app.schema.whatsapp import WhatsAppWebhookPayload
from tests.test_production_hardening import _FakeContainer, _batched_webhook


class _ListableFakeContainer(_FakeContainer):
    """Add deterministic Blob listing to the shared dispatch-test fake."""

    def __init__(self) -> None:
        super().__init__()
        self.creation_times: dict[str, datetime] = {}

    def list_blobs(self, *, name_starts_with: str):
        async def items():
            for name in self.documents:
                if name.startswith(name_starts_with):
                    yield SimpleNamespace(
                        name=name,
                        creation_time=self.creation_times.get(name),
                    )

        return items()


def _same_timestamp_webhook() -> WhatsAppWebhookPayload:
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
                                        "id": "same-time-first",
                                        "from": "919111111111",
                                        "timestamp": "1700000042",
                                        "type": "text",
                                        "text": {"body": "first"},
                                    },
                                    {
                                        "id": "same-time-second",
                                        "from": "919111111111",
                                        "timestamp": "1700000042",
                                        "type": "text",
                                        "text": {"body": "second"},
                                    },
                                ]
                            }
                        }
                    ]
                }
            ],
        }
    )


class DispatchOrderingRegressionTests(unittest.IsolatedAsyncioTestCase):
    def test_dispatch_unit_sequence_default_is_backward_compatible(self) -> None:
        unit = DispatchUnit(b"{}", "message", "seller-session", "1700000000")

        self.assertEqual(unit.sequence, 0)

        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=_ListableFakeContainer(),
        )
        self.assertIn("-0000-message.json", dispatcher._pending_name(unit))

    def test_same_webhook_timestamp_produces_stable_unique_names(self) -> None:
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=_ListableFakeContainer(),
        )

        first_pass = _dispatch_units(_same_timestamp_webhook())
        second_pass = _dispatch_units(_same_timestamp_webhook())
        first_names = [dispatcher._pending_name(unit) for unit in first_pass]
        second_names = [dispatcher._pending_name(unit) for unit in second_pass]

        self.assertEqual([unit.sequence for unit in first_pass], [0, 1])
        self.assertEqual(first_names, second_names)
        self.assertEqual(len(set(first_names)), 2)
        self.assertIn("-0000-", first_names[0])
        self.assertIn("-0001-", first_names[1])

    def test_terminal_identity_ignores_batch_sequence_and_timestamp(self) -> None:
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=_ListableFakeContainer(),
        )
        first = DispatchUnit(
            b"{}",
            "a" * 64,
            "b" * 64,
            "1700000042",
            sequence=0,
        )
        reordered_redelivery = DispatchUnit(
            b"{}",
            first.message_id,
            first.session_id,
            "1700000099",
            sequence=17,
        )
        first_pending = dispatcher._pending_name(first)
        redelivered_pending = dispatcher._pending_name(reordered_redelivery)

        self.assertNotEqual(first_pending, redelivered_pending)
        self.assertEqual(
            dispatcher._terminal_name(first_pending),
            dispatcher._terminal_name(redelivered_pending),
        )
        self.assertEqual(
            dispatcher._terminal_name(first_pending),
            f"webhook-queue/terminal/{first.session_id}/{first.message_id}.json",
        )

    async def test_pending_names_returns_only_earliest_per_seller(self) -> None:
        container = _ListableFakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        webhook = _batched_webhook()
        units = _dispatch_units(webhook)

        await dispatcher.dispatch(b"signed-body", webhook, object())  # type: ignore[arg-type]
        names = [dispatcher._pending_name(unit) for unit in units]
        for unit, name in zip(units, names, strict=True):
            container.creation_times[name] = datetime.fromtimestamp(
                int(unit.enqueued_at),
                tz=UTC,
            )

        pending = await dispatcher._pending_names()

        self.assertEqual(pending, [names[0], names[2]])
        self.assertNotIn(names[1], pending)
        self.assertEqual(len({units[0].session_id, units[2].session_id}), 2)

    async def test_concurrent_pollers_select_same_earliest_seller_message(self) -> None:
        container = _ListableFakeContainer()
        first_dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        second_dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        webhook = _same_timestamp_webhook()
        units = _dispatch_units(webhook)

        await first_dispatcher.dispatch(
            b"signed-body",
            webhook,
            object(),  # type: ignore[arg-type]
        )
        names = [first_dispatcher._pending_name(unit) for unit in units]
        same_creation_time = datetime.fromtimestamp(1700000042, tz=UTC)
        for name in names:
            container.creation_times[name] = same_creation_time

        first_selection, second_selection = await asyncio.gather(
            first_dispatcher._pending_names(),
            second_dispatcher._pending_names(),
        )

        self.assertEqual(first_selection, [names[0]])
        self.assertEqual(second_selection, [names[0]])
        self.assertNotIn(names[1], first_selection)
        self.assertNotIn(names[1], second_selection)

    async def test_large_single_seller_backlog_does_not_starve_other_sellers(self) -> None:
        container = _ListableFakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        congested_session = "0" * 64
        other_session = "f" * 64
        for sequence in range(1001):
            name = (
                "webhook-queue/pending/"
                f"{congested_session}/1700000042-{sequence:04d}-{sequence:064x}.json"
            )
            container.documents[name] = b"{}"
        other_name = (
            "webhook-queue/pending/"
            f"{other_session}/1700000043-0000-{'a' * 64}.json"
        )
        container.documents[other_name] = b"{}"

        pending = await dispatcher._pending_names()

        self.assertEqual(len(pending), 2)
        self.assertTrue(pending[0].startswith(f"webhook-queue/pending/{congested_session}/"))
        self.assertEqual(pending[1], other_name)

    async def test_streaming_selection_keeps_earliest_item_per_seller(self) -> None:
        container = _ListableFakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        session = "c" * 64
        later_name = (
            f"webhook-queue/pending/{session}/"
            f"1700000099-0000-{'1' * 64}.json"
        )
        earlier_name = (
            f"webhook-queue/pending/{session}/"
            f"1700000001-0000-{'2' * 64}.json"
        )
        # Insert the later item first to prove selection is based on ordering data rather
        # than relying on the iterator's order.
        container.documents[later_name] = b"{}"
        container.documents[earlier_name] = b"{}"
        container.creation_times[later_name] = datetime.fromtimestamp(99, tz=UTC)
        container.creation_times[earlier_name] = datetime.fromtimestamp(1, tz=UTC)

        pending = await dispatcher._pending_names()

        self.assertEqual(pending, [earlier_name])

    async def test_event_timestamp_wins_when_blob_upload_order_is_reversed(self) -> None:
        container = _ListableFakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        session = "d" * 64
        earlier_event_uploaded_later = (
            f"webhook-queue/pending/{session}/"
            f"1700000001-0000-{'3' * 64}.json"
        )
        later_event_uploaded_first = (
            f"webhook-queue/pending/{session}/"
            f"1700000099-0000-{'4' * 64}.json"
        )
        container.documents[later_event_uploaded_first] = b"{}"
        container.documents[earlier_event_uploaded_later] = b"{}"
        container.creation_times[later_event_uploaded_first] = datetime.fromtimestamp(
            1,
            tz=UTC,
        )
        container.creation_times[earlier_event_uploaded_later] = datetime.fromtimestamp(
            99,
            tz=UTC,
        )

        pending = await dispatcher._pending_names()

        self.assertEqual(pending, [earlier_event_uploaded_later])

    async def test_legacy_flat_queue_is_one_bounded_serial_partition(self) -> None:
        container = _ListableFakeContainer()
        dispatcher = AzureBlobWebhookDispatcher(
            container_name="licensing-workflows",
            container_client=container,
        )
        later_name = f"webhook-queue/pending/1700000099-{'1' * 64}.json"
        earlier_name = f"webhook-queue/pending/1700000001-{'2' * 64}.json"
        container.documents[later_name] = b"{}"
        container.documents[earlier_name] = b"{}"
        container.creation_times[later_name] = datetime.fromtimestamp(99, tz=UTC)
        container.creation_times[earlier_name] = datetime.fromtimestamp(1, tz=UTC)

        pending = await dispatcher._pending_names()

        self.assertEqual(pending, [earlier_name])


if __name__ == "__main__":
    unittest.main()
