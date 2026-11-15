"""Typed runtime settings loaded from environment and Secret Manager."""

from __future__ import annotations

from dataclasses import dataclass
import functools
import os


@dataclass(frozen=True)
class AgentSettings:
    """Immutable runtime settings for the C4 Model Architecture Creator Agent."""

    app_name: str = "c4_architect_agent"
    service_name: str = "c4-architect-agent"
    environment: str = "development"
    gcp_project_id: str = ""
    gcp_location: str = "us-central1"
    use_vertex_ai: bool = True

    # Strategic model routing tiers
    fast_model: str = "gemini-2.5-flash"
    reasoning_model: str = "gemini-2.5-pro"

    # Compaction & caching budgets
    compaction_interval: int = 5
    compaction_overlap_size: int = 1
    compaction_token_threshold: int = 12000
    compaction_event_retention_size: int = 6
    context_cache_ttl_seconds: int = 1800
    context_cache_min_tokens: int = 2048

    # Persistent storage
    session_db_url: str = "sqlite+aiosqlite:///./data/c4_sessions.db"
    knowledge_db_path: str = "./data/c4_knowledge_store.sqlite3"
    vertex_search_datastore_id: str = ""

    # Secret Manager secret identifiers (names only, never raw secret values)
    publishing_webhook_secret_id: str = "c4-architect-publishing-webhook-token"
    db_conn_secret_id: str = "c4-architect-session-db-uri"
    model_armor_template_secret_id: str = "c4-architect-model-armor-template"

    # Telemetry & DLP flags
    enable_cloud_trace: bool = False
    enable_cloud_dlp: bool = False
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> AgentSettings:
        """Constructs validated settings from environment variables."""
        return cls(
            app_name=os.getenv("C4_APP_NAME", "c4_architect_agent"),
            service_name=os.getenv("C4_SERVICE_NAME", "c4-architect-agent"),
            environment=os.getenv("C4_ENVIRONMENT", "development"),
            gcp_project_id=os.getenv("GOOGLE_CLOUD_PROJECT", ""),
            gcp_location=os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1"),
            use_vertex_ai=os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "TRUE").upper() == "TRUE",
            fast_model=os.getenv("C4_FAST_MODEL", "gemini-2.5-flash"),
            reasoning_model=os.getenv("C4_REASONING_MODEL", "gemini-2.5-pro"),
            compaction_interval=int(os.getenv("C4_COMPACTION_INTERVAL", "5")),
            compaction_overlap_size=int(os.getenv("C4_COMPACTION_OVERLAP", "1")),
            compaction_token_threshold=int(os.getenv("C4_COMPACTION_TOKEN_THRESHOLD", "12000")),
            compaction_event_retention_size=int(os.getenv("C4_EVENT_RETENTION_SIZE", "6")),
            context_cache_ttl_seconds=int(os.getenv("C4_CACHE_TTL_SECONDS", "1800")),
            context_cache_min_tokens=int(os.getenv("C4_CACHE_MIN_TOKENS", "2048")),
            session_db_url=os.getenv(
                "C4_SESSION_DB_URL", "sqlite+aiosqlite:///./data/c4_sessions.db"
            ),
            knowledge_db_path=os.getenv(
                "C4_KNOWLEDGE_DB_PATH", "./data/c4_knowledge_store.sqlite3"
            ),
            vertex_search_datastore_id=os.getenv("C4_VERTEX_SEARCH_DATASTORE_ID", ""),
            publishing_webhook_secret_id=os.getenv(
                "C4_PUBLISHING_WEBHOOK_SECRET_ID", "c4-architect-publishing-webhook-token"
            ),
            db_conn_secret_id=os.getenv(
                "C4_DB_CONN_SECRET_ID", "c4-architect-session-db-uri"
            ),
            model_armor_template_secret_id=os.getenv(
                "C4_MODEL_ARMOR_TEMPLATE_SECRET_ID", "c4-architect-model-armor-template"
            ),
            enable_cloud_trace=os.getenv("C4_ENABLE_CLOUD_TRACE", "FALSE").upper() == "TRUE",
            enable_cloud_dlp=os.getenv("C4_ENABLE_CLOUD_DLP", "FALSE").upper() == "TRUE",
            log_level=os.getenv("C4_LOG_LEVEL", "INFO").upper(),
        )


@functools.lru_cache(maxsize=1)
def get_settings() -> AgentSettings:
    """Returns cached singleton runtime settings."""
    return AgentSettings.from_env()
