import unittest
from decimal import Decimal

from httpx import ASGITransport, AsyncClient, MockTransport, Response

from app.api.whatsapp.service import QuoteResults, WhatsAppWebhookService
from app.core.agent.main import PricingAgentAPIError, PricingAgentClient
from app.core.agent.schema import QuoteSelectionRequiredResponse
from app.core.whatsapp import WhatsAppClient
from app.schema.whatsapp import InteractiveRow, WhatsAppWebhookPayload


class FakePricingAgentClient:
    def __init__(self) -> None:
        self.selected_quote_calls: list[dict[str, object]] = []

    async def analyze_tenant_file(self, **_: object) -> dict[str, object]:
        return {
            "licenses": [
                {
                    "product_title": "Product A",
                    "assigned_licenses": 10,
                    "active_licenses": 8,
                },
                {
                    "product_title": "Product B",
                    "assigned_licenses": 2,
                    "active_licenses": 1,
                },
            ]
        }

    async def analyze_and_quote(
        self, *, product_query: str, **kwargs: object
    ) -> object:
        if product_query == "Product B" or kwargs.get("sku_id") is not None:
            if kwargs.get("sku_id") is not None:
                self.selected_quote_calls.append(kwargs)
            return type(
                "Analysis",
                (),
                {
                    "final_quote": type(
                        "Quote",
                        (),
                        {
                            "model_dump": lambda _, **__: _quote(
                                len(self.selected_quote_calls)
                            )
                        },
                    )()
                },
            )()

        selection = QuoteSelectionRequiredResponse.model_validate(
            {
                "detail": {
                    "code": "QUOTE_SELECTION_REQUIRED",
                    "message": "Choose an option",
                    "available_options": [
                        {
                            "product_id": "product-a",
                            "sku_id": 2,
                            "sku_title": "Product A monthly",
                            "term_duration": "P1M",
                            "billing_plan": "Monthly",
                        },
                        {
                            "product_id": "product-a",
                            "sku_id": "000F",
                            "sku_title": "Product A annual",
                            "term_duration": "P1Y",
                            "billing_plan": "Annual",
                        },
                    ],
                }
            }
        )
        raise PricingAgentAPIError("selection required", quote_selection=selection)

def _quote(number: int) -> dict[str, object]:
    return {
        "quote_number": number,
        "sku_title": f"Quote {number}",
        "product_id": "product",
        "sku_id": number,
        "term_duration": "P1M",
        "billing_plan": "Monthly",
        "target_quantity": 10,
        "existing_quantity": 8,
        "promo_percentage": "0",
        "total_quote_amount": "100.00",
    }


class DocumentQuoteFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_expands_all_selection_options_into_final_quotes(self) -> None:
        pricing_client = FakePricingAgentClient()
        service = WhatsAppWebhookService(object(), pricing_client)  # type: ignore[arg-type]

        result = await service._document_quote_text(b"file", "tenant.csv", "text/csv")

        self.assertEqual(len(pricing_client.selected_quote_calls), 2)
        self.assertEqual(pricing_client.selected_quote_calls[0]["target_quantity"], 10)
        self.assertEqual(pricing_client.selected_quote_calls[0]["sku_id"], 2)
        self.assertEqual(pricing_client.selected_quote_calls[0]["file"], b"file")
        self.assertIn("2 products | 3 pricing options", result)
        self.assertIn("*Product A*", result)
        self.assertIn("10 requested | 8 existing | 2 options", result)
        self.assertIn("1 month, monthly - INR 100.00", result)
        self.assertNotIn("Product: product | SKU:", result)

    def test_text_chunks_respect_whatsapp_limit(self) -> None:
        chunks = WhatsAppWebhookService._text_chunks("a" * 9000)

        self.assertEqual("".join(chunks), "a" * 9000)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))

    def test_formats_quote_amount_with_grouping_and_two_decimals(self) -> None:
        self.assertEqual(
            WhatsAppWebhookService._formatted_amount("2404155.6"), "2,404,155.60"
        )

    async def test_interactive_quote_navigation(self) -> None:
        whatsapp_client = CapturingWhatsAppClient()
        service = WhatsAppWebhookService(whatsapp_client, object())  # type: ignore[arg-type]
        quote = {
            **_quote(1),
            "_requested_product": "Microsoft 365 E3",
            "sku_title": "Microsoft 365 E3",
            "term_duration": "P1Y",
            "billing_plan": "Annual",
            "total_quote_amount": "850.0",
            "initial_quote_without_promo": "100.0",
            "initial_quote_with_promo": "85.0",
            "promo_quantity": 10,
            "promo_percentage": "15",
            "promo_code": "SAVE15",
        }
        session_id = service._create_quote_session(
            "123", QuoteResults("tenant.csv", 1, [quote], [])
        )

        self.assertTrue(await service._send_product_list("123", session_id, 0))
        product_row = whatsapp_client.messages[-1].interactive.action.sections[0].rows[0]
        await service._handle_interactive_reply("123", product_row.id)
        variant_row = whatsapp_client.messages[-1].interactive.action.sections[0].rows[0]
        self.assertEqual(variant_row.description, "Microsoft 365 E3")
        await service._handle_interactive_reply("123", variant_row.id)
        option_row = whatsapp_client.messages[-1].interactive.action.sections[0].rows[0]
        self.assertEqual(
            option_row.description, "INR 850.00 total | 15% promo - code SAVE15"
        )
        await service._handle_interactive_reply("123", option_row.id)

        detail = whatsapp_client.messages[-1]
        self.assertEqual(detail.interactive.type, "button")
        self.assertIn("Regular unit price: INR 100.00", detail.interactive.body.text)
        self.assertIn("Promo unit price: INR 85.00", detail.interactive.body.text)
        self.assertIn("Total savings: INR 150.00", detail.interactive.body.text)
        self.assertIn("Promo applied to: 10 licenses", detail.interactive.body.text)
        self.assertIn("Final total: *INR 850.00*", detail.interactive.body.text)
        self.assertIn("Promotion: 15% off", detail.interactive.body.text)
        self.assertIn("Promo code: *SAVE15*", detail.interactive.body.text)
        self.assertEqual(len(detail.interactive.action.buttons), 2)

    def test_shows_automatic_promotion_when_code_is_missing(self) -> None:
        promo = WhatsAppWebhookService._promo_text(
            {"promo_percentage": "7.5", "promo_quantity": 1}
        )

        self.assertEqual(promo, "Promotion: 7.5% off (applied automatically)")

    def test_normalizes_fractional_promo_and_marks_it_not_applied(self) -> None:
        quote = {
            "promo_percentage": "0.05",
            "promo_quantity": 0,
            "existing_quantity": 45,
            "initial_quote_without_promo": "1746.36",
            "initial_quote_with_promo": "1659.04",
        }

        self.assertEqual(WhatsAppWebhookService._promo_percentage(quote), Decimal("5.00"))
        self.assertEqual(
            WhatsAppWebhookService._promo_text(quote),
            "Promotion available: 5% off\nPromo status: Not applied - API returned "
            "0 promo-eligible licenses (45 existing licenses)",
        )

    def test_list_pagination_stays_within_whatsapp_row_limit(self) -> None:
        rows = WhatsAppWebhookService._paged_rows(
            items=list(range(20)),
            page=1,
            row_factory=lambda index, item: InteractiveRow(
                id=f"row-{index}", title=str(item)
            ),
            previous_id="previous",
            next_id="next",
        )

        self.assertEqual(len(rows), 10)
        self.assertEqual(rows[-2].id, "previous")
        self.assertEqual(rows[-1].id, "next")

    def test_long_edition_names_get_distinct_row_titles(self) -> None:
        titles = WhatsAppWebhookService._distinct_row_titles(
            [
                "10-Year Audit Log Retention Add On Commercial",
                "10-Year Audit Log Retention Add On Education",
            ]
        )

        self.assertEqual(len(titles), 2)
        self.assertNotEqual(titles[0], titles[1])
        self.assertTrue(all(len(title) <= 24 for title in titles))
        self.assertIn("Commercial", titles[0])
        self.assertIn("Education", titles[1])

    def test_edition_titles_use_standard_and_variant_suffix(self) -> None:
        titles = WhatsAppWebhookService._edition_row_titles(
            "10-Year Audit Log Retention Add On",
            [
                "10-Year Audit Log Retention Add On",
                "10-Year Audit Log Retention Add On for FLW",
            ],
        )

        self.assertEqual(titles, ["Standard", "For FLW"])

    def test_parses_incoming_interactive_list_reply(self) -> None:
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
                                            "id": "message-1",
                                            "from": "123",
                                            "type": "interactive",
                                            "interactive": {
                                                "type": "list_reply",
                                                "list_reply": {
                                                    "id": "quote|session|product|0",
                                                    "title": "Microsoft 365 E3",
                                                },
                                            },
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ],
            }
        )

        reply = webhook.entry[0].changes[0].value.messages[0].interactive
        self.assertIsNotNone(reply)
        self.assertEqual(reply.list_reply.id, "quote|session|product|0")  # type: ignore[union-attr]


