import json
import unittest

from httpx import AsyncClient, MockTransport, Request, Response

from app.core.whatsapp import WhatsAppClient


class WhatsAppImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_png_is_uploaded_then_sent_as_an_image_message(self) -> None:
        requests: list[Request] = []

        def handler(request: Request) -> Response:
            requests.append(request)
            if request.url.path.endswith("/media"):
                return Response(200, json={"id": "media-table-123"})
            if request.url.path.endswith("/messages"):
                return Response(200, json={"messages": [{"id": "wamid.123"}]})
            return Response(404)

        async with AsyncClient(transport=MockTransport(handler)) as http_client:
            client = WhatsAppClient(
                http_client=http_client,
                access_token="test-token",
                phone_number_id="1164810520058946",
                base_url="https://graph.facebook.test",
                api_version="v25.0",
            )
            response = await client.send_image(
                to="918197235267",
                content=b"\x89PNG\r\n\x1a\nrendered-table",
                filename="annual-comparison-table-1.png",
                caption="Annual commercial comparison",
            )

        self.assertEqual(response, {"messages": [{"id": "wamid.123"}]})
        self.assertEqual(len(requests), 2)
        self.assertTrue(requests[0].url.path.endswith("/media"))
        self.assertIn("multipart/form-data", requests[0].headers["content-type"])
        self.assertIn(b"annual-comparison-table-1.png", requests[0].content)
        self.assertIn(b"image/png", requests[0].content)

        self.assertTrue(requests[1].url.path.endswith("/messages"))
        payload = json.loads(requests[1].content)
        self.assertEqual(
            payload,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": "918197235267",
                "type": "image",
                "image": {
                    "id": "media-table-123",
                    "caption": "Annual commercial comparison",
                },
            },
        )


if __name__ == "__main__":
    unittest.main()
