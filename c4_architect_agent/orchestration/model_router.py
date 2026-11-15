"""Strategic Model Routing between fast low-latency Flash and deep-reasoning Pro models.

Routes requests dynamically based on agent role and architectural complexity:
- `gemini-2.5-flash`: intent classification, entity extraction, catalog lookup, syntax checks
- `gemini-2.5-pro`: multi-level L1-L4 C4 synthesis, trade-off ADRs, security/compliance audits
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from ..config.settings import get_settings
from ..observability.structured_logger import log_structured_event

try:
    from google.adk.plugins.base_plugin import BasePlugin
except ImportError:  # pragma: no cover
    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


_REASONING_KEYWORDS: tuple[str, ...] = (
    "structurizr",
    "trade-off",
    "tradeoff",
    "multi-region",
    "pci-dss",
    "hipaa",
    "fedramp",
    "lgpd",
    "gdpr",
    "zero-trust",
    "threat model",
    "consensus",
    "cqrs",
    "event sourcing",
    "full c4",
    "level 1 to level 4",
    "l1-l4",
    "adr",
)

_PRO_TIER_AGENTS = frozenset({
    "c4_dsl_synthesizer_agent",
    "architecture_compliance_reviewer",
})

_FLASH_TIER_AGENTS = frozenset({
    "requirements_domain_extractor",
    "reference_pattern_retriever",
})


@dataclass(frozen=True)
class ModelRoutingDecision:
    """Represents the selected Gemini model tier and routing rationale."""

    selected_model: str
    tier: str  # "FAST_FLASH" | "DEEP_REASONING_PRO"
    reason: str
    complexity_score: int


class StrategicModelRouter:
    """Selects the optimal Gemini model (`Flash` vs `Pro`) per task and prompt complexity."""

    def __init__(
        self,
        *,
        fast_model: str | None = None,
        reasoning_model: str | None = None,
        complexity_threshold: int = 3,
    ) -> None:
        settings = get_settings()
        self.fast_model = fast_model or settings.fast_model
        self.reasoning_model = reasoning_model or settings.reasoning_model
        self.complexity_threshold = complexity_threshold

    def score_prompt_complexity(self, prompt_text: str) -> int:
        """Computes a deterministic complexity score from architectural signals."""
        if not prompt_text:
            return 0
        lowered = prompt_text.lower()
        keyword_hits = sum(1 for kw in _REASONING_KEYWORDS if kw in lowered)
        length_points = 2 if len(prompt_text) > 900 else (1 if len(prompt_text) > 400 else 0)
        return keyword_hits + length_points

    def route(
        self,
        *,
        agent_name: str,
        prompt_text: str = "",
        compliance_profile: str = "standard",
    ) -> ModelRoutingDecision:
        """Determines whether `fast_model` (Flash) or `reasoning_model` (Pro) should serve the call."""
        complexity = self.score_prompt_complexity(prompt_text)

        if agent_name in _PRO_TIER_AGENTS:
            return ModelRoutingDecision(
                selected_model=self.reasoning_model,
                tier="DEEP_REASONING_PRO",
                reason=f"Specialist '{agent_name}' requires multi-level architectural reasoning.",
                complexity_score=max(complexity, self.complexity_threshold),
            )

        if agent_name in _FLASH_TIER_AGENTS and compliance_profile == "standard":
            return ModelRoutingDecision(
                selected_model=self.fast_model,
                tier="FAST_FLASH",
                reason=f"Specialist '{agent_name}' executes bounded extraction/retrieval.",
                complexity_score=complexity,
            )

        if compliance_profile in {"pci-dss", "hipaa", "fedramp"}:
            return ModelRoutingDecision(
                selected_model=self.reasoning_model,
                tier="DEEP_REASONING_PRO",
                reason=f"High-assurance compliance profile '{compliance_profile}' escalates to Pro.",
                complexity_score=complexity + 2,
            )

        if complexity >= self.complexity_threshold:
            return ModelRoutingDecision(
                selected_model=self.reasoning_model,
                tier="DEEP_REASONING_PRO",
                reason=f"Prompt complexity score ({complexity}) >= threshold ({self.complexity_threshold}).",
                complexity_score=complexity,
            )

        return ModelRoutingDecision(
            selected_model=self.fast_model,
            tier="FAST_FLASH",
            reason=f"Low-latency triage/lookup (complexity={complexity}).",
            complexity_score=complexity,
        )


class ModelRoutingPlugin(BasePlugin):
    """ADK Plugin dynamically routing `llm_request.model` in `before_model_callback`."""

    def __init__(
        self,
        router: StrategicModelRouter | None = None,
        name: str = "strategic_model_routing_plugin",
    ) -> None:
        super().__init__(name=name)
        self.router = router or StrategicModelRouter()

    @staticmethod
    def _extract_latest_prompt_text(llm_request: Any) -> str:
        contents = getattr(llm_request, "contents", None) or []
        chunks: list[str] = []
        for content in contents[-2:]:
            parts = getattr(content, "parts", None) or []
            for part in parts:
                text = getattr(part, "text", None)
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks)

    async def before_model_callback(
        self, *, callback_context: Any, llm_request: Any
    ) -> Optional[Any]:
        agent_name = str(getattr(callback_context, "agent_name", "c4_architecture_coordinator"))
        state = getattr(callback_context, "state", None)
        compliance_profile = (
            str(state.get("compliance_profile", "standard")) if state is not None else "standard"
        )
        prompt_text = self._extract_latest_prompt_text(llm_request)
        decision = self.router.route(
            agent_name=agent_name,
            prompt_text=prompt_text,
            compliance_profile=compliance_profile,
        )
        if hasattr(llm_request, "model"):
            llm_request.model = decision.selected_model

        log_structured_event(
            "MODEL_ROUTING_DECISION",
            f"Routed '{agent_name}' to '{decision.selected_model}' ({decision.tier})",
            agent_name=agent_name,
            metadata={
                "selected_model": decision.selected_model,
                "tier": decision.tier,
                "complexity_score": decision.complexity_score,
                "reason": decision.reason,
            },
        )
        return None