class CapturingWhatsAppClient:
    def __init__(self) -> None:
        self.messages: list[object] = []

    async def send_message(self, message: object) -> dict[str, object]:
        self.messages.append(message)
        return {}


class PricingAgentClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_final_quote_serializes_numeric_sku_id_as_string(self) -> None:
        received_payload: dict[str, object] = {}

        async def app(scope: dict[str, object], receive: object, send: object) -> None:
            assert scope["type"] == "http"
            body = b""
            while True:
                message = await receive()  # type: ignore[operator]
                body += message.get("body", b"")
                if not message.get("more_body"):
                    break
            import json

            received_payload.update(json.loads(body))
            response = _complete_quote()
            await send(  # type: ignore[operator]
                {"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]}
            )
            await send(  # type: ignore[operator]
                {"type": "http.response.body", "body": json.dumps(response).encode()}
            )

        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http_client:
            client = PricingAgentClient(http_client, "http://test")
            await client.create_final_quote("Product A", 10, 8, sku_id=2)

        self.assertEqual(received_payload["sku_id"], "2")


class WhatsAppAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_failure_prevents_fallback_send(self) -> None:
        request_count = 0

        async def reject_request(_: object) -> Response:
            nonlocal request_count
            request_count += 1
            return Response(
                401,
                json={
                    "error": {
                        "message": "Authentication Error",
                        "type": "OAuthException",
                        "code": 190,
                    }
                },
            )

        async with AsyncClient(transport=MockTransport(reject_request)) as http_client:
            whatsapp_client = WhatsAppClient(http_client, "invalid", "phone-id")
            service = WhatsAppWebhookService(
                whatsapp_client, object()  # type: ignore[arg-type]
            )
            sent = await service._send_interactive_list(
                "123",
                "Choose a product",
                "View products",
                [InteractiveRow(id="product-1", title="Product")],
            )
            await service._send_text("123", "Fallback")

        self.assertFalse(sent)
        self.assertFalse(whatsapp_client.credentials_valid)
        self.assertEqual(request_count, 1)


def _complete_quote() -> dict[str, object]:
    return {
        **_quote(1),
        "quote_status": "created",
        "source_row_number": 1,
        "non_promo_quantity": 10,
        "promo_quantity": 0,
        "initial_quote_without_promo": "100.00",
        "initial_quote_with_promo": "100.00",
        "non_promo_amount": "100.00",
        "promo_amount": "0.00",
        "blended_unit_price": "10.00",
    }
