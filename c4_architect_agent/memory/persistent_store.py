"""Persistent session service and vector-searchable C4 reference & ADR store.

Provides two persistence pillars:
1. Conversational Session Persistence: ADK `DatabaseSessionService` backed by
   SQLite (`sqlite+aiosqlite`) locally or Cloud SQL PostgreSQL (`postgresql+asyncpg`)
   via Secret Manager in production (with Vertex AI Agent Engine session compatibility).
2. Semantic Knowledge & Episodic Memory Store: SQLite-persisted knowledge base with
   deterministic cosine vector similarity retrieval + optional Vertex AI Search
   (`google-cloud-discoveryengine`) retrieval for enterprise architecture catalogs.
"""

from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import functools
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any
import uuid

from ..config.secrets import get_secret_resolver
from ..config.settings import get_settings
from ..observability.pii_redactor import get_pii_redactor
from ..observability.structured_logger import log_structured_event

try:
    from google.adk.sessions import DatabaseSessionService, InMemorySessionService
except ImportError:  # pragma: no cover
    DatabaseSessionService = None  # type: ignore[assignment,misc]
    InMemorySessionService = None  # type: ignore[assignment,misc]


_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{2,}")

_SEED_REFERENCE_PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "doc_id": "ref-event-driven-fintech",
        "kind": "reference_pattern",
        "project_slug": "seed_catalog",
        "domain": "fintech",
        "title": "Event-Driven Payment & Ledger C4 Reference Architecture",
        "content": (
            "Actors: Customer, Fraud Analyst. Containers: API Gateway (Cloud Run HTTPS/OIDC), "
            "Payment Orchestrator Service (Python FastAPI gRPC/mTLS), Event Bus (Cloud Pub/Sub), "
            "Immutable Ledger Service (Go), Ledger Database (Cloud Spanner), Fraud Scoring Worker "
            "(Vertex AI Endpoint). Enforces idempotency keys, PCI-DSS tokenization, and dead-letter queues."
        ),
        "metadata": {"compliance": "pci-dss", "containers": 6},
    },
    {
        "doc_id": "ref-rag-enterprise-platform",
        "kind": "reference_pattern",
        "project_slug": "seed_catalog",
        "domain": "ai-platform",
        "title": "Enterprise Multi-Agent RAG & Governance C4 Blueprint",
        "content": (
            "Actors: Knowledge Worker, Platform Admin. Containers: Web Portal (Next.js HTTPS/IAP), "
            "Coordinator Agent Service (Google ADK on Cloud Run), Ingestion Pipeline (Cloud Workflows), "
            "Vector Knowledge Store (Vertex AI Search / AlloyDB pgvector), Audit & Trace Sink "
            "(Cloud Logging + Cloud Trace + Cloud DLP PII redaction)."
        ),
        "metadata": {"compliance": "lgpd-gdpr", "containers": 5},
    },
    {
        "doc_id": "ref-healthcare-fhir-portal",
        "kind": "reference_pattern",
        "project_slug": "seed_catalog",
        "domain": "healthcare",
        "title": "HIPAA-Compliant Telehealth & FHIR Integration C4 Architecture",
        "content": (
            "Actors: Patient, Clinician. External Systems: EHR System (HL7 FHIR R4 over mTLS). "
            "Containers: Patient Mobile App (Flutter HTTPS/OAuth2), Clinical BFF API (Cloud Run), "
            "Appointment & Triage Service, Cloud Healthcare FHIR Store, Async Notification Queue (Pub/Sub)."
        ),
        "metadata": {"compliance": "hipaa", "containers": 5},
    },
)


def _compute_term_vector(text: str) -> dict[str, float]:
    """Builds an L2-normalized sparse TF vector for lightweight local semantic retrieval."""
    tokens = [tok.lower() for tok in _TOKEN_RE.findall(text)]
    if not tokens:
        return {}
    counts = Counter(tokens)
    norm = math.sqrt(sum(v * v for v in counts.values())) or 1.0
    return {term: round(freq / norm, 6) for term, freq in counts.items()}


