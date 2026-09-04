"""Privacy-preserving structured logging."""

from __future__ import annotations

import logging
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

ALLOWED_LOG_FIELDS = frozenset(
    {
        "event",
        "component",
        "operation",
        "status",
        "version",
        "git_sha",
        "reason_code",
    }
)


def sanitize_log_event(event: Mapping[str, object]) -> dict[str, object]:
    """Keep only operational metadata that is safe for rotating logs."""
    return {key: value for key, value in event.items() if key in ALLOWED_LOG_FIELDS}


def _allowlist_processor(
    _: Any, __: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    return sanitize_log_event(event_dict)


def log_operational_event(
    *,
    event: str,
    component: str,
    operation: str,
    status: str,
    version: str,
    git_sha: str,
) -> None:
    """Emit a runtime event through the allowlisted structured logging path."""
    structlog.get_logger("stock_profiler").info(
        event,
        component=component,
        operation=operation,
        status=status,
        version=version,
        git_sha=git_sha,
    )


def configure_logging(*, development: bool) -> None:
    """Configure JSON production logs without accepting arbitrary bound values."""
    renderer: structlog.types.Processor
    renderer = (
        structlog.dev.ConsoleRenderer() if development else structlog.processors.JSONRenderer()
    )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            _allowlist_processor,
            renderer,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
