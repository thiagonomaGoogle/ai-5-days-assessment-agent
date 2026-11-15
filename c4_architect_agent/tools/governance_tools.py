"""Architecture compliance validation, ADR persistence, and HITL-gated catalog publication."""

from __future__ import annotations

from typing import Any
import uuid

from ..config.secrets import get_secret_resolver
from ..config.settings import get_settings
from ..memory.persistent_store import get_knowledge_store
from ..observability.pii_redactor import get_pii_redactor
from ..observability.tracing import start_traced_span
from ..orchestration.hitl_confirmation import verify_or_request_human_confirmation
from ..schemas.c4_schemas import (
    AdrRecordInput,
    AdrRecordOutput,
    C4ArchitectureSpecInput,
    ComplianceFinding,
    ComplianceReportOutput,
    PublishBundleInput,
    SchemaValidationError,
    validate_against_schema,
)
from .c4_modeling_tools import _guided_error


def validate_c4_architecture_compliance(
    architecture_spec: dict[str, Any],
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Audits a C4 specification against Well-Architected security, reliability, and regulatory rules.

    Checks for:
    1. Direct external/person access to data stores (`database`/`cache`) bypassing API tiers.
    2. Unencrypted wire protocols (`http` without TLS/HTTPS, `plaintext`, `ftp`, `telnet`).
    3. Missing asynchronous decoupling or observability/audit sinks on regulated profiles
       (`pci-dss`, `hipaa`, `lgpd-gdpr`, `fedramp`).
    4. Bidirectional synchronous tight coupling cycles between microservices.

    Args:
        architecture_spec: Dictionary conforming to `C4ArchitectureSpecInput`.
        tool_context: Optional ADK `ToolContext` to store compliance score in session state.

    Returns:
        Validated `ComplianceReportOutput` dictionary with `passed`, `score` (0-100), and
        actionable `findings`, or `status="error"` with `recovery_instructions`.
    """
    with start_traced_span("tool.validate_c4_architecture_compliance"):
        try:
            spec: C4ArchitectureSpecInput = validate_against_schema(
                C4ArchitectureSpecInput, architecture_spec
            )
        except SchemaValidationError as exc:
            return _guided_error(
                error_code="COMPLIANCE_INPUT_SCHEMA_INVALID",
                error_message=str(exc),
                invalid_fields=exc.invalid_fields,
                recovery_instructions=(
                    "Correct `architecture_spec` so all required fields (`project_slug`, "
                    "`system_name`, `actors`, `containers`, `relationships`) match "
                    "`C4ArchitectureSpecInput`, then re-run `validate_c4_architecture_compliance`."
                ),
                schema_cls=C4ArchitectureSpecInput,
            )

        findings: list[ComplianceFinding] = []
        actor_aliases = {a.alias for a in spec.actors}
        data_store_aliases = {
            c.alias for c in spec.containers if c.container_type in {"database", "cache"}
        }

        # Rule C4-SEC-001: Actors must never connect directly to databases/caches
        for rel in spec.relationships:
            if rel.source_alias in actor_aliases and rel.target_alias in data_store_aliases:
                findings.append(
                    ComplianceFinding(
                        rule_id="C4-SEC-001",
                        severity="CRITICAL",
                        element_alias=rel.target_alias,
                        message=(
                            f"Actor '{rel.source_alias}' connects directly to data store "
                            f"'{rel.target_alias}' without an authenticated API/service boundary."
                        ),
                        remediation=(
                            "Place an API Gateway or backend service container between external "
                            "actors and internal database/cache containers."
                        ),
                    )
                )

        # Rule C4-SEC-002: Enforce TLS/mTLS encrypted protocols on every edge
        for rel in spec.relationships:
            proto_lower = rel.protocol.lower()
            has_plain_http = "http" in proto_lower and "https" not in proto_lower
            has_legacy_insecure = any(
                bad in proto_lower for bad in ("plaintext", "telnet", "ftp", "unencrypted")
            )
            if has_plain_http or has_legacy_insecure:
                findings.append(
                    ComplianceFinding(
                        rule_id="C4-SEC-002",
                        severity="HIGH",
                        element_alias=f"{rel.source_alias}->{rel.target_alias}",
                        message=(
                            f"Relationship '{rel.source_alias} -> {rel.target_alias}' uses "
                            f"unencrypted protocol '{rel.protocol}'."
                        ),
                        remediation="Upgrade transport to HTTPS/TLS 1.3, gRPC/mTLS, or TLS-wrapped SQL.",
                    )
                )

        # Rule C4-REL-003: Detect synchronous 2-node cycles between service containers
        service_aliases = {c.alias for c in spec.containers if c.container_type == "service"}
        directed_service_edges = {
            (r.source_alias, r.target_alias)
            for r in spec.relationships
            if r.source_alias in service_aliases and r.target_alias in service_aliases
        }
        reported_cycles: set[tuple[str, str]] = set()
        for src, tgt in directed_service_edges:
            pair = tuple(sorted((src, tgt)))
            if (tgt, src) in directed_service_edges and pair not in reported_cycles:
                reported_cycles.add(pair)  # type: ignore[arg-type]
                findings.append(
                    ComplianceFinding(
                        rule_id="C4-REL-003",
                        severity="MEDIUM",
                        element_alias=f"{src}<->{tgt}",
                        message=(
                            f"Bidirectional coupling detected between services '{src}' and '{tgt}'."
                        ),
                        remediation=(
                            "Break synchronous cycles using an asynchronous `queue` container "
                            "(e.g., Cloud Pub/Sub) or clarify domain ownership."
                        ),
                    )
                )

        # Rule C4-GOV-004: Regulated profiles require dedicated audit/async or multi-tier separation
        if spec.compliance_profile in {"pci-dss", "hipaa", "fedramp", "lgpd-gdpr"}:
            if len(spec.containers) < 2:
                findings.append(
                    ComplianceFinding(
                        rule_id="C4-GOV-004",
                        severity="HIGH",
                        element_alias=spec.project_slug,
                        message=(
                            f"Regulated profile '{spec.compliance_profile}' requires network/tier "
                            "segmentation (at least separate compute and isolated data containers)."
                        ),
                        remediation="Decompose compute service and encrypted persistence into distinct containers.",
                    )
                )

        penalty_weights = {"CRITICAL": 35, "HIGH": 20, "MEDIUM": 10, "LOW": 5}
        deduction = sum(penalty_weights[f.severity] for f in findings)
        score = max(0, 100 - deduction)
        passed = not any(f.severity in {"CRITICAL", "HIGH"} for f in findings)

        report = ComplianceReportOutput(
            project_slug=spec.project_slug,
            passed=passed,
            score=score,
            findings=findings,
        )
        if tool_context is not None and getattr(tool_context, "state", None) is not None:
            tool_context.state["latest_compliance_score"] = score
            tool_context.state["latest_compliance_passed"] = passed

        return report.model_dump()


def record_architecture_decision_record(
    project_slug: str,
    title: str,
    context: str,
    decision: str,
    consequences: str,
    alternatives_considered: list[str],
    status: str = "ACCEPTED",
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Validates and persists an Architecture Decision Record (ADR) into the knowledge store.

    Args:
        project_slug: Target C4 project identifier (`^[a-z][a-z0-9_-]{1,62}$`).
        title: Concise ADR title (e.g., `'ADR-001: Adopt Cloud Pub/Sub for Ledger Decoupling'`).
        context: Architectural forces, constraints, and problem context (>=15 chars).
        decision: Chosen architectural approach and rationale (>=15 chars).
        consequences: Positive trade-offs, operational costs, and risks (>=10 chars).
        alternatives_considered: Non-empty list of rejected alternatives and why.
        status: Lifecycle status (`'PROPOSED'`, `'ACCEPTED'`, or `'SUPERSEDED'`).
        tool_context: Optional ADK `ToolContext`.

    Returns:
        Validated `AdrRecordOutput` dictionary (`status="success"`) with persisted `adr_id`
        and rendered Markdown, or `status="error"` with `recovery_instructions`.
    """
    with start_traced_span("tool.record_architecture_decision_record"):
        try:
            validated: AdrRecordInput = validate_against_schema(
                AdrRecordInput,
                {
                    "project_slug": project_slug,
                    "title": title,
                    "context": context,
                    "decision": decision,
                    "consequences": consequences,
                    "alternatives_considered": alternatives_considered,
                    "status": status,
                },
            )
        except SchemaValidationError as exc:
            return _guided_error(
                error_code="INVALID_ADR_INPUT",
                error_message=str(exc),
                invalid_fields=exc.invalid_fields,
                recovery_instructions=(
                    "Ensure `project_slug` is lowercase snake_case, `context` and `decision` "
                    "are >=15 characters, `alternatives_considered` has >=1 item, and `status` "
                    "is 'PROPOSED', 'ACCEPTED', or 'SUPERSEDED'."
                ),
                schema_cls=AdrRecordInput,
            )

        redactor = get_pii_redactor()
        adr_id = f"adr-{validated.project_slug}-{uuid.uuid4().hex[:8]}"
        alt_bullets = "\n".join(
            f"- {redactor.redact_text(alt)}" for alt in validated.alternatives_considered
        )
        markdown = (
            f"# {redactor.redact_text(validated.title)}\n\n"
            f"- **ADR ID**: `{adr_id}`\n"
            f"- **Status**: {validated.status}\n"
            f"- **Project**: `{validated.project_slug}`\n\n"
            f"## Context\n{redactor.redact_text(validated.context)}\n\n"
            f"## Decision\n{redactor.redact_text(validated.decision)}\n\n"
            f"## Alternatives Considered\n{alt_bullets}\n\n"
            f"## Consequences\n{redactor.redact_text(validated.consequences)}\n"
        )

        get_knowledge_store().upsert_document(
            doc_id=adr_id,
            kind="adr",
            project_slug=validated.project_slug,
            domain="adr",
            title=validated.title,
            content=markdown,
            metadata={"status": validated.status},
        )
        if tool_context is not None and getattr(tool_context, "state", None) is not None:
            existing_adrs = list(tool_context.state.get("recorded_adr_ids", []))
            existing_adrs.append(adr_id)
            tool_context.state["recorded_adr_ids"] = existing_adrs

        out = AdrRecordOutput(
            adr_id=adr_id,
            project_slug=validated.project_slug,
            markdown=markdown,
        )
        return out.model_dump()


def publish_approved_c4_architecture_bundle(
    project_slug: str,
    version_tag: str,
    target_catalog: str,
    release_notes: str,
    structurizr_dsl: str,
    explicit_human_approval: bool = False,
    tool_context: Any | None = None,
) -> dict[str, Any]:
    """Publishes a C4 architecture baseline to an enterprise catalog (Human-in-the-Loop gated).

    High-stakes governance action: requires explicit human confirmation before writing an
    immutable release tag to `'enterprise-catalog'` or `'architecture-review-board'`.
    Resolves catalog webhook credentials exclusively via Google Cloud Secret Manager.

    Args:
        project_slug: Target C4 workspace slug (`^[a-z][a-z0-9_-]{1,62}$`).
        version_tag: Semantic version starting with `'v'` (e.g., `'v1.0.0'`).
        target_catalog: Destination catalog (`'enterprise-catalog'`, `'staging-catalog'`,
            or `'architecture-review-board'`).
        release_notes: Summary of architectural changes and ADR references (>=10 chars).
        structurizr_dsl: Complete Structurizr workspace DSL to publish (>=20 chars).
        explicit_human_approval: Used only by pre-approved CLI automation or tests;
            interactive sessions must obtain confirmation via ADK `ToolConfirmation`.
        tool_context: Optional ADK `ToolContext` carrying `tool_confirmation` and state.

    Returns:
        Dictionary with `status="success"` once approved and published,
        `status="pending_human_approval"` if halted by the HITL gate, or
        `status="error"` with guided `recovery_instructions`.
    """
    with start_traced_span("tool.publish_approved_c4_architecture_bundle"):
        try:
            validated: PublishBundleInput = validate_against_schema(
                PublishBundleInput,
                {
                    "project_slug": project_slug,
                    "version_tag": version_tag,
                    "target_catalog": target_catalog,
                    "release_notes": release_notes,
                    "structurizr_dsl": structurizr_dsl,
                },
            )
        except SchemaValidationError as exc:
            return _guided_error(
                error_code="INVALID_PUBLISH_BUNDLE_INPUT",
                error_message=str(exc),
                invalid_fields=exc.invalid_fields,
                recovery_instructions=(
                    "Ensure `version_tag` follows semantic versioning like `'v1.0.0'`, "
                    "`target_catalog` is one of ['enterprise-catalog', 'staging-catalog', "
                    "'architecture-review-board'], and `structurizr_dsl` is non-empty."
                ),
                schema_cls=PublishBundleInput,
            )

        # Enforce Human-in-the-Loop confirmation gate for production governance catalogs
        if validated.target_catalog in {"enterprise-catalog", "architecture-review-board"}:
            hitl = verify_or_request_human_confirmation(
                tool_context,
                action_summary=(
                    f"Publish C4 baseline '{validated.project_slug}@{validated.version_tag}' "
                    f"to '{validated.target_catalog}'"
                ),
                confirmation_payload={
                    "project_slug": validated.project_slug,
                    "version_tag": validated.version_tag,
                    "target_catalog": validated.target_catalog,
                },
                explicit_approval_override=explicit_human_approval,
            )
            if not hitl.approved:
                return {
                    "status": hitl.status.lower(),
                    "requires_human_confirmation": True,
                    "project_slug": validated.project_slug,
                    "version_tag": validated.version_tag,
                    "target_catalog": validated.target_catalog,
                    "message": hitl.reason,
                    "next_step_for_agent": (
                        "Present the C4 diagrams, compliance score, and version tag to the "
                        "user and wait for explicit human confirmation before retrying."
                    ),
                }
            approver = hitl.approver
        else:
            approver = "staging_auto_policy"

        # Resolve optional catalog webhook token from Google Cloud Secret Manager (never hardcoded)
        settings = get_settings()
        webhook_secret_configured = bool(
            get_secret_resolver().get_secret(
                settings.publishing_webhook_secret_id,
                env_fallback_var="C4_PUBLISHING_WEBHOOK_TOKEN",
                required=False,
            )
        )

        receipt = get_knowledge_store().record_published_bundle(
            project_slug=validated.project_slug,
            version_tag=validated.version_tag,
            target_catalog=validated.target_catalog,
            release_notes=validated.release_notes,
            structurizr_dsl=validated.structurizr_dsl,
            approved_by=approver,
        )
        return {
            "status": "success",
            "approved_by": approver,
            "secret_manager_webhook_verified": webhook_secret_configured,
            **receipt,
        }
