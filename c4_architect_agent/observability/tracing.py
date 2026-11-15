"""Distributed tracing with OpenTelemetry and Google Cloud Trace export.

Links spans across the entire request lifecycle (user query -> coordinator ->
parallel discovery -> C4 synthesizer -> tool calls -> final answer) and exposes
active trace_id / span_id for structured JSON log correlation.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import secrets
import time
from typing import Any, Iterator

from ..config.settings import get_settings

try:
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
except ImportError:  # pragma: no cover - lightweight fallback when SDK wheel absent
    otel_trace = None  # type: ignore[assignment]
    Resource = None  # type: ignore[assignment]
    TracerProvider = None  # type: ignore[assignment]
    BatchSpanProcessor = None  # type: ignore[assignment]
    SimpleSpanProcessor = None  # type: ignore[assignment]
    InMemorySpanExporter = None  # type: ignore[assignment]

try:
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
except ImportError:  # pragma: no cover
    CloudTraceSpanExporter = None  # type: ignore[assignment]


@dataclass
class SpanRecord:
    """Portable representation of a distributed trace span for inspection and tests."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    start_time_ns: int
    end_time_ns: int = 0
    status: str = "OK"
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if self.end_time_ns <= self.start_time_ns:
            return 0.0
        return round((self.end_time_ns - self.start_time_ns) / 1_000_000.0, 3)


_ACTIVE_SPAN_STACK: ContextVar[tuple[SpanRecord, ...]] = ContextVar(
    "c4_active_span_stack", default=()
)
_RECORDED_SPANS: list[SpanRecord] = []
_TRACING_CONFIGURED = False
_IN_MEMORY_EXPORTER: Any | None = None


def configure_tracing(
    *,
    service_name: str | None = None,
    enable_cloud_trace: bool | None = None,
    project_id: str | None = None,
) -> None:
    """Initializes OpenTelemetry TracerProvider and Cloud Trace / in-memory exporters."""
    global _TRACING_CONFIGURED, _IN_MEMORY_EXPORTER
    if _TRACING_CONFIGURED:
        return

    settings = get_settings()
    resolved_service = service_name or settings.service_name
    use_cloud_trace = (
        enable_cloud_trace if enable_cloud_trace is not None else settings.enable_cloud_trace
    )
    resolved_project = project_id if project_id is not None else settings.gcp_project_id

    if otel_trace is not None and TracerProvider is not None and Resource is not None:
        resource = Resource.create(
            {
                "service.name": resolved_service,
                "deployment.environment": settings.environment,
            }
        )
        provider = TracerProvider(resource=resource)
        if InMemorySpanExporter is not None and SimpleSpanProcessor is not None:
            _IN_MEMORY_EXPORTER = InMemorySpanExporter()
            provider.add_span_processor(SimpleSpanProcessor(_IN_MEMORY_EXPORTER))

        if (
            use_cloud_trace
            and CloudTraceSpanExporter is not None
            and BatchSpanProcessor is not None
        ):
            exporter = (
                CloudTraceSpanExporter(project_id=resolved_project)
                if resolved_project
                else CloudTraceSpanExporter()
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))

        otel_trace.set_tracer_provider(provider)

    _TRACING_CONFIGURED = True


def get_tracer(instrumenting_module_name: str = "c4_architect_agent") -> Any:
    """Returns an OpenTelemetry tracer (initializing the provider on first use)."""
    configure_tracing()
    if otel_trace is not None:
        return otel_trace.get_tracer(instrumenting_module_name)
    return None


def get_current_trace_context() -> tuple[str, str, str | None]:
    """Returns (trace_id, span_id, parent_span_id) for structured log correlation."""
    stack = _ACTIVE_SPAN_STACK.get()
    if stack:
        top = stack[-1]
        return top.trace_id, top.span_id, top.parent_span_id
    if otel_trace is not None:
        span = otel_trace.get_current_span()
        ctx = span.get_span_context() if span is not None else None
        if ctx is not None and getattr(ctx, "is_valid", False):
            return f"{ctx.trace_id:032x}", f"{ctx.span_id:016x}", None
    return "", "", None


@contextmanager
def start_traced_span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
) -> Iterator[SpanRecord]:
    """Context manager creating a linked child span in OpenTelemetry and local ledger."""
    configure_tracing()
    stack = _ACTIVE_SPAN_STACK.get()
    parent = stack[-1] if stack else None
    trace_id = parent.trace_id if parent is not None else secrets.token_hex(16)
    span_id = secrets.token_hex(8)
    parent_span_id = parent.span_id if parent is not None else None

    record = SpanRecord(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        start_time_ns=time.time_ns(),
        attributes=dict(attributes or {}),
    )
    token = _ACTIVE_SPAN_STACK.set((*stack, record))

    tracer = get_tracer()
    otel_cm = tracer.start_as_current_span(name) if tracer is not None else None
    otel_span = otel_cm.__enter__() if otel_cm is not None else None
    if otel_span is not None:
        ctx = otel_span.get_span_context()
        if ctx is not None and getattr(ctx, "is_valid", False):
            record.trace_id = f"{ctx.trace_id:032x}"
            record.span_id = f"{ctx.span_id:016x}"
        for key, value in record.attributes.items():
            if isinstance(value, (bool, int, float, str)):
                otel_span.set_attribute(key, value)

    try:
        yield record
    except Exception as exc:
        record.status = "ERROR"
        record.attributes["error.type"] = type(exc).__name__
        record.attributes["error.message"] = str(exc)
        if otel_span is not None:
            otel_span.record_exception(exc)
        raise
    finally:
        record.end_time_ns = time.time_ns()
        _RECORDED_SPANS.append(record)
        _ACTIVE_SPAN_STACK.reset(token)
        if otel_cm is not None:
            otel_cm.__exit__(None, None, None)


def get_recorded_spans() -> list[SpanRecord]:
    """Returns recorded spans for observability assertions and evaluation diagnostics."""
    return list(_RECORDED_SPANS)


def clear_recorded_spans() -> None:
    """Clears recorded in-memory spans (used between isolated test cases)."""
    _RECORDED_SPANS.clear()
    if _IN_MEMORY_EXPORTER is not None:
        _IN_MEMORY_EXPORTER.clear()
