"""Strict JSON Schemas (Pydantic v2) for C4 Model entities, tool I/O, and error envelopes.

Every tool input and output is constrained by explicit JSON Schemas with
`extra="forbid"`, pattern constraints, and field descriptions so LLMs receive
deterministic validation rules and structured recovery guidance on invalid calls.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Literal

try:
    from pydantic import BaseModel, ConfigDict, Field, ValidationError

    PYDANTIC_AVAILABLE = True
except ImportError:  # pragma: no cover - stdlib fallback for bare container test runners
    PYDANTIC_AVAILABLE = False
    ValidationError = ValueError  # type: ignore[misc,assignment]

    def Field(default: Any = ..., **kwargs: Any) -> Any:  # type: ignore[misc]
        return _FieldSpec(default=default, metadata=kwargs)

    def ConfigDict(**kwargs: Any) -> dict[str, Any]:  # type: ignore[misc]
        return dict(kwargs)


@dataclass
class _FieldSpec:
    default: Any
    metadata: dict[str, Any]


class SchemaValidationError(ValueError):
    """Raised when tool arguments violate an explicit JSON Schema contract."""

    def __init__(self, message: str, invalid_fields: list[dict[str, str]]) -> None:
        super().__init__(message)
        self.invalid_fields = invalid_fields


if not PYDANTIC_AVAILABLE:  # pragma: no cover - lightweight validator mirroring Pydantic v2
    class BaseModel:  # type: ignore[no-redef]
        model_config: dict[str, Any] = {"extra": "forbid"}

        def __init__(self, **data: Any) -> None:
            annotations = self._collect_annotations()
            allowed_keys = set(annotations.keys())
            if self.model_config.get("extra") == "forbid":
                unknown = sorted(set(data.keys()) - allowed_keys)
                if unknown:
                    raise SchemaValidationError(
                        f"Unexpected extra fields: {', '.join(unknown)}",
                        [{"field": k, "reason": "Extra inputs are not permitted"} for k in unknown],
                    )

            errors: list[dict[str, str]] = []
            for field_name, field_type in annotations.items():
                raw_attr = getattr(self.__class__, field_name, _FieldSpec(..., {}))
                spec = raw_attr if isinstance(raw_attr, _FieldSpec) else _FieldSpec(raw_attr, {})
                if field_name in data:
                    val = data[field_name]
                elif spec.default is not ...:
                    default_factory = spec.metadata.get("default_factory")
                    val = default_factory() if callable(default_factory) else spec.default
                elif "default_factory" in spec.metadata:
                    val = spec.metadata["default_factory"]()
                else:
                    errors.append({"field": field_name, "reason": "Field required"})
                    continue

                coerced, field_errors = self._validate_field(field_name, val, field_type, spec)
                if field_errors:
                    errors.extend(field_errors)
                else:
                    setattr(self, field_name, coerced)

            if errors:
                summary = "; ".join(f"{e['field']}: {e['reason']}" for e in errors)
                raise SchemaValidationError(f"Schema validation failed ({summary})", errors)

        @classmethod
        def _collect_annotations(cls) -> dict[str, Any]:
            merged: dict[str, Any] = {}
            for base in reversed(cls.__mro__):
                for k, v in getattr(base, "__annotations__", {}).items():
                    if k != "model_config" and not k.startswith("_"):
                        merged[k] = v
            return merged

        @classmethod
        def _validate_field(
            cls,
            name: str,
            val: Any,
            annotation: Any,
            spec: _FieldSpec,
        ) -> tuple[Any, list[dict[str, str]]]:
            meta = spec.metadata
            anno_str = str(annotation)

            if val is None and ("None" in anno_str or "Optional" in anno_str):
                return None, []

            if anno_str.startswith("list[") and isinstance(val, list):
                min_len = meta.get("min_length")
                if min_len is not None and len(val) < min_len:
                    return val, [{"field": name, "reason": f"List must contain at least {min_len} item(s)"}]
                inner_name = anno_str[5:-1].strip()
                sub_cls = globals().get(inner_name)
                if isinstance(sub_cls, type) and issubclass(sub_cls, BaseModel):
                    converted = []
                    sub_errors: list[dict[str, str]] = []
                    for idx, item in enumerate(val):
                        try:
                            converted.append(
                                item if isinstance(item, sub_cls) else sub_cls.model_validate(item)
                            )
                        except SchemaValidationError as exc:
                            for e in exc.invalid_fields:
                                sub_errors.append(
                                    {"field": f"{name}[{idx}].{e['field']}", "reason": e["reason"]}
                                )
                    return converted, sub_errors
                return val, []

            if "Literal[" in anno_str:
                allowed = [
                    tok.strip().strip("'\"")
                    for tok in anno_str.split("Literal[", 1)[1].rstrip("]").split(",")
                ]
                if str(val) not in allowed:
                    return val, [
                        {"field": name, "reason": f"Input should be one of {allowed}, got '{val}'"}
                    ]
                return val, []

            if anno_str in ("str", "<class 'str'>"):
                if not isinstance(val, str):
                    return val, [{"field": name, "reason": "Input should be a valid string"}]
                min_len = meta.get("min_length")
                max_len = meta.get("max_length")
                pattern = meta.get("pattern")
                if min_len is not None and len(val.strip()) < min_len:
                    return val, [
                        {"field": name, "reason": f"String should have at least {min_len} characters"}
                    ]
                if max_len is not None and len(val) > max_len:
                    return val, [
                        {"field": name, "reason": f"String should have at most {max_len} characters"}
                    ]
                if pattern is not None and not re.match(pattern, val):
                    return val, [
                        {"field": name, "reason": f"String should match pattern '{pattern}'"}
                    ]
                return val, []

            if anno_str in ("int", "<class 'int'>"):
                if isinstance(val, bool) or not isinstance(val, int):
                    return val, [{"field": name, "reason": "Input should be a valid integer"}]
                ge = meta.get("ge")
                le = meta.get("le")
                if ge is not None and val < ge:
                    return val, [{"field": name, "reason": f"Input should be >= {ge}"}]
                if le is not None and val > le:
                    return val, [{"field": name, "reason": f"Input should be <= {le}"}]
                return val, []

            if anno_str in ("bool", "<class 'bool'>"):
                if not isinstance(val, bool):
                    return val, [{"field": name, "reason": "Input should be a valid boolean"}]
                return val, []

            return val, []

        @classmethod
        def model_validate(cls, obj: Any) -> Any:
            if isinstance(obj, cls):
                return obj
            if not isinstance(obj, dict):
                raise SchemaValidationError(
                    "Input payload must be a JSON object",
                    [{"field": "$root", "reason": "Expected JSON object"}],
                )
            return cls(**obj)

        @classmethod
        def model_json_schema(cls) -> dict[str, Any]:
            annotations = cls._collect_annotations()
            properties: dict[str, Any] = {}
            required: list[str] = []
            for field_name, field_type in annotations.items():
                raw_attr = getattr(cls, field_name, _FieldSpec(..., {}))
                spec = raw_attr if isinstance(raw_attr, _FieldSpec) else _FieldSpec(raw_attr, {})
                prop: dict[str, Any] = {"title": field_name, "type": str(field_type)}
                if "description" in spec.metadata:
                    prop["description"] = spec.metadata["description"]
                if "pattern" in spec.metadata:
                    prop["pattern"] = spec.metadata["pattern"]
                properties[field_name] = prop
                if spec.default is ... and "default_factory" not in spec.metadata:
                    required.append(field_name)
            return {
                "title": cls.__name__,
                "type": "object",
                "additionalProperties": False,
                "properties": properties,
                "required": required,
            }

        def model_dump(self) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key in self._collect_annotations():
                val = getattr(self, key, None)
                if isinstance(val, BaseModel):
                    result[key] = val.model_dump()
                elif isinstance(val, list):
                    result[key] = [
                        item.model_dump() if isinstance(item, BaseModel) else item for item in val
                    ]
                else:
                    result[key] = val
            return result


SLUG_PATTERN = r"^[a-z][a-z0-9_-]{1,62}$"


class C4PersonSpec(BaseModel):
    """C4 Level 1 Person/Actor interacting with the software system."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(
        ...,
        pattern=SLUG_PATTERN,
        description="Unique snake_case identifier for the actor (e.g., 'enterprise_architect').",
    )
    label: str = Field(..., min_length=2, max_length=80, description="Human-readable actor title.")
    description: str = Field(
        ..., min_length=5, max_length=300, description="Primary responsibility and goals."
    )
    external: bool = Field(
        default=False, description="True when the actor is outside the organization boundary."
    )


