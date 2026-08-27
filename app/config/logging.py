import logging
import re
from hashlib import sha256

_LOGGER_NAME = "ssp_licensing_agent"
_configured = False

_URL_QUERY_RE = re.compile(
    r"(?i)((?:https?://[^\s?#]+|/[^\s?#]*))\?[^\s#\"']*"
)
_NAMED_SECRET_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(access_token|api[_-]?key|accountkey|client_secret|app_secret)"
    r"(\s*[:=]\s*)([^\s,;&]+)"
)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_OPENAI_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


def redact_log_text(value: str) -> str:
    """Remove credential material and URL query strings from rendered log text."""

    redacted = _URL_QUERY_RE.sub(r"\1?[REDACTED]", value)
    redacted = _NAMED_SECRET_RE.sub(r"\1\2[REDACTED]", redacted)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return _OPENAI_KEY_RE.sub("sk-[REDACTED]", redacted)


class RedactingFormatter(logging.Formatter):
    """Last-line defence against secrets embedded in exceptions or library output."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_log_text(super().format(record))


def _protect_server_loggers() -> None:
    """Apply final-output redaction to web-server handlers, including access logs."""

    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        server_logger = logging.getLogger(logger_name)
        for handler in server_logger.handlers:
            handler.setFormatter(
                RedactingFormatter(
                    "%(asctime)s %(levelname)s %(name)s %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S%z",
                )
            )


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return the shared application logger once."""
    global _configured

    logger = logging.getLogger(_LOGGER_NAME)
    # Uvicorn configures its handlers before the application lifespan begins. Reapply
    # protection on every call so an already-configured application logger cannot leave a
    # newly-created access handler exposing ``hub.verify_token`` in the request query.
    _protect_server_loggers()
    if _configured:
        return logger

    handler = logging.StreamHandler()
    handler.setFormatter(
        RedactingFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    logger.addHandler(handler)
    logger.setLevel(level.upper())
    logger.propagate = False
    _configured = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{_LOGGER_NAME}.{name}")


def opaque_identifier(value: str, *, length: int = 12) -> str:
    """Return a stable log correlation value without exposing the source identifier."""
    return sha256(value.encode("utf-8")).hexdigest()[:length]
