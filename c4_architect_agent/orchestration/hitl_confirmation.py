"""Human-in-the-Loop (HITL) confirmation gates for high-stakes governance actions.

Integrates directly with Google ADK's `FunctionTool(..., require_confirmation=...)`,
`tool_context.request_confirmation(...)`, and `tool_context.tool_confirmation` so
high-impact mutations (such as publishing baselines to the enterprise architecture
catalog) pause execution until a human architect explicitly approves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..observability.structured_logger import log_structured_event


@dataclass(frozen=True)
class HitlDecision:
    """Represents the evaluated Human-in-the-Loop confirmation state."""

    approved: bool
    status: str  # "APPROVED" | "REJECTED" | "PENDING_HUMAN_APPROVAL"
    approver: str
    reason: str


def requires_publication_approval(target_catalog: str = "enterprise-catalog", **_: Any) -> bool:
    """Predicate compatible with `FunctionTool(func, require_confirmation=...)`.

    Requires explicit human confirmation whenever publishing to production governance
    catalogs (`enterprise-catalog` or `architecture-review-board`).
    """
    return target_catalog in {"enterprise-catalog", "architecture-review-board"}


def verify_or_request_human_confirmation(
    tool_context: Any | None,
    *,
    action_summary: str,
    confirmation_payload: dict[str, Any],
    explicit_approval_override: bool = False,
) -> HitlDecision:
    """Enforces an explicit code stop unless human approval has been granted.

    Args:
        tool_context: Active ADK `ToolContext` (if running inside ADK tool execution).
        action_summary: Human-readable prompt describing the high-stakes change.
        confirmation_payload: Structured metadata displayed in the approval UI.
        explicit_approval_override: Deterministic test/CLI flag only when pre-confirmed.

    Returns:
        HitlDecision indicating whether execution may proceed or must halt.
    """
    # 1. Inspect ADK native `tool_context.tool_confirmation` when resuming after approval
    if tool_context is not None:
        confirmation = getattr(tool_context, "tool_confirmation", None)
        if confirmation is not None:
            is_confirmed = bool(getattr(confirmation, "confirmed", False))
            payload = getattr(confirmation, "payload", None) or {}
            approver = (
                str(payload.get("approved_by", "human_architect"))
                if isinstance(payload, dict)
                else "human_architect"
            )
            if is_confirmed:
                log_structured_event(
                    "HITL_CONFIRMATION_APPROVED",
                    f"Human approved high-stakes action: {action_summary}",
                    metadata={"approver": approver, "payload": confirmation_payload},
                )
                return HitlDecision(
                    approved=True,
                    status="APPROVED",
                    approver=approver,
                    reason="Confirmed via ADK ToolConfirmation.",
                )
            log_structured_event(
                "HITL_CONFIRMATION_REJECTED",
                f"Human rejected high-stakes action: {action_summary}",
                metadata={"payload": confirmation_payload},
            )
            return HitlDecision(
                approved=False,
                status="REJECTED",
                approver=approver,
                reason="Human reviewer declined the confirmation request.",
            )

        # Check session state approval token if set interactively
        state = getattr(tool_context, "state", None)
        if state is not None and bool(state.get("human_approval_granted", False)):
            approver = str(state.get("human_approver", "human_architect"))
            return HitlDecision(
                approved=True,
                status="APPROVED",
                approver=approver,
                reason="Confirmed via session approval state.",
            )

        # Request confirmation through ADK's ToolContext pause mechanism when available
        request_fn = getattr(tool_context, "request_confirmation", None)
        function_call_id = getattr(tool_context, "function_call_id", None)
        if callable(request_fn) and function_call_id:
            request_fn(
                hint=(
                    f"Approval required: {action_summary}. "
                    "Confirm to publish this C4 baseline to the target governance catalog."
                ),
                payload=confirmation_payload,
            )

    if explicit_approval_override:
        return HitlDecision(
            approved=True,
            status="APPROVED",
            approver="explicit_cli_approver",
            reason="Explicit human override confirmed.",
        )

    log_structured_event(
        "HITL_CONFIRMATION_REQUESTED",
        f"Halting execution awaiting human confirmation: {action_summary}",
        metadata=confirmation_payload,
    )
    return HitlDecision(
        approved=False,
        status="PENDING_HUMAN_APPROVAL",
        approver="",
        reason=(
            f"Execution paused awaiting human approval for: {action_summary}. "
            "Ask the user to review the C4 bundle and explicitly confirm publication."
        ),
    )
