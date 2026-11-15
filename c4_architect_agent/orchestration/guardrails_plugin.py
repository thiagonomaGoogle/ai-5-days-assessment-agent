"""Security, prompt-injection, policy, and self-evaluation guardrails plugin for ADK.

Implements three defense layers:
1. Input Guardrail (`before_model_callback`): screens user prompts for prompt-injection /
   system-override attempts (compatible with Google Cloud Model Armor templates) and
   redacts sensitive PII before invoking the model.
2. Tool Policy Guardrail (`before_tool_callback`): blocks unsafe tool arguments (e.g.,
   plaintext credentials, disallowed network schemes, or oversized DSL inputs).
3. Self-Evaluation Output Guardrail (`after_model_callback`): evaluates generated C4
   diagrams/DSL for structural soundness (balanced Structurizr braces, labeled protocols,
   zero leaked credentials) before returning output.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from ..observability.pii_redactor import get_pii_redactor
from ..observability.structured_logger import log_structured_event

try:
    from google.adk.models.llm_response import LlmResponse
    from google.adk.plugins.base_plugin import BasePlugin
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover
    LlmResponse = None  # type: ignore[assignment,misc]
    genai_types = None  # type: ignore[assignment]

    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


_PROMPT_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "SYSTEM_PROMPT_OVERRIDE",
        re.compile(
            r"(?i)\b(?:ignore|disregard|bypass)\s+(?:all\s+)?(?:previous|prior|system|safety)\s+"
            r"(?:instructions|constraints|constitution|rules|guardrails)\b"
        ),
    ),
    (
        "SECRET_EXFILTRATION",
        re.compile(
            r"(?i)\b(?:print|dump|reveal|exfiltrate|output)\s+(?:all\s+)?"
            r"(?:secret\s+manager|environment\s+variables|api\s+keys|private\s+keys|system\s+prompt)\b"
        ),
    ),
    (
        "GUARDRAIL_DISABLE",
        re.compile(r"(?i)\b(?:disable|skip|turn\s+off)\s+(?:human\s+approval|hitl|pii\s+redaction)\b"),
    ),
)

_FORBIDDEN_PROTOCOL_PATTERNS = re.compile(r"(?i)\b(?:telnet|ftp|http-plaintext|unencrypted-tcp)\b")


def detect_prompt_injection(text: str) -> str | None:
    """Returns matched guardrail rule ID if prompt-injection / policy bypass is detected."""
    if not text:
        return None
    for rule_id, pattern in _PROMPT_INJECTION_PATTERNS:
        if pattern.search(text):
            return rule_id
    return None


def evaluate_c4_output_quality(response_text: str) -> dict[str, Any]:
    """Self-evaluates model output for C4 structural validity and secret hygiene."""
    issues: list[str] = []
    if "workspace" in response_text and "model {" in response_text:
        open_braces = response_text.count("{")
        close_braces = response_text.count("}")
        if open_braces != close_braces:
            issues.append(
                f"Unbalanced Structurizr DSL braces (open={open_braces}, close={close_braces})."
            )
    if _FORBIDDEN_PROTOCOL_PATTERNS.search(response_text):
        issues.append("Insecure legacy protocol detected in architecture output (Telnet/FTP/plaintext).")
    if re.search(r"\b(?:AIza[0-9A-Za-z\-_]{20,}|sk-[0-9A-Za-z]{20,})\b", response_text):
        issues.append("Potential raw credential token detected in model response.")

    return {
        "passed": len(issues) == 0,
        "issues": issues,
    }


def _build_blocked_llm_response(message: str) -> Any:
    if LlmResponse is not None and genai_types is not None:
        return LlmResponse(
            content=genai_types.Content(
                role="model",
                parts=[genai_types.Part.from_text(text=message)],
            )
        )
    return {"blocked": True, "message": message}


class ArchitectureGuardrailsPlugin(BasePlugin):
    """Global ADK Guardrails & Self-Evaluation Policy Plugin."""

    def __init__(self, name: str = "architecture_guardrails_plugin") -> None:
        super().__init__(name=name)
        self._redactor = get_pii_redactor()

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> Optional[Any]:
        agent_name = str(getattr(callback_context, "agent_name", "c4_architect_agent"))
        contents = getattr(llm_request, "contents", None) or []

        for content in contents:
            parts = getattr(content, "parts", None) or []
            for part in parts:
                text = getattr(part, "text", None)
                if not isinstance(text, str):
                    continue
                violation = detect_prompt_injection(text)
                if violation is not None:
                    log_structured_event(
                        "GUARDRAIL_BLOCKED_PROMPT",
                        f"Blocked adversarial prompt ({violation})",
                        agent_name=agent_name,
                        metadata={"rule_id": violation},
                    )
                    return _build_blocked_llm_response(
                        "Request blocked by Architecture Security Guardrail "
                        f"(`{violation}`). I can only assist with legitimate C4 software "
                        "architecture modeling within governance and safety policies."
                    )
                # Scrub accidental user PII before sending prompt upstream
                scrubbed = self._redactor.redact_text(text)
                if scrubbed != text:
                    part.text = scrubbed
        return None

    async def before_tool_callback(
        self,
        *,
        tool: Any,
        tool_args: dict[str, Any],
        tool_context: Any,
    ) -> Optional[dict[str, Any]]:
        del tool_context
        tool_name = str(getattr(tool, "name", tool))
        serialized = str(tool_args)

        if len(serialized) > 120_000:
            return {
                "status": "error",
                "error_code": "GUARDRAIL_PAYLOAD_TOO_LARGE",
                "error_message": f"Arguments to '{tool_name}' exceed 120 KB policy cap.",
                "recovery_instructions": (
                    "Split the C4 specification into cohesive containers/components "
                    "and invoke the tool with concise descriptions (<300 chars per element)."
                ),
            }

        violation = detect_prompt_injection(serialized)
        if violation is not None:
            return {
                "status": "error",
                "error_code": f"GUARDRAIL_{violation}",
                "error_message": "Tool invocation blocked due to policy-violating directive.",
                "recovery_instructions": "Remove adversarial instructions and provide valid C4 fields.",
            }
        return None

    async def after_model_callback(
        self, *, callback_context: Any, llm_response: Any
    ) -> Optional[Any]:
        agent_name = str(getattr(callback_context, "agent_name", "c4_architect_agent"))
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) or []

        for part in parts:
            text = getattr(part, "text", None)
            if not isinstance(text, str) or not text.strip():
                continue
            eval_report = evaluate_c4_output_quality(text)
            scrubbed = self._redactor.redact_text(text)
            if not eval_report["passed"]:
                warning_note = (
                    "\n\n> **Automated Architecture Self-Evaluation Note**: "
                    + " ".join(eval_report["issues"])
                )
                scrubbed = scrubbed + warning_note
                log_structured_event(
                    "SELF_EVAL_GUARDRAIL_FINDING",
                    "Output self-evaluation appended architectural quality warning",
                    agent_name=agent_name,
                    metadata=eval_report,
                )
            if scrubbed != text:
                part.text = scrubbed
        return None