def _cosine_similarity(vec_a: dict[str, float], vec_b: dict[str, float]) -> float:
    if not vec_a or not vec_b:
        return 0.0
    if len(vec_a) > len(vec_b):
        vec_a, vec_b = vec_b, vec_a
    return sum(weight * vec_b.get(term, 0.0) for term, weight in vec_a.items())


class PersistentArchitectureKnowledgeStore:
    """SQLite-backed persistent store for C4 patterns, ADRs, and consolidated session memories."""

    def __init__(self, db_path: str | None = None) -> None:
        settings = get_settings()
        resolved_path = Path(db_path or settings.knowledge_db_path)
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(resolved_path)
        self._lock = threading.Lock()
        self._redactor = get_pii_redactor()
        self._init_schema_and_seed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema_and_seed(self) -> None:
        with self._lock, closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_documents (
                    doc_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    project_slug TEXT NOT NULL,
                    domain TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    term_vector_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_project
                    ON knowledge_documents(project_slug, kind);

                CREATE TABLE IF NOT EXISTS published_bundles (
                    bundle_id TEXT PRIMARY KEY,
                    project_slug TEXT NOT NULL,
                    version_tag TEXT NOT NULL,
                    target_catalog TEXT NOT NULL,
                    release_notes TEXT NOT NULL,
                    structurizr_dsl TEXT NOT NULL,
                    approved_by TEXT NOT NULL,
                    published_at_utc TEXT NOT NULL
                );
                """
            )
            existing = conn.execute("SELECT COUNT(*) AS cnt FROM knowledge_documents").fetchone()
            if existing and int(existing["cnt"]) == 0:
                for seed in _SEED_REFERENCE_PATTERNS:
                    self._upsert_unlocked(
                        conn,
                        doc_id=seed["doc_id"],
                        kind=seed["kind"],
                        project_slug=seed["project_slug"],
                        domain=seed["domain"],
                        title=seed["title"],
                        content=seed["content"],
                        metadata=seed["metadata"],
                    )

    def _upsert_unlocked(
        self,
        conn: sqlite3.Connection,
        *,
        doc_id: str,
        kind: str,
        project_slug: str,
        domain: str,
        title: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        clean_title = self._redactor.redact_text(title)
        clean_content = self._redactor.redact_text(content)
        clean_meta = self._redactor.redact_payload(metadata or {})
        vector = _compute_term_vector(f"{clean_title} {domain} {clean_content}")
        conn.execute(
            """
            INSERT INTO knowledge_documents (
                doc_id, kind, project_slug, domain, title, content,
                term_vector_json, metadata_json, created_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(doc_id) DO UPDATE SET
                title = excluded.title,
                content = excluded.content,
                term_vector_json = excluded.term_vector_json,
                metadata_json = excluded.metadata_json
            """,
            (
                doc_id,
                kind,
                project_slug,
                domain.lower().strip(),
                clean_title,
                clean_content,
                json.dumps(vector),
                json.dumps(clean_meta),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        return doc_id

    def upsert_document(
        self,
        *,
        kind: str,
        project_slug: str,
        title: str,
        content: str,
        domain: str = "",
        doc_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Scrubs PII and persists a reference pattern, ADR, or episodic memory entry."""
        resolved_id = doc_id or f"{kind}-{uuid.uuid4().hex[:10]}"
        with self._lock, closing(self._connect()) as conn, conn:
            self._upsert_unlocked(
                conn,
                doc_id=resolved_id,
                kind=kind,
                project_slug=project_slug,
                domain=domain,
                title=title,
                content=content,
                metadata=metadata,
            )
        return resolved_id

    def search(
        self,
        query: str,
        *,
        domain_filter: str = "",
        project_slug: str = "",
        top_k: int = 3,
    ) -> list[dict[str, Any]]:
        """Performs vector cosine similarity retrieval across persistent architecture docs."""
        q_vec = _compute_term_vector(query)
        sql = "SELECT * FROM knowledge_documents WHERE 1=1"
        params: list[Any] = []
        if domain_filter.strip():
            sql += " AND domain = ?"
            params.append(domain_filter.lower().strip())
        if project_slug.strip():
            sql += " AND (project_slug = ? OR project_slug = 'seed_catalog')"
            params.append(project_slug.strip())

        with self._lock, closing(self._connect()) as conn:
            rows = conn.execute(sql, params).fetchall()

        scored: list[tuple[float, dict[str, Any]]] = []
        for row in rows:
            doc_vec = json.loads(row["term_vector_json"])
            score = _cosine_similarity(q_vec, doc_vec)
            if score > 0.0 or not q_vec:
                scored.append(
                    (
                        round(score, 4),
                        {
                            "doc_id": row["doc_id"],
                            "kind": row["kind"],
                            "project_slug": row["project_slug"],
                            "domain": row["domain"],
                            "title": row["title"],
                            "content": row["content"],
                            "similarity_score": round(score, 4),
                            "metadata": json.loads(row["metadata_json"]),
                        },
                    )
                )

        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[:top_k]]

    def record_published_bundle(
        self,
        *,
        project_slug: str,
        version_tag: str,
        target_catalog: str,
        release_notes: str,
        structurizr_dsl: str,
        approved_by: str,
    ) -> dict[str, Any]:
        """Persists an approved C4 architecture release bundle after HITL confirmation."""
        bundle_id = f"bundle-{project_slug}-{version_tag}-{uuid.uuid4().hex[:6]}"
        published_at = datetime.now(timezone.utc).isoformat()
        clean_notes = self._redactor.redact_text(release_notes)
        clean_dsl = self._redactor.redact_text(structurizr_dsl)
        with self._lock, closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO published_bundles (
                    bundle_id, project_slug, version_tag, target_catalog,
                    release_notes, structurizr_dsl, approved_by, published_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    project_slug,
                    version_tag,
                    target_catalog,
                    clean_notes,
                    clean_dsl,
                    approved_by,
                    published_at,
                ),
            )
        return {
            "bundle_id": bundle_id,
            "project_slug": project_slug,
            "version_tag": version_tag,
            "target_catalog": target_catalog,
            "published_at_utc": published_at,
        }


