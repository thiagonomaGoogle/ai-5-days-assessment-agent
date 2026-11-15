"""Structured JSON logging integrated with PII redaction and OpenTelemetry trace IDs.

Uses python-json-logger / Google Cloud Logging JSON conventions (`severity`,
`logging.googleapis.com/trace`, `logging.googleapis.com/spanId`) and never uses
unstructured `print()` statements in agent code.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import sys
from typing import Any

from ..config.settings import get_settings
from .pii_redactor import PiiRedactionPipeline, get_pii_redactor
from .tracing import get_current_trace_context

try:
    from pythonjsonlogger import jsonlogger  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover
    jsonlogger = None  # type: ignore[assignment]


class PiiSanitizingFilter(logging.Filter):
    """Logging filter that scrubs PII and secrets from log messages and structured extras."""

    def __init__(self, redactor: PiiRedactionPipeline | None = None) -> None:
        super().__init__()
        self._redactor = redactor or get_pii_redactor()

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redactor.redact_text(record.msg)
        if record.args:
            record.args = self._redactor.redact_payload(record.args)
        if hasattr(record, "structured_data"):
            record.structured_data = self._redactor.redact_payload(
                getattr(record, "structured_data")
            )
        return True


class CloudStructuredJsonFormatter(logging.Formatter):
    """Formats every log entry as a single-line Google Cloud Structured JSON object."""

    def __init__(self, service_name: str, project_id: str = "") -> None:
        super().__init__()
        self.service_name = service_name
        self.project_id = project_id
        self._redactor = get_pii_redactor()

    def format(self, record: logging.LogRecord) -> str:
        trace_id, span_id, parent_span_id = get_current_trace_context()
        message = self._redactor.redact_text(record.getMessage())

        payload: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,
            "service": self.service_name,
            "logger": record.name,
            "message": message,
            "event_type": getattr(record, "event_type", "AGENT_LOG"),
            "agent_name": getattr(record, "agent_name", ""),
            "session_id": getattr(record, "session_id", ""),
        }

        if trace_id:
            payload["trace_id"] = trace_id
            if self.project_id:
                payload["logging.googleapis.com/trace"] = (
                    f"projects/{self.project_id}/traces/{trace_id}"
                )
        if span_id:
            payload["span_id"] = span_id
            payload["logging.googleapis.com/spanId"] = span_id
        if parent_span_id:
            payload["parent_span_id"] = parent_span_id

        extra_data = getattr(record, "structured_data", None)
        if isinstance(extra_data, dict) and extra_data:
            payload["metadata"] = self._redactor.redact_payload(extra_data)

        if record.exc_info:
            payload["exception"] = self._redactor.redact_text(
                self.formatException(record.exc_info)
            )

        return json.dumps(payload, ensure_ascii=False, default=str)


def get_structured_logger(name: str = "c4_architect_agent") -> logging.Logger:
    """Returns a configured structured JSON logger with active PII scrubbing."""
    settings = get_settings()
    logger = logging.getLogger(name)
    if not getattr(logger, "_c4_structured_configured", False):
        logger.setLevel(getattr(logging, settings.log_level, logging.INFO))
        handler = logging.StreamHandler(stream=sys.stderr)
        if jsonlogger is not None and False:  # CloudStructuredJsonFormatter enforces GCP trace keys
            handler.setFormatter(jsonlogger.JsonFormatter())
        else:
            handler.setFormatter(
                CloudStructuredJsonFormatter(
                    service_name=settings.service_name,
                    project_id=settings.gcp_project_id,
                )
            )
        handler.addFilter(PiiSanitizingFilter())
        logger.handlers = [handler]
        logger.propagate = False
        setattr(logger, "_c4_structured_configured", True)
    return logger


def log_structured_event(
    event_type: str,
    message: str,
    *,
    level: int = logging.INFO,
    agent_name: str = "",
    session_id: str = "",
    metadata: dict[str, Any] | None = None,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Emits a sanitized structured JSON log record and returns the sanitized metadata."""
    active_logger = logger or get_structured_logger()
    redactor = get_pii_redactor()
    sanitized_metadata = redactor.redact_payload(metadata or {})
    sanitized_message = redactor.redact_text(message)
    active_logger.log(
        level,
        sanitized_message,
        extra={
            "event_type": event_type,
            "agent_name": agent_name,
            "session_id": session_id,
            "structured_data": sanitized_metadata,
        },
    )
    return sanitized_metadata
