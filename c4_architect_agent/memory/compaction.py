"""Conversation history compaction and Google Cloud context caching management.

Combines native ADK `EventsCompactionConfig` + `ContextCacheConfig` with a
deterministic token-budget sliding window & architectural-fact-preserving
compactor (`HistoryCompactionEngine`) to prevent context bloat in multi-turn
architecture design sessions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config.settings import get_settings
from ..observability.pii_redactor import get_pii_redactor
from ..observability.structured_logger import log_structured_event

try:
    from google.adk.agents.context_cache_config import ContextCacheConfig
    from google.adk.apps.app import EventsCompactionConfig
except ImportError:  # pragma: no cover
    ContextCacheConfig = None  # type: ignore[assignment,misc]
    EventsCompactionConfig = None  # type: ignore[assignment,misc]


def estimate_token_count(text: str) -> int:
    """Fast deterministic token approximation (~4 chars per token + word boundary floor)."""
    if not text:
        return 0
    char_est = (len(text) + 3) // 4
    word_est = len(text.split())
    return max(char_est, word_est)


@dataclass
class CompactionResult:
    """Result of compacting a multi-turn conversation history."""

    compacted_triggered: bool
    original_turn_count: int
    retained_turn_count: int
    tokens_before: int
    tokens_after: int
    summary_digest: str
    retained_messages: list[dict[str, str]]


class HistoryCompactionEngine:
    """Manages context bloat via token thresholds, sliding windows, and summarization."""

    def __init__(
        self,
        *,
        token_budget: int = 4000,
        retain_recent_turns: int = 4,
        overlap_turns: int = 1,
    ) -> None:
        if token_budget <= 0:
            raise ValueError("token_budget must be > 0")
        if retain_recent_turns < 1:
            raise ValueError("retain_recent_turns must be >= 1")
        self.token_budget = token_budget
        self.retain_recent_turns = retain_recent_turns
        self.overlap_turns = max(0, overlap_turns)
        self._redactor = get_pii_redactor()

    def summarize_evicted_turns(self, evicted_turns: list[dict[str, str]]) -> str:
        """Extracts durable architectural decisions, containers, and constraints from older turns."""
        key_lines: list[str] = []
        architectural_markers = (
            "system:",
            "container",
            "component",
            "actor",
            "adr",
            "protocol",
            "compliance",
            "database",
            "queue",
            "decision",
            "c4",
        )
        for idx, turn in enumerate(evicted_turns, start=1):
            role = turn.get("role", "user")
            raw_content = self._redactor.redact_text(turn.get("content", "")).strip()
            if not raw_content:
                continue
            first_sentence = raw_content.splitlines()[0][:180]
            lowered = raw_content.lower()
            if any(marker in lowered for marker in architectural_markers):
                key_lines.append(f"- [Turn {idx} / {role}] {first_sentence}")
            elif len(key_lines) < 3:
                key_lines.append(f"- [Turn {idx} / {role}] {first_sentence[:110]}")

        if not key_lines:
            return "Compacted earlier conversational turns (no durable architectural deltas)."
        return "Compacted Architectural Context Summary:\n" + "\n".join(key_lines[:12])

    def compact_messages(self, messages: list[dict[str, str]]) -> CompactionResult:
        """Compacts history when total estimated tokens exceed `token_budget`."""
        total_tokens = sum(estimate_token_count(m.get("content", "")) for m in messages)
        if total_tokens <= self.token_budget or len(messages) <= self.retain_recent_turns:
            return CompactionResult(
                compacted_triggered=False,
                original_turn_count=len(messages),
                retained_turn_count=len(messages),
                tokens_before=total_tokens,
                tokens_after=total_tokens,
                summary_digest="",
                retained_messages=list(messages),
            )

        cutoff = max(1, len(messages) - self.retain_recent_turns)
        evicted_slice = messages[:cutoff]
        recent_slice = messages[cutoff:]
        evicted_tokens = sum(estimate_token_count(m.get("content", "")) for m in evicted_slice)
        summary_digest = self.summarize_evicted_turns(evicted_slice)
        max_digest_chars = max(80, evicted_tokens * 2)
        if len(summary_digest) > max_digest_chars:
            summary_digest = summary_digest[:max_digest_chars].rsplit(" ", 1)[0] + "..."

        synthetic_summary_turn = {
            "role": "system",
            "content": summary_digest,
        }
        compacted_messages = [synthetic_summary_turn, *recent_slice]
        tokens_after = sum(estimate_token_count(m.get("content", "")) for m in compacted_messages)

        log_structured_event(
            "HISTORY_COMPACTION_EXECUTED",
            f"Compacted {len(messages)} turns down to {len(compacted_messages)} entries",
            metadata={
                "tokens_before": total_tokens,
                "tokens_after": tokens_after,
                "evicted_turns": cutoff,
            },
        )
        return CompactionResult(
            compacted_triggered=True,
            original_turn_count=len(messages),
            retained_turn_count=len(compacted_messages),
            tokens_before=total_tokens,
            tokens_after=tokens_after,
            summary_digest=summary_digest,
            retained_messages=compacted_messages,
        )


def build_adk_events_compaction_config() -> Any:
    """Builds the native Google ADK `EventsCompactionConfig` for the `App` container."""
    settings = get_settings()
    params = {
        "compaction_interval": settings.compaction_interval,
        "overlap_size": settings.compaction_overlap_size,
        "token_threshold": settings.compaction_token_threshold,
        "event_retention_size": settings.compaction_event_retention_size,
    }
    if EventsCompactionConfig is not None:
        return EventsCompactionConfig(**params)
    return params


def build_adk_context_cache_config() -> Any:
    """Builds the native Google ADK `ContextCacheConfig` for Gemini context caching on Vertex AI."""
    settings = get_settings()
    params = {
        "cache_intervals": 10,
        "ttl_seconds": settings.context_cache_ttl_seconds,
        "min_tokens": settings.context_cache_min_tokens,
    }
    if ContextCacheConfig is not None:
        return ContextCacheConfig(**params)
    return params