class C4ExternalSystemSpec(BaseModel):
    """C4 Level 1 External Software System dependency."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(
        ..., pattern=SLUG_PATTERN, description="Unique snake_case identifier for external system."
    )
    label: str = Field(..., min_length=2, max_length=80, description="Display name of system.")
    technology: str = Field(
        ..., min_length=2, max_length=80, description="Integration technology (e.g., 'REST/OAuth2')."
    )
    description: str = Field(
        ..., min_length=5, max_length=300, description="Role of the external system."
    )


class C4ComponentSpec(BaseModel):
    """C4 Level 3 Component residing inside a Container."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., pattern=SLUG_PATTERN, description="Unique component identifier.")
    label: str = Field(..., min_length=2, max_length=80, description="Component display name.")
    technology: str = Field(
        ..., min_length=2, max_length=80, description="Implementation framework/module type."
    )
    description: str = Field(
        ..., min_length=5, max_length=300, description="Bounded responsibility of this component."
    )


class C4ContainerSpec(BaseModel):
    """C4 Level 2 Container (deployable runtime unit or data store)."""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., pattern=SLUG_PATTERN, description="Unique container identifier.")
    label: str = Field(..., min_length=2, max_length=80, description="Container display name.")
    container_type: Literal["service", "webapp", "mobile", "database", "queue", "cache"] = Field(
        ..., description="Deployable archetype determining C4 shape (ContainerDb, ContainerQueue, etc.)."
    )
    technology: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Runtime stack (e.g., 'Python 3.12 / FastAPI on Cloud Run', 'Cloud SQL PostgreSQL 16').",
    )
    description: str = Field(
        ..., min_length=5, max_length=300, description="Core responsibility and data boundary."
    )
    components: list[C4ComponentSpec] = Field(
        default_factory=list,
        description="Optional C4 Level 3 components decomposed inside this container.",
    )


