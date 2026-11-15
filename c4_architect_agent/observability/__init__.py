"""Observability, OpenTelemetry distributed tracing, structured JSON logging, and PII redaction."""

from .audit_plugin import AuditLedgerEntry, IntentOutcomeAuditPlugin, get_audit_plugin
from .pii_redactor import PiiRedactionPipeline, get_pii_redactor
from .structured_logger import get_structured_logger, log_structured_event
from .tracing import (
    SpanRecord,
    configure_tracing,
    get_recorded_spans,
    get_tracer,
    start_traced_span,
)

__all__ = [
    "AuditLedgerEntry",
    "IntentOutcomeAuditPlugin",
    "PiiRedactionPipeline",
    "SpanRecord",
    "configure_tracing",
    "get_audit_plugin",
    "get_pii_redactor",
    "get_recorded_spans",
    "get_structured_logger",
    "get_tracer",
    "log_structured_event",
    "start_traced_span",
]