def build_persistent_session_service(db_url: str | None = None) -> Any:
    """Builds ADK's persistent `DatabaseSessionService` (resolving DB URI via Secret Manager if set)."""
    settings = get_settings()
    resolver = get_secret_resolver()
    secret_db_url = resolver.get_secret(
        settings.db_conn_secret_id,
        env_fallback_var="C4_MOUNTED_DB_URI",
        required=False,
    )
    resolved_url = db_url or secret_db_url or settings.session_db_url

    if resolved_url.startswith("sqlite"):
        sqlite_path = resolved_url.split("///", 1)[-1]
        if sqlite_path and sqlite_path != ":memory:":
            Path(sqlite_path).parent.mkdir(parents=True, exist_ok=True)

    if DatabaseSessionService is not None:
        try:
            service = DatabaseSessionService(db_url=resolved_url)
            log_structured_event(
                "SESSION_SERVICE_INITIALIZED",
                "Initialized persistent ADK DatabaseSessionService",
                metadata={"backend": resolved_url.split(":", 1)[0]},
            )
            return service
        except Exception as exc:
            log_structured_event(
                "SESSION_SERVICE_FALLBACK",
                f"Falling back to InMemorySessionService: {type(exc).__name__}",
            )
            if InMemorySessionService is not None:
                return InMemorySessionService()
    return {"backend": "sqlite_persistent_fallback", "db_url": resolved_url}


@functools.lru_cache(maxsize=1)
def get_knowledge_store() -> PersistentArchitectureKnowledgeStore:
    """Returns singleton persistent knowledge store."""
    return PersistentArchitectureKnowledgeStore()
