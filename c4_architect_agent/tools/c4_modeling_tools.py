"""C4 Model discovery, reference retrieval, and L1-L4 Mermaid/Structurizr DSL generation tools.

All tools follow strict AgentOps design rules:
- Comprehensive docstrings documenting purpose, parameters, and return structure.
- Specific, domain-descriptive verb-noun function names.
- Explicit Pydantic v2 JSON Schema validation on inputs and outputs.
- Guided Error Handling returning actionable LLM recovery instructions instead of crashing.
"""

from __future__ import annotations

import re
from typing import Any

from ..memory.persistent_store import get_knowledge_store
from ..observability.pii_redactor import get_pii_redactor
from ..observability.tracing import start_traced_span
from ..schemas.c4_schemas import (
    C4ArchitectureSpecInput,
    C4DiagramBundleOutput,
    GuidedToolError,
    PatternSearchInput,
    SchemaValidationError,
    validate_against_schema,
)


def _guided_error(
    *,
    error_code: str,
    error_message: str,
    recovery_instructions: str,
    invalid_fields: list[dict[str, str]] | None = None,
    schema_cls: type[Any] | None = None,
) -> dict[str, Any]:
    """Builds a validated `GuidedToolError` dictionary guiding the LLM to self-correct."""
    schema_summary: dict[str, Any] = {}
    if schema_cls is not None and hasattr(schema_cls, "model_json_schema"):
        full_schema = schema_cls.model_json_schema()
        schema_summary = {
            "title": full_schema.get("title", schema_cls.__name__),
            "required": full_schema.get("required", []),
        }
    err = GuidedToolError(
        error_code=error_code,
        error_message=error_message,
        invalid_fields=invalid_fields or [],
        recovery_instructions=recovery_instructions,
        expected_schema_summary=schema_summary,
    )
    return err.model_dump()


