import logging
import sys

import structlog

from app.core.config import settings


def _resolve_log_level() -> int:
    level_name = (settings.app_log_level or "WARNING").upper()
    return getattr(logging, level_name, logging.WARNING)


def setup_logging() -> None:
    log_level = _resolve_log_level()

    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    if settings.is_production:
        processors = shared_processors + [structlog.processors.JSONRenderer()]
    else:
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(colors=True)
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )

    # Silence noisy loggers
    noisy_server_level = logging.INFO if log_level <= logging.INFO else logging.WARNING
    logging.getLogger("uvicorn.access").disabled = log_level > logging.INFO
    logging.getLogger("uvicorn.access").setLevel(noisy_server_level)
    logging.getLogger("uvicorn.error").setLevel(noisy_server_level)
    logging.getLogger("uvicorn.protocols.websockets").setLevel(noisy_server_level)
    logging.getLogger("websockets").setLevel(noisy_server_level)
    logging.getLogger("websockets.server").setLevel(noisy_server_level)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.pool").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("hpack").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("s3transfer").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


logger = structlog.get_logger(__name__)
