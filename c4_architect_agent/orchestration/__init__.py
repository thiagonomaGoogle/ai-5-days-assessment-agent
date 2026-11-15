"""Orchestration plugins: strategic model routing, policy guardrails, and HITL hooks."""

from .guardrails_plugin import ArchitectureGuardrailsPlugin, evaluate_c4_output_quality
from .hitl_confirmation import (
    HitlDecision,
    requires_publication_approval,
    verify_or_request_human_confirmation,
)
from .model_router import ModelRoutingDecision, ModelRoutingPlugin, StrategicModelRouter

__all__ = [
    "ArchitectureGuardrailsPlugin",
    "HitlDecision",
    "ModelRoutingDecision",
    "ModelRoutingPlugin",
    "StrategicModelRouter",
    "evaluate_c4_output_quality",
    "requires_publication_approval",
    "verify_or_request_human_confirmation",
]
