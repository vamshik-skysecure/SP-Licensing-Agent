from __future__ import annotations

import io
import logging
import unittest

from app.config.logging import configure_logging, redact_log_text


class LoggingSecurityTests(unittest.TestCase):
    def test_relative_webhook_query_is_redacted(self) -> None:
        rendered = redact_log_text(
            'GET /api/whatsapp/webhook?hub.mode=subscribe&hub.verify_token=top-secret '
            'HTTP/1.1'
        )

        self.assertNotIn("top-secret", rendered)
        self.assertNotIn("hub.verify_token", rendered)
        self.assertIn("/api/whatsapp/webhook?[REDACTED]", rendered)

    def test_configure_logging_protects_uvicorn_access_handler(self) -> None:
        server_logger = logging.getLogger("uvicorn.access")
        original_handlers = list(server_logger.handlers)
        original_level = server_logger.level
        original_propagate = server_logger.propagate
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        try:
            server_logger.handlers = [handler]
            server_logger.setLevel(logging.INFO)
            server_logger.propagate = False

            configure_logging("INFO")
            server_logger.info(
                '%s - "%s %s HTTP/%s" %d',
                "127.0.0.1:50000",
                "GET",
                (
                    "/api/whatsapp/webhook?hub.mode=subscribe&"
                    "hub.verify_token=top-secret&hub.challenge=123"
                ),
                "1.1",
                200,
            )

            rendered = stream.getvalue()
            self.assertNotIn("top-secret", rendered)
            self.assertNotIn("hub.verify_token", rendered)
            self.assertIn("/api/whatsapp/webhook?[REDACTED]", rendered)
        finally:
            server_logger.handlers = original_handlers
            server_logger.setLevel(original_level)
            server_logger.propagate = original_propagate


if __name__ == "__main__":
    unittest.main()
