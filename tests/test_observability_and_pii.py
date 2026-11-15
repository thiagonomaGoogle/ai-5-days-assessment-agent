"""Tests for Category 4: Observability & Tracing (JSON logs, Intent vs Outcome, OTel, PII)."""

from __future__ import annotations

from dataclasses import dataclass, field
import io
import json
import logging
import unittest

from c4_architect_agent.observability.audit_plugin import IntentOutcomeAuditPlugin
from c4_architect_agent.observability.pii_redactor import PiiRedactionPipeline
from c4_architect_agent.observability.structured_logger import (
    CloudStructuredJsonFormatter,
    PiiSanitizingFilter,
)
from c4_architect_agent.observability.tracing import (
    clear_recorded_spans,
    get_recorded_spans,
    start_traced_span,
)


@dataclass
class _DummyTool:
    name: str = "generate_c4_mermaid_and_structurizr_dsl"


@dataclass
class _DummySession:
    id: str = "sess-obs-999"


@dataclass
class _DummyToolCtx:
    function_call_id: str = "fc-123"
    agent_name: str = "c4_dsl_synthesizer_agent"
    session: _DummySession = field(default_factory=_DummySession)


class ObservabilityAndPiiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        clear_recorded_spans()

    def test_pii_redactor_scrubs_emails_ssn_cpf_tokens_and_secret_keys(self) -> None:
        redactor = PiiRedactionPipeline(enable_cloud_dlp=False)
        raw = (
            "Owner maria.silva@company.org, SSN 123-45-6789, CPF 123.456.789-01, "
            "IP 10.24.8.19, token AIzaSyD1234567890abcdefghijklmnop"
        )
        scrubbed = redactor.redact_text(raw)
        self.assertNotIn("maria.silva@company.org", scrubbed)
        self.assertNotIn("123-45-6789", scrubbed)
        self.assertNotIn("123.456.789-01", scrubbed)
        self.assertNotIn("10.24.8.19", scrubbed)
        self.assertNotIn("AIzaSyD", scrubbed)
        self.assertIn("[REDACTED_EMAIL]", scrubbed)
        self.assertIn("[REDACTED_SSN]", scrubbed)
        self.assertIn("[REDACTED_CPF]", scrubbed)
        self.assertIn("[REDACTED_API_TOKEN]", scrubbed)

        nested = redactor.redact_payload(
            {"api_key": "super-secret-value", "notes": "Call +55 (11) 98765-4321"}
        )
        self.assertEqual(nested["api_key"], "[REDACTED_SECRET_FIELD]")
        self.assertIn("[REDACTED_PHONE]", nested["notes"])

    def test_structured_json_logger_includes_trace_context_and_redacts_pii(self) -> None:
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        handler.setFormatter(
            CloudStructuredJsonFormatter(service_name="c4-test-svc", project_id="demo-gcp")
        )
        handler.addFilter(PiiSanitizingFilter())

        test_logger = logging.getLogger("c4_json_test")
        test_logger.handlers = [handler]
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False

        with start_traced_span("request.c4_design") as root_span:
            test_logger.info(
                "Designing architecture for architect@acme.io",
                extra={
                    "event_type": "C4_SYNTHESIS",
                    "agent_name": "c4_dsl_synthesizer_agent",
                    "session_id": "sess-42",
                    "structured_data": {"client_secret": "do-not-log", "containers": 4},
                },
            )

        parsed = json.loads(buf.getvalue().strip())
        self.assertEqual(parsed["severity"], "INFO")
        self.assertEqual(parsed["event_type"], "C4_SYNTHESIS")
        self.assertEqual(parsed["trace_id"], root_span.trace_id)
        self.assertIn("[REDACTED_EMAIL]", parsed["message"])
        self.assertEqual(parsed["metadata"]["client_secret"], "[REDACTED_SECRET_FIELD]")

    def test_distributed_tracing_links_parent_and_child_spans(self) -> None:
        with start_traced_span("user_query") as parent_span:
            with start_traced_span("coordinator.route") as child_span:
                self.assertEqual(child_span.trace_id, parent_span.trace_id)
                self.assertEqual(child_span.parent_span_id, parent_span.span_id)

        spans = get_recorded_spans()
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0].name, "coordinator.route")
        self.assertEqual(spans[1].name, "user_query")

    async def test_audit_plugin_captures_paired_intent_and_outcome_records(self) -> None:
        plugin = IntentOutcomeAuditPlugin()
        tool = _DummyTool()
        ctx = _DummyToolCtx()

        await plugin.before_tool_callback(
            tool=tool,
            tool_args={"project_slug": "omnipay", "contact": "dev@corp.io"},
            tool_context=ctx,
        )
        await plugin.after_tool_callback(
            tool=tool,
            tool_args={"project_slug": "omnipay"},
            tool_context=ctx,
            result={"status": "success", "containers": 4},
        )

        self.assertEqual(len(plugin.ledger), 2)
        intent_entry, outcome_entry = plugin.ledger[0], plugin.ledger[1]
        self.assertEqual(intent_entry.phase, "INTENT")
        self.assertEqual(outcome_entry.phase, "OUTCOME")
        self.assertEqual(intent_entry.correlation_id, outcome_entry.correlation_id)
        self.assertEqual(
            intent_entry.sanitized_payload["arguments"]["contact"],
            "[REDACTED_EMAIL]",
        )
        self.assertIsNotNone(outcome_entry.latency_ms)


if __name__ == "__main__":
    unittest.main()
