"""Intent vs. Outcome audit plugin for Google ADK.

Explicitly captures and correlates:
1. INTENT before execution (`before_agent_callback`, `before_tool_callback`)
2. OUTCOME after execution (`after_agent_callback`, `after_tool_callback`, `on_tool_error_callback`)

Every pair shares a deterministic `correlation_id`, active OpenTelemetry span,
and PII-redacted argument/result payloads.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import functools
import logging
import time
from typing import Any, Optional
import uuid

from .pii_redactor import get_pii_redactor
from .structured_logger import log_structured_event
from .tracing import start_traced_span

try:
    from google.adk.plugins.base_plugin import BasePlugin
except ImportError:  # pragma: no cover - minimal BasePlugin shim when ADK wheel absent
    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


@dataclass
class AuditLedgerEntry:
    """Immutable audit record capturing either an INTENT or an OUTCOME event."""

    correlation_id: str
    phase: str  # "INTENT" | "OUTCOME"
    target_kind: str  # "AGENT" | "TOOL"
    target_name: str
    agent_name: str
    session_id: str
    timestamp_utc: str
    status: str
    trace_id: str = ""
    span_id: str = ""
    latency_ms: float | None = None
    sanitized_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IntentOutcomeAuditPlugin(BasePlugin):
    """ADK Plugin recording paired INTENT (pre-execution) and OUTCOME (post-execution) logs."""

    def __init__(self, name: str = "intent_outcome_audit_plugin") -> None:
        super().__init__(name=name)
        self._redactor = get_pii_redactor()
        self._inflight_timers: dict[str, tuple[str, float, Any]] = {}
        self.ledger: list[AuditLedgerEntry] = []

    @staticmethod
    def _extract_session_id(ctx: Any) -> str:
        session = getattr(ctx, "session", None)
        if session is not None and getattr(session, "id", None):
            return str(session.id)
        inv = getattr(ctx, "_invocation_context", None)
        if inv is not None and getattr(inv, "session", None) is not None:
            return str(inv.session.id)
        return "local-session"

    @staticmethod
    def _extract_agent_name(ctx: Any, fallback: str = "") -> str:
        agent_name = getattr(ctx, "agent_name", None)
        if agent_name:
            return str(agent_name)
        node = getattr(ctx, "node", None)
        if node is not None and getattr(node, "name", None):
            return str(node.name)
        return fallback or "c4_architect_agent"

    async def before_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> Optional[Any]:
        agent_name = getattr(agent, "name", "unknown_agent")
        session_id = self._extract_session_id(callback_context)
        correlation_id = f"agent-{uuid.uuid4().hex[:12]}"
        span_cm = start_traced_span(
            f"agent.{agent_name}",
            attributes={"agent.name": agent_name, "session.id": session_id},
        )
        span_record = span_cm.__enter__()
        key = f"agent:{id(callback_context)}:{agent_name}"
        self._inflight_timers[key] = (correlation_id, time.perf_counter(), span_cm)

        entry = AuditLedgerEntry(
            correlation_id=correlation_id,
            phase="INTENT",
            target_kind="AGENT",
            target_name=agent_name,
            agent_name=agent_name,
            session_id=session_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            status="PLANNED",
            trace_id=span_record.trace_id,
            span_id=span_record.span_id,
            sanitized_payload={
                "intended_action": f"Execute specialist agent '{agent_name}'",
                "model": str(getattr(agent, "model", "workflow")),
            },
        )
        self.ledger.append(entry)
        log_structured_event(
            "AGENT_INTENT",
            f"Intent: invoking agent '{agent_name}'",
            agent_name=agent_name,
            session_id=session_id,
            metadata=entry.to_dict(),
        )
        return None

    async def after_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> Optional[Any]:
        agent_name = getattr(agent, "name", "unknown_agent")
        session_id = self._extract_session_id(callback_context)
        key = f"agent:{id(callback_context)}:{agent_name}"
        inflight = self._inflight_timers.pop(key, None)
        if inflight is not None:
            correlation_id, started_at, span_cm = inflight
            latency_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
            span_cm.__exit__(None, None, None)
        else:
            correlation_id = f"agent-{uuid.uuid4().hex[:12]}"
            latency_ms = 0.0

        entry = AuditLedgerEntry(
            correlation_id=correlation_id,
            phase="OUTCOME",
            target_kind="AGENT",
            target_name=agent_name,
            agent_name=agent_name,
            session_id=session_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            status="COMPLETED",
            latency_ms=latency_ms,
            sanitized_payload={"outcome": f"Agent '{agent_name}' completed execution."},
        )
        self.ledger.append(entry)
        log_structured_event(
            "AGENT_OUTCOME",
            f"Outcome: agent '{agent_name}' completed in {latency_ms}ms",
            agent_name=agent_name,
            session_id=session_id,
            metadata=entry.to_dict(),
        )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
    ) -> Optional[dict[str, Any]]:
        tool_name = getattr(tool, "name", str(tool))
        agent_name = self._extract_agent_name(tool_context)
        session_id = self._extract_session_id(tool_context)
        call_id = getattr(tool_context, "function_call_id", None) or uuid.uuid4().hex[:12]
        correlation_id = f"tool-{call_id}"

        span_cm = start_traced_span(
            f"tool.{tool_name}",
            attributes={
                "tool.name": tool_name,
                "agent.name": agent_name,
                "session.id": session_id,
            },
        )
        span_record = span_cm.__enter__()
        self._inflight_timers[correlation_id] = (
            correlation_id,
            time.perf_counter(),
            span_cm,
        )

        sanitized_args = self._redactor.redact_payload(tool_args)
        entry = AuditLedgerEntry(
            correlation_id=correlation_id,
            phase="INTENT",
            target_kind="TOOL",
            target_name=tool_name,
            agent_name=agent_name,
            session_id=session_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            status="INTENDED",
            trace_id=span_record.trace_id,
            span_id=span_record.span_id,
            sanitized_payload={
                "intended_tool": tool_name,
                "arguments": sanitized_args,
            },
        )
        self.ledger.append(entry)
        log_structured_event(
            "TOOL_INTENT",
            f"Intent: calling tool '{tool_name}'",
            agent_name=agent_name,
            session_id=session_id,
            metadata=entry.to_dict(),
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        result: dict[str, Any],
    ) -> Optional[dict[str, Any]]:
        del tool_args
        tool_name = getattr(tool, "name", str(tool))
        agent_name = self._extract_agent_name(tool_context)
        session_id = self._extract_session_id(tool_context)
        call_id = getattr(tool_context, "function_call_id", None)
        correlation_id = f"tool-{call_id}" if call_id else f"tool-{uuid.uuid4().hex[:12]}"

        inflight = self._inflight_timers.pop(correlation_id, None)
        if inflight is not None:
            _, started_at, span_cm = inflight
            latency_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
            span_cm.__exit__(None, None, None)
        else:
            latency_ms = 0.0

        sanitized_result = self._redactor.redact_payload(result)
        result_status = (
            str(result.get("status", "SUCCESS")).upper()
            if isinstance(result, dict)
            else "SUCCESS"
        )
        entry = AuditLedgerEntry(
            correlation_id=correlation_id,
            phase="OUTCOME",
            target_kind="TOOL",
            target_name=tool_name,
            agent_name=agent_name,
            session_id=session_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            status=result_status,
            latency_ms=latency_ms,
            sanitized_payload={
                "tool": tool_name,
                "result_status": result_status,
                "result_preview": sanitized_result,
            },
        )
        self.ledger.append(entry)
        log_structured_event(
            "TOOL_OUTCOME",
            f"Outcome: tool '{tool_name}' finished with status={result_status} in {latency_ms}ms",
            level=logging.WARNING if result_status == "ERROR" else logging.INFO,
            agent_name=agent_name,
            session_id=session_id,
            metadata=entry.to_dict(),
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
        error: Exception,
    ) -> Optional[dict[str, Any]]:
        del tool_args
        tool_name = getattr(tool, "name", str(tool))
        agent_name = self._extract_agent_name(tool_context)
        session_id = self._extract_session_id(tool_context)
        call_id = getattr(tool_context, "function_call_id", None)
        correlation_id = f"tool-{call_id}" if call_id else f"tool-{uuid.uuid4().hex[:12]}"

        inflight = self._inflight_timers.pop(correlation_id, None)
        if inflight is not None:
            _, started_at, span_cm = inflight
            latency_ms = round((time.perf_counter() - started_at) * 1000.0, 3)
            span_cm.__exit__(type(error), error, error.__traceback__)
        else:
            latency_ms = 0.0

        recovery_payload = {
            "status": "error",
            "error_code": "UNHANDLED_TOOL_RUNTIME_ERROR",
            "error_message": self._redactor.redact_text(str(error)),
            "recovery_instructions": (
                f"Inspect parameters passed to '{tool_name}', ensure required fields "
                "match its JSON schema, and retry with corrected values."
            ),
        }
        entry = AuditLedgerEntry(
            correlation_id=correlation_id,
            phase="OUTCOME",
            target_kind="TOOL",
            target_name=tool_name,
            agent_name=agent_name,
            session_id=session_id,
            timestamp_utc=datetime.now(timezone.utc).isoformat(),
            status="ERROR",
            latency_ms=latency_ms,
            sanitized_payload=recovery_payload,
        )
        self.ledger.append(entry)
        log_structured_event(
            "TOOL_OUTCOME_ERROR",
            f"Outcome error in tool '{tool_name}': {type(error).__name__}",
            level=logging.ERROR,
            agent_name=agent_name,
            session_id=session_id,
            metadata=entry.to_dict(),
        )
        return recovery_payload


@functools.lru_cache(maxsize=1)
def get_audit_plugin() -> IntentOutcomeAuditPlugin:
    """Returns singleton IntentOutcomeAuditPlugin instance."""
    return IntentOutcomeAuditPlugin()
