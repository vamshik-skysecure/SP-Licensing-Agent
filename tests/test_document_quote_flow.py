import unittest

from httpx import ASGITransport, AsyncClient

from app.api.whatsapp.service import WhatsAppWebhookService
from app.core.agent.main import PricingAgentAPIError, PricingAgentClient
from app.core.agent.schema import QuoteSelectionRequiredResponse


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
        self.assertIn("1 month, monthly - 100.00", result)
        self.assertNotIn("Product: product | SKU:", result)

    def test_text_chunks_respect_whatsapp_limit(self) -> None:
        chunks = WhatsAppWebhookService._text_chunks("a" * 9000)

        self.assertEqual("".join(chunks), "a" * 9000)
        self.assertTrue(all(len(chunk) <= 4096 for chunk in chunks))

    def test_formats_quote_amount_with_grouping_and_two_decimals(self) -> None:
        self.assertEqual(
            WhatsAppWebhookService._formatted_amount("2404155.6"), "2,404,155.60"
        )


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
