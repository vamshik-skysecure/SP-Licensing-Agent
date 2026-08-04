import logging

_LOGGER_NAME = "ssp_licensing_agent"
_configured = False


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Configure and return the shared application logger once."""
    global _configured

    logger = logging.getLogger(_LOGGER_NAME)
    if _configured:
        return logger

    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
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