class C4CodeElementSpec(BaseModel):
    """C4 Level 4 Code-level interface, class, or domain aggregate."""

    model_config = ConfigDict(extra="forbid")

    component_alias: str = Field(
        ..., pattern=SLUG_PATTERN, description="Parent C4 Level 3 component alias."
    )
    class_name: str = Field(
        ..., min_length=2, max_length=80, description="Class, protocol, or interface identifier."
    )
    stereotype: Literal["interface", "service", "repository", "entity", "adapter"] = Field(
        default="service", description="UML/C4 Level 4 stereotype."
    )
    key_methods: list[str] = Field(
        default_factory=list, description="Public method signatures exposed by this type."
    )


class C4RelationshipSpec(BaseModel):
    """Directed relationship between two C4 elements with explicit protocol."""

    model_config = ConfigDict(extra="forbid")

    source_alias: str = Field(..., pattern=SLUG_PATTERN, description="Caller/origin alias.")
    target_alias: str = Field(..., pattern=SLUG_PATTERN, description="Callee/destination alias.")
    label: str = Field(
        ...,
        min_length=3,
        max_length=120,
        description="Action-verb description (e.g., 'Publishes domain events to').",
    )
    protocol: str = Field(
        ...,
        min_length=2,
        max_length=80,
        description="Explicit wire protocol and auth mechanism (e.g., 'gRPC / mTLS', 'HTTPS / OIDC').",
    )


class C4ArchitectureSpecInput(BaseModel):
    """Validated input schema for generating complete L1-L4 C4 Model diagrams."""

    model_config = ConfigDict(extra="forbid")

    project_slug: str = Field(
        ...,
        pattern=SLUG_PATTERN,
        description="Unique snake_case or kebab-case identifier for the architecture workspace.",
    )
    system_name: str = Field(
        ..., min_length=3, max_length=100, description="Target software system name."
    )
    system_description: str = Field(
        ..., min_length=10, max_length=500, description="High-level business purpose of the system."
    )
    compliance_profile: Literal["standard", "pci-dss", "hipaa", "lgpd-gdpr", "fedramp"] = Field(
        default="standard",
        description="Regulatory compliance baseline applied during architectural validation.",
    )
    actors: list[C4PersonSpec] = Field(
        ..., min_length=1, description="At least one C4 Level 1 Person/Actor."
    )
    containers: list[C4ContainerSpec] = Field(
        ..., min_length=1, description="At least one C4 Level 2 Container."
    )
    external_systems: list[C4ExternalSystemSpec] = Field(
        default_factory=list, description="External upstream/downstream systems."
    )
    relationships: list[C4RelationshipSpec] = Field(
        ..., min_length=1, description="Directed relationships with explicit protocols."
    )
    code_elements: list[C4CodeElementSpec] = Field(
        default_factory=list, description="Optional C4 Level 4 class/interface specifications."
    )


