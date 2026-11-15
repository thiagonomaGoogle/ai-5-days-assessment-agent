"""Multi-Agent C4 Model Architecture Creator built with Google ADK (`google-adk`).

Orchestration Topology:
- `root_agent` (`LlmAgent`: `c4_architecture_coordinator`)
  ├── Direct read-only lookup tools + HITL-gated `publish_approved_c4_architecture_bundle`
  └── `c4_generation_pipeline` (`SequentialAgent`)
        1. `parallel_discovery_stage` (`ParallelAgent`)
             ├── `requirements_domain_extractor` (`LlmAgent` on `gemini-2.5-flash`)
             └── `reference_pattern_retriever` (`LlmAgent` on `gemini-2.5-flash`)
        2. `c4_dsl_synthesizer_agent` (`LlmAgent` on `gemini-2.5-pro`)
        3. `architecture_compliance_reviewer` (`LlmAgent` on `gemini-2.5-pro`)

Application-wide capabilities registered on `app` (`google.adk.apps.App`):
- `EventsCompactionConfig` + `ContextCacheConfig` for context bloat & token cost control
- `ArchitectureGuardrailsPlugin` (prompt-injection defense, tool policy, self-eval)
- `ModelRoutingPlugin` (dynamic Flash vs. Pro routing)
- `IntentOutcomeAuditPlugin` (structured JSON `INTENT` + `OUTCOME` logs + OpenTelemetry)
- `AsyncMemoryConsolidationPlugin` (non-blocking background episodic memory persistence)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .config.settings import get_settings
from .memory.async_consolidator import AsyncMemoryConsolidationPlugin
from .memory.compaction import (
    build_adk_context_cache_config,
    build_adk_events_compaction_config,
)
from .memory.persistent_store import build_persistent_session_service
from .observability.audit_plugin import get_audit_plugin
from .observability.tracing import configure_tracing
from .orchestration.guardrails_plugin import ArchitectureGuardrailsPlugin
from .orchestration.hitl_confirmation import requires_publication_approval
from .orchestration.model_router import ModelRoutingPlugin
from .prompts.constitution import (
    C4_SYNTHESIZER_CONSTITUTION,
    COMPLIANCE_REVIEWER_CONSTITUTION,
    COORDINATOR_CONSTITUTION,
    DOMAIN_EXTRACTOR_CONSTITUTION,
    PATTERN_RETRIEVER_CONSTITUTION,
)
from .tools.c4_modeling_tools import (
    extract_c4_entities_from_requirements,
    generate_c4_mermaid_and_structurizr_dsl,
    search_reference_architecture_patterns,
)
from .tools.governance_tools import (
    publish_approved_c4_architecture_bundle,
    record_architecture_decision_record,
    validate_c4_architecture_compliance,
)

try:
    from google.adk.agents import LlmAgent, ParallelAgent, SequentialAgent
    from google.adk.apps import App, ResumabilityConfig
    from google.adk.tools import FunctionTool

    ADK_AVAILABLE = True
except ImportError:  # pragma: no cover - lightweight structural representation for offline CI
    ADK_AVAILABLE = False

    @dataclass
    class FunctionTool:  # type: ignore[no-redef]
        func: Any
        require_confirmation: Any = False
        name: str = ""

        def __post_init__(self) -> None:
            self.name = getattr(self.func, "__name__", "tool")

    @dataclass
    class LlmAgent:  # type: ignore[no-redef]
        name: str
        model: str
        description: str = ""
        instruction: str = ""
        tools: list[Any] = field(default_factory=list)
        sub_agents: list[Any] = field(default_factory=list)
        output_key: str | None = None

    @dataclass
    class ParallelAgent:  # type: ignore[no-redef]
        name: str
        description: str = ""
        sub_agents: list[Any] = field(default_factory=list)

    @dataclass
    class SequentialAgent:  # type: ignore[no-redef]
        name: str
        description: str = ""
        sub_agents: list[Any] = field(default_factory=list)

    @dataclass
    class ResumabilityConfig:  # type: ignore[no-redef]
        is_resumable: bool = True

    @dataclass
    class App:  # type: ignore[no-redef]
        name: str
        root_agent: Any
        plugins: list[Any] = field(default_factory=list)
        events_compaction_config: Any = None
        context_cache_config: Any = None
        resumability_config: Any = None


configure_tracing()
_settings = get_settings()

# Wrap tools in ADK FunctionTool; high-stakes catalog publication enforces HITL confirmation
extract_entities_tool = FunctionTool(extract_c4_entities_from_requirements)
search_patterns_tool = FunctionTool(search_reference_architecture_patterns)
generate_c4_dsl_tool = FunctionTool(generate_c4_mermaid_and_structurizr_dsl)
validate_compliance_tool = FunctionTool(validate_c4_architecture_compliance)
record_adr_tool = FunctionTool(record_architecture_decision_record)
publish_bundle_tool = FunctionTool(
    publish_approved_c4_architecture_bundle,
    require_confirmation=requires_publication_approval,
)

# Stage 1A (Parallel): Fast domain entity extraction on Gemini 2.5 Flash
requirements_domain_extractor = LlmAgent(
    name="requirements_domain_extractor",
    model=_settings.fast_model,
    description=(
        "Extracts C4 actors, bounded contexts, candidate containers, and regulatory "
        "compliance profiles from stakeholder requirements."
    ),
    instruction=DOMAIN_EXTRACTOR_CONSTITUTION,
    tools=[extract_entities_tool],
    output_key="extracted_domain_entities",
)

# Stage 1B (Parallel): Fast reference pattern & ADR retrieval on Gemini 2.5 Flash
reference_pattern_retriever = LlmAgent(
    name="reference_pattern_retriever",
    model=_settings.fast_model,
    description=(
        "Retrieves vetted enterprise C4 reference architectures and historical ADRs "
        "from the persistent vector knowledge store."
    ),
    instruction=PATTERN_RETRIEVER_CONSTITUTION,
    tools=[search_patterns_tool],
    output_key="retrieved_reference_patterns",
)

# Parallel Discovery Stage combining domain extraction + reference retrieval concurrently
parallel_discovery_stage = ParallelAgent(
    name="parallel_discovery_stage",
    description=(
        "Concurrently extracts stakeholder C4 entities and retrieves matching "
        "enterprise reference architectures."
    ),
    sub_agents=[
        requirements_domain_extractor,
        reference_pattern_retriever,
    ],
)

# Stage 2 (Sequential): Deep-reasoning C4 L1-L4 synthesis & ADR authoring on Gemini 2.5 Pro
c4_dsl_synthesizer_agent = LlmAgent(
    name="c4_dsl_synthesizer_agent",
    model=_settings.reasoning_model,
    description=(
        "Synthesizes Level 1-4 C4 architectures into validated Mermaid C4 diagrams "
        "and Structurizr DSL workspaces, recording key trade-offs as ADRs."
    ),
    instruction=C4_SYNTHESIZER_CONSTITUTION,
    tools=[generate_c4_dsl_tool, record_adr_tool],
    output_key="synthesized_c4_bundle",
)

# Stage 3 (Sequential): Deep-reasoning Well-Architected & regulatory compliance audit on Pro
architecture_compliance_reviewer = LlmAgent(
    name="architecture_compliance_reviewer",
    model=_settings.reasoning_model,
    description=(
        "Audits synthesized C4 specifications against Well-Architected security, "
        "encryption, decoupling, and regulatory compliance rules."
    ),
    instruction=COMPLIANCE_REVIEWER_CONSTITUTION,
    tools=[validate_compliance_tool],
    output_key="compliance_audit_report",
)

# End-to-end Sequential C4 Creation & Verification Pipeline
c4_generation_pipeline = SequentialAgent(
    name="c4_generation_pipeline",
    description=(
        "End-to-end C4 Model creation pipeline executing parallel discovery, "
        "L1-L4 Mermaid & Structurizr DSL synthesis, and compliance verification."
    ),
    sub_agents=[
        parallel_discovery_stage,
        c4_dsl_synthesizer_agent,
        architecture_compliance_reviewer,
    ],
)

# Root Coordinator Agent exported for `adk web`, `adk run`, and `adk eval`
root_agent = LlmAgent(
    name="c4_architecture_coordinator",
    model=_settings.fast_model,
    description=(
        "Principal C4 Model Architecture Coordinator routing quick catalog inquiries, "
        "full L1-L4 multi-agent C4 generation, and HITL-approved catalog releases."
    ),
    instruction=COORDINATOR_CONSTITUTION,
    tools=[
        search_patterns_tool,
        extract_entities_tool,
        publish_bundle_tool,
    ],
    sub_agents=[c4_generation_pipeline],
)

# Top-level ADK Application container with plugins, compaction, caching, and resumability
app = App(
    name=_settings.app_name,
    root_agent=root_agent,
    plugins=[
        ArchitectureGuardrailsPlugin(),
        ModelRoutingPlugin(),
        get_audit_plugin(),
        AsyncMemoryConsolidationPlugin(),
    ],
    events_compaction_config=build_adk_events_compaction_config(),
    context_cache_config=build_adk_context_cache_config(),
    resumability_config=ResumabilityConfig(is_resumable=True),
)

# Helper factory for custom FastAPI / Cloud Run runners needing explicit session persistence
default_session_service = build_persistent_session_service()