def extract_c4_entities_from_requirements(
    project_slug: str,
    requirements_brief: str,
    compliance_profile: str = "standard",
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Extracts candidate C4 Level 1-2 architectural entities from raw stakeholder requirements.

    Parses unstructured product and non-functional requirements to surface candidate
    personas/actors, runtime containers (web/API/workers/databases/queues), external
    integrations, and regulatory constraints, persisting workspace metadata in session state.

    Args:
        project_slug: Lowercase identifier matching `^[a-z][a-z0-9_-]{1,62}$`
            (e.g., `'omnipay_checkout_platform'`).
        requirements_brief: Stakeholder problem statement, expected traffic, data stores,
            and external integrations (minimum 15 characters).
        compliance_profile: Regulatory baseline to apply. Must be one of
            `'standard'`, `'pci-dss'`, `'hipaa'`, `'lgpd-gdpr'`, or `'fedramp'`.
        tool_context: Optional ADK `ToolContext` automatically injected at runtime to
            persist `project_slug` and `compliance_profile` in session state.

    Returns:
        Dictionary with `status="success"` and structured candidate C4 entities, or
        `status="error"` with `recovery_instructions` if inputs are invalid.
    """
    with start_traced_span(
        "tool.extract_c4_entities_from_requirements",
        attributes={"c4.project_slug": project_slug},
    ):
        if not re.match(r"^[a-z][a-z0-9_-]{1,62}$", project_slug or ""):
            return _guided_error(
                error_code="INVALID_PROJECT_SLUG",
                error_message=(
                    f"project_slug '{project_slug}' must start with a lowercase letter and "
                    "contain only lowercase letters, digits, hyphens, or underscores."
                ),
                invalid_fields=[{"field": "project_slug", "reason": "Pattern mismatch"}],
                recovery_instructions=(
                    "Convert `project_slug` to lowercase snake_case (for example, "
                    "`'digital_banking_c4'`) and call `extract_c4_entities_from_requirements` again."
                ),
            )

        allowed_profiles = {"standard", "pci-dss", "hipaa", "lgpd-gdpr", "fedramp"}
        if compliance_profile not in allowed_profiles:
            return _guided_error(
                error_code="UNSUPPORTED_COMPLIANCE_PROFILE",
                error_message=f"Unsupported compliance_profile '{compliance_profile}'.",
                invalid_fields=[
                    {"field": "compliance_profile", "reason": f"Must be in {sorted(allowed_profiles)}"}
                ],
                recovery_instructions=(
                    "Pass one of: 'standard', 'pci-dss', 'hipaa', 'lgpd-gdpr', or 'fedramp'."
                ),
            )

        clean_brief = get_pii_redactor().redact_text((requirements_brief or "").strip())
        if len(clean_brief) < 15:
            return _guided_error(
                error_code="REQUIREMENTS_BRIEF_TOO_SHORT",
                error_message="requirements_brief must contain at least 15 characters of context.",
                invalid_fields=[{"field": "requirements_brief", "reason": "Too short (<15 chars)"}],
                recovery_instructions=(
                    "Provide a fuller architectural description naming primary users, core business "
                    "capabilities, and data persistence needs."
                ),
            )

        lowered = clean_brief.lower()
        detected_archetypes: list[dict[str, str]] = [
            {"container_type": "service", "suggested_alias": "api_gateway_service", "protocol": "HTTPS/OIDC"}
        ]
        if any(k in lowered for k in ("ui", "portal", "web", "dashboard", "frontend", "react", "next")):
            detected_archetypes.append(
                {"container_type": "webapp", "suggested_alias": "web_portal_app", "protocol": "HTTPS/TLS1.3"}
            )
        if any(k in lowered for k in ("mobile", "ios", "android", "flutter", "app")):
            detected_archetypes.append(
                {"container_type": "mobile", "suggested_alias": "mobile_client_app", "protocol": "HTTPS/OAuth2"}
            )
        if any(k in lowered for k in ("event", "async", "queue", "kafka", "pubsub", "pub/sub", "stream")):
            detected_archetypes.append(
                {"container_type": "queue", "suggested_alias": "domain_event_bus", "protocol": "PubSub/Protobuf-TLS"}
            )
        if any(k in lowered for k in ("cache", "redis", "memorystore", "low-latency")):
            detected_archetypes.append(
                {"container_type": "cache", "suggested_alias": "session_cache_store", "protocol": "RESP3/TLS"}
            )
        detected_archetypes.append(
            {"container_type": "database", "suggested_alias": "primary_relational_db", "protocol": "PostgreSQL/TLS"}
        )

        if tool_context is not None and getattr(tool_context, "state", None) is not None:
            tool_context.state["project_slug"] = project_slug
            tool_context.state["compliance_profile"] = compliance_profile

        return {
            "status": "success",
            "project_slug": project_slug,
            "compliance_profile": compliance_profile,
            "sanitized_requirements": clean_brief,
            "candidate_containers": detected_archetypes,
            "recommended_c4_levels": ["C4Context", "C4Container", "C4Component", "CodeClassDiagram"],
        }


def search_reference_architecture_patterns(
    query: str,
    domain_filter: str = "",
    top_k: int = 3,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Searches the persistent knowledge base for vetted C4 reference patterns and ADRs.

    Args:
        query: Natural-language search query describing architectural style, domain, or
            quality attributes (e.g., `'event-driven payment ledger pci-dss'`).
        domain_filter: Optional domain category filter (`'fintech'`, `'ai-platform'`,
            `'healthcare'`, or `''` for all domains).
        top_k: Maximum number of ranked patterns to return (integer between 1 and 10).
        tool_context: Optional ADK `ToolContext` for scoping project-specific ADRs.

    Returns:
        Dictionary containing `status="success"` and ranked `matches`, or `status="error"`
        with guided `recovery_instructions` if validation fails.
    """
    with start_traced_span("tool.search_reference_architecture_patterns"):
        try:
            validated: PatternSearchInput = validate_against_schema(
                PatternSearchInput,
                {"query": query, "domain_filter": domain_filter, "top_k": top_k},
            )
        except SchemaValidationError as exc:
            return _guided_error(
                error_code="INVALID_PATTERN_SEARCH_INPUT",
                error_message=str(exc),
                invalid_fields=exc.invalid_fields,
                recovery_instructions=(
                    "Ensure `query` is 3-300 characters and `top_k` is an integer between 1 and 10."
                ),
                schema_cls=PatternSearchInput,
            )

        project_slug = ""
        if tool_context is not None and getattr(tool_context, "state", None) is not None:
            project_slug = str(tool_context.state.get("project_slug", ""))

        matches = get_knowledge_store().search(
            validated.query,
            domain_filter=validated.domain_filter,
            project_slug=project_slug,
            top_k=validated.top_k,
        )
        return {
            "status": "success",
            "query": validated.query,
            "match_count": len(matches),
            "matches": matches,
        }


def _escape_quote(text: str) -> str:
    return text.replace('"', "'").replace("\n", " ").strip()


def _mermaid_container_macro(container_type: str) -> str:
    if container_type in {"database", "cache"}:
        return "ContainerDb"
    if container_type == "queue":
        return "ContainerQueue"
    return "Container"


def generate_c4_mermaid_and_structurizr_dsl(
    architecture_spec: dict[str, Any],
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Validates a C4 specification and renders L1-L4 Mermaid diagrams plus Structurizr DSL.

    Validates `architecture_spec` against `C4ArchitectureSpecInput`, verifies referential
    integrity across every relationship (`source_alias` and `target_alias` must exist),
    and produces four C4 views (Context, Container, Component, Code) plus a complete
    Structurizr DSL workspace.

    Args:
        architecture_spec: Structured dictionary conforming to `C4ArchitectureSpecInput`
            with keys `project_slug`, `system_name`, `system_description`,
            `compliance_profile`, `actors`, `containers`, `external_systems`,
            `relationships`, and optional `code_elements`.
        tool_context: Optional ADK `ToolContext` used to persist the latest generated
            C4 summary and Structurizr DSL into session state for downstream review.

    Returns:
        Validated `C4DiagramBundleOutput` dictionary (`status="success"`), or a
        `GuidedToolError` dictionary (`status="error"`) detailing exactly which schema
        field or relationship alias needs correction.
    """
    with start_traced_span("tool.generate_c4_mermaid_and_structurizr_dsl"):
        try:
            spec: C4ArchitectureSpecInput = validate_against_schema(
                C4ArchitectureSpecInput, architecture_spec
            )
        except SchemaValidationError as exc:
            return _guided_error(
                error_code="C4_SCHEMA_VALIDATION_FAILED",
                error_message=str(exc),
                invalid_fields=exc.invalid_fields,
                recovery_instructions=(
                    "Fix the listed `invalid_fields` in `architecture_spec`: ensure every alias "
                    "matches `^[a-z][a-z0-9_-]{1,62}$`, `container_type` is one of "
                    "['service', 'webapp', 'mobile', 'database', 'queue', 'cache'], and no "
                    "unexpected extra keys are passed."
                ),
                schema_cls=C4ArchitectureSpecInput,
            )

        # Verify referential integrity across actors, containers, components, external systems
        actor_aliases = {a.alias for a in spec.actors}
        ext_aliases = {e.alias for e in spec.external_systems}
        container_aliases = {c.alias for c in spec.containers}
        component_aliases = {
            comp.alias for c in spec.containers for comp in c.components
        }
        all_known_aliases = (
            actor_aliases | ext_aliases | container_aliases | component_aliases | {spec.project_slug}
        )

        # Check duplicate aliases across elements
        seen_aliases: set[str] = set()
        for alias in [
            *(a.alias for a in spec.actors),
            *(e.alias for e in spec.external_systems),
            *(c.alias for c in spec.containers),
            *(comp.alias for c in spec.containers for comp in c.components),
        ]:
            if alias in seen_aliases:
                return _guided_error(
                    error_code="DUPLICATE_C4_ELEMENT_ALIAS",
                    error_message=f"Duplicate C4 alias '{alias}' found across elements.",
                    invalid_fields=[{"field": "alias", "reason": f"Duplicate alias '{alias}'"}],
                    recovery_instructions=(
                        f"Assign unique snake_case aliases to every actor, container, component, "
                        f"and external system (rename duplicate '{alias}')."
                    ),
                )
            seen_aliases.add(alias)

        dangling_edges: list[dict[str, str]] = []
        for idx, rel in enumerate(spec.relationships):
            if rel.source_alias not in all_known_aliases:
                dangling_edges.append(
                    {
                        "field": f"relationships[{idx}].source_alias",
                        "reason": f"Unknown alias '{rel.source_alias}'",
                    }
                )
            if rel.target_alias not in all_known_aliases:
                dangling_edges.append(
                    {
                        "field": f"relationships[{idx}].target_alias",
                        "reason": f"Unknown alias '{rel.target_alias}'",
                    }
                )
        if dangling_edges:
            return _guided_error(
                error_code="DANGLING_C4_RELATIONSHIP_ALIAS",
                error_message="One or more relationships reference undefined element aliases.",
                invalid_fields=dangling_edges,
                recovery_instructions=(
                    f"Update `source_alias` / `target_alias` so every relationship connects "
                    f"defined aliases: {sorted(all_known_aliases)}."
                ),
            )

        sys_alias = spec.project_slug
        sys_name = _escape_quote(spec.system_name)
        sys_desc = _escape_quote(spec.system_description)

        # 1. Level 1: C4Context Mermaid
        l1_lines = [
            "C4Context",
            f'  title System Context Diagram for {sys_name}',
        ]
        for actor in spec.actors:
            macro = "Person_Ext" if actor.external else "Person"
            l1_lines.append(
                f'  {macro}({actor.alias}, "{_escape_quote(actor.label)}", "{_escape_quote(actor.description)}")'
            )
        l1_lines.append(f'  System({sys_alias}, "{sys_name}", "{sys_desc}")')
        for ext in spec.external_systems:
            l1_lines.append(
                f'  System_Ext({ext.alias}, "{_escape_quote(ext.label)}", "{_escape_quote(ext.description)}")'
            )

        l1_seen_pairs: set[tuple[str, str, str]] = set()
        for rel in spec.relationships:
            src = sys_alias if rel.source_alias in (container_aliases | component_aliases) else rel.source_alias
            tgt = sys_alias if rel.target_alias in (container_aliases | component_aliases) else rel.target_alias
            if src == tgt:
                continue
            edge_key = (src, tgt, rel.protocol)
            if edge_key not in l1_seen_pairs:
                l1_seen_pairs.add(edge_key)
                l1_lines.append(
                    f'  Rel({src}, {tgt}, "{_escape_quote(rel.label)}", "{_escape_quote(rel.protocol)}")'
                )

        # 2. Level 2: C4Container Mermaid
        l2_lines = [
            "C4Container",
            f'  title Container Diagram for {sys_name}',
        ]
        for actor in spec.actors:
            macro = "Person_Ext" if actor.external else "Person"
            l2_lines.append(
                f'  {macro}({actor.alias}, "{_escape_quote(actor.label)}", "{_escape_quote(actor.description)}")'
            )
        l2_lines.append(f'  System_Boundary({sys_alias}_boundary, "{sys_name}") {{')
        for cont in spec.containers:
            c_macro = _mermaid_container_macro(cont.container_type)
            l2_lines.append(
                f'    {c_macro}({cont.alias}, "{_escape_quote(cont.label)}", '
                f'"{_escape_quote(cont.technology)}", "{_escape_quote(cont.description)}")'
            )
        l2_lines.append("  }")
        for ext in spec.external_systems:
            l2_lines.append(
                f'  System_Ext({ext.alias}, "{_escape_quote(ext.label)}", "{_escape_quote(ext.description)}")'
            )
        for rel in spec.relationships:
            if rel.source_alias in component_aliases or rel.target_alias in component_aliases:
                continue
            l2_lines.append(
                f'  Rel({rel.source_alias}, {rel.target_alias}, '
                f'"{_escape_quote(rel.label)}", "{_escape_quote(rel.protocol)}")'
            )

        # 3. Level 3: C4Component Mermaid
        l3_lines = [
            "C4Component",
            f'  title Component Diagram for {sys_name}',
        ]
        for cont in spec.containers:
            if cont.components:
                l3_lines.append(
                    f'  Container_Boundary({cont.alias}_boundary, "{_escape_quote(cont.label)}") {{'
                )
                for comp in cont.components:
                    l3_lines.append(
                        f'    Component({comp.alias}, "{_escape_quote(comp.label)}", '
                        f'"{_escape_quote(comp.technology)}", "{_escape_quote(comp.description)}")'
                    )
                l3_lines.append("  }")
            else:
                c_macro = _mermaid_container_macro(cont.container_type)
                l3_lines.append(
                    f'  {c_macro}({cont.alias}, "{_escape_quote(cont.label)}", '
                    f'"{_escape_quote(cont.technology)}", "{_escape_quote(cont.description)}")'
                )
        for rel in spec.relationships:
            if rel.source_alias in (component_aliases | container_aliases) and rel.target_alias in (
                component_aliases | container_aliases | ext_aliases
            ):
                l3_lines.append(
                    f'  Rel({rel.source_alias}, {rel.target_alias}, '
                    f'"{_escape_quote(rel.label)}", "{_escape_quote(rel.protocol)}")'
                )

        # 4. Level 4: Code Class Diagram Mermaid
        l4_lines = ["classDiagram"]
        if spec.code_elements:
            for elem in spec.code_elements:
                l4_lines.append(f"  class {elem.class_name} {{")
                l4_lines.append(f"    <<{elem.stereotype}>>")
                for method_sig in elem.key_methods:
                    l4_lines.append(f"    +{method_sig}")
                l4_lines.append("  }")
        else:
            for cont in spec.containers[:3]:
                cls_name = "".join(part.capitalize() for part in re.split(r"[_-]", cont.alias))
                l4_lines.append(f"  class {cls_name}Handler {{")
                l4_lines.append("    <<service>>")
                l4_lines.append("    +execute(request) Response")
                l4_lines.append("  }")

        # 5. Structurizr DSL Workspace
        dsl_lines = [
            f'workspace "{sys_name}" "{sys_desc}" {{',
            "  model {",
        ]
        for actor in spec.actors:
            dsl_lines.append(
                f'    {actor.alias} = person "{_escape_quote(actor.label)}" "{_escape_quote(actor.description)}"'
            )
        for ext in spec.external_systems:
            dsl_lines.append(
                f'    {ext.alias} = softwareSystem "{_escape_quote(ext.label)}" "{_escape_quote(ext.description)}" "External"'
            )
        dsl_lines.append(f'    {sys_alias} = softwareSystem "{sys_name}" "{sys_desc}" {{')
        for cont in spec.containers:
            if cont.components:
                dsl_lines.append(
                    f'      {cont.alias} = container "{_escape_quote(cont.label)}" '
                    f'"{_escape_quote(cont.description)}" "{_escape_quote(cont.technology)}" {{'
                )
                for comp in cont.components:
                    dsl_lines.append(
                        f'        {comp.alias} = component "{_escape_quote(comp.label)}" '
                        f'"{_escape_quote(comp.description)}" "{_escape_quote(comp.technology)}"'
                    )
                dsl_lines.append("      }")
            else:
                dsl_lines.append(
                    f'      {cont.alias} = container "{_escape_quote(cont.label)}" '
                    f'"{_escape_quote(cont.description)}" "{_escape_quote(cont.technology)}"'
                )
        dsl_lines.append("    }")
        for rel in spec.relationships:
            dsl_lines.append(
                f'    {rel.source_alias} -> {rel.target_alias} '
                f'"{_escape_quote(rel.label)}" "{_escape_quote(rel.protocol)}"'
            )
        dsl_lines.extend(
            [
                "  }",
                "  views {",
                f'    systemContext {sys_alias} "SystemContext" {{',
                "      include *",
                "      autoLayout lr",
                "    }",
                f'    container {sys_alias} "Containers" {{',
                "      include *",
                "      autoLayout lr",
                "    }",
                "    theme default",
                "  }",
                "}",
            ]
        )

        structurizr_dsl = "\n".join(dsl_lines)
        bundle = C4DiagramBundleOutput(
            project_slug=spec.project_slug,
            system_name=spec.system_name,
            c4_context_mermaid="\n".join(l1_lines),
            c4_container_mermaid="\n".join(l2_lines),
            c4_component_mermaid="\n".join(l3_lines),
            c4_code_mermaid="\n".join(l4_lines),
            structurizr_dsl=structurizr_dsl,
            element_counts={
                "actors": len(spec.actors),
                "containers": len(spec.containers),
                "components": len(component_aliases),
                "external_systems": len(spec.external_systems),
                "relationships": len(spec.relationships),
            },
        )
        output_dict = bundle.model_dump()

        if tool_context is not None and getattr(tool_context, "state", None) is not None:
            tool_context.state["project_slug"] = spec.project_slug
            tool_context.state["compliance_profile"] = spec.compliance_profile
            tool_context.state["latest_structurizr_dsl"] = structurizr_dsl
            tool_context.state["latest_c4_summary"] = (
                f"System: {spec.system_name} ({spec.project_slug}) | "
                f"Containers: {[c.alias for c in spec.containers]} | "
                f"Compliance: {spec.compliance_profile}"
            )

        return output_dict