class C4DiagramBundleOutput(BaseModel):
    """Structured output schema containing rendered C4 L1-L4 Mermaid & Structurizr DSL."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    project_slug: str
    system_name: str
    c4_context_mermaid: str = Field(..., description="C4Context (Level 1) Mermaid diagram.")
    c4_container_mermaid: str = Field(..., description="C4Container (Level 2) Mermaid diagram.")
    c4_component_mermaid: str = Field(..., description="C4Component (Level 3) Mermaid diagram.")
    c4_code_mermaid: str = Field(..., description="classDiagram (Level 4) Mermaid diagram.")
    structurizr_dsl: str = Field(..., description="Complete Structurizr workspace DSL.")
    element_counts: dict[str, int]


class ComplianceFinding(BaseModel):
    """Single architectural governance or security finding."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    severity: Literal["CRITICAL", "HIGH", "MEDIUM", "LOW"]
    element_alias: str
    message: str
    remediation: str


class ComplianceCheckInput(BaseModel):
    """Input schema for validating C4 architecture against Well-Architected & security rules."""

    model_config = ConfigDict(extra="forbid")

    architecture: C4ArchitectureSpecInput


class ComplianceReportOutput(BaseModel):
    """Output schema for C4 architecture compliance validation."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    project_slug: str
    passed: bool
    score: int = Field(..., ge=0, le=100)
    findings: list[ComplianceFinding]


class PatternSearchInput(BaseModel):
    """Input schema for querying reference C4 architecture patterns & ADRs."""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(
        ...,
        min_length=3,
        max_length=300,
        description="Natural-language architectural query (e.g., 'event-driven CQRS payment gateway').",
    )
    domain_filter: str = Field(
        default="",
        max_length=60,
        description="Optional domain tag filter (e.g., 'fintech', 'healthcare', 'data-platform').",
    )
    top_k: int = Field(default=3, ge=1, le=10, description="Maximum reference patterns to return.")


class AdrRecordInput(BaseModel):
    """Input schema for recording an Architecture Decision Record (ADR)."""

    model_config = ConfigDict(extra="forbid")

    project_slug: str = Field(..., pattern=SLUG_PATTERN)
    title: str = Field(..., min_length=5, max_length=140)
    context: str = Field(..., min_length=15, max_length=2000)
    decision: str = Field(..., min_length=15, max_length=2000)
    consequences: str = Field(..., min_length=10, max_length=2000)
    alternatives_considered: list[str] = Field(..., min_length=1)
    status: Literal["PROPOSED", "ACCEPTED", "SUPERSEDED"] = "ACCEPTED"


class AdrRecordOutput(BaseModel):
    """Output schema returned when an ADR is persisted."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["success"] = "success"
    adr_id: str
    project_slug: str
    markdown: str


class PublishBundleInput(BaseModel):
    """High-stakes input schema for publishing an official C4 architecture baseline."""

    model_config = ConfigDict(extra="forbid")

    project_slug: str = Field(..., pattern=SLUG_PATTERN)
    version_tag: str = Field(
        ...,
        pattern=r"^v\d+\.\d+\.\d+$",
        description="Semantic version tag starting with 'v' (e.g., 'v1.0.0').",
    )
    target_catalog: Literal["enterprise-catalog", "staging-catalog", "architecture-review-board"] = (
        Field(..., description="Destination governance catalog.")
    )
    release_notes: str = Field(..., min_length=10, max_length=1000)
    structurizr_dsl: str = Field(..., min_length=20)


class GuidedToolError(BaseModel):
    """Guided error response instructing the LLM how to self-recover without crashing."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["error"] = "error"
    error_code: str
    error_message: str
    invalid_fields: list[dict[str, str]] = Field(default_factory=list)
    recovery_instructions: str
    expected_schema_summary: dict[str, Any] = Field(default_factory=dict)


def validate_against_schema(model_cls: type[Any], payload: dict[str, Any]) -> Any:
    """Validates `payload` against `model_cls` and normalizes errors into SchemaValidationError."""
    try:
        return model_cls.model_validate(payload)
    except SchemaValidationError:
        raise
    except Exception as exc:
        if PYDANTIC_AVAILABLE and isinstance(exc, ValidationError):
            fields = [
                {
                    "field": ".".join(str(loc) for loc in err.get("loc", ())),
                    "reason": str(err.get("msg", "Invalid value")),
                }
                for err in exc.errors()
            ]
            raise SchemaValidationError(str(exc), fields) from exc
        raise SchemaValidationError(str(exc), [{"field": "$payload", "reason": str(exc)}]) from exc
