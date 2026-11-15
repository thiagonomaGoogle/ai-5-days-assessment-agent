"""Asynchronous background memory consolidation worker and ADK plugin.

Offloads expensive episodic memory extraction, PII scrubbing, vector indexing,
and history compaction updates to non-blocking `asyncio` background tasks so
turn-level user responses never block on storage consolidation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import functools
from typing import Any, Optional

from ..observability.pii_redactor import get_pii_redactor
from ..observability.structured_logger import log_structured_event
from .compaction import HistoryCompactionEngine
from .persistent_store import PersistentArchitectureKnowledgeStore, get_knowledge_store

try:
    from google.adk.plugins.base_plugin import BasePlugin
except ImportError:  # pragma: no cover
    class BasePlugin:  # type: ignore[no-redef]
        def __init__(self, name: str) -> None:
            self.name = name


@dataclass
class MemoryConsolidationJob:
    """Unit of episodic memory work processed asynchronously in the background."""

    session_id: str
    project_slug: str
    agent_name: str
    summary_title: str
    transcript_turns: list[dict[str, str]]
    domain: str = "architecture"


class AsyncMemoryConsolidator:
    """Non-blocking asyncio queue + background task runner for memory consolidation."""

    def __init__(
        self,
        *,
        store: PersistentArchitectureKnowledgeStore | None = None,
        compactor: HistoryCompactionEngine | None = None,
    ) -> None:
        self._store = store or get_knowledge_store()
        self._compactor = compactor or HistoryCompactionEngine(
            token_budget=1200, retain_recent_turns=3, overlap_turns=1
        )
        self._redactor = get_pii_redactor()
        self._queue: asyncio.Queue[MemoryConsolidationJob] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._pending_tasks: set[asyncio.Task[str]] = set()
        self.completed_consolidations: int = 0

    def _ensure_queue(self) -> asyncio.Queue[MemoryConsolidationJob]:
        if self._queue is None:
            self._queue = asyncio.Queue()
        return self._queue

    async def _consolidate_single_job(self, job: MemoryConsolidationJob) -> str:
        """Executes compaction + PII scrubbing + SQLite vector persistence off the critical path."""
        await asyncio.sleep(0)  # Yield control immediately so caller/UI never blocks
        compacted = self._compactor.compact_messages(job.transcript_turns)
        digest = (
            compacted.summary_digest
            if compacted.summary_digest
            else self._compactor.summarize_evicted_turns(job.transcript_turns)
        )
        sanitized_digest = self._redactor.redact_text(digest)

        doc_id = await asyncio.to_thread(
            self._store.upsert_document,
            kind="episodic_memory",
            project_slug=job.project_slug,
            domain=job.domain,
            title=job.summary_title,
            content=sanitized_digest,
            metadata={
                "session_id": job.session_id,
                "agent_name": job.agent_name,
                "turns_processed": len(job.transcript_turns),
                "compacted_triggered": compacted.compacted_triggered,
            },
        )
        self.completed_consolidations += 1
        log_structured_event(
            "ASYNC_MEMORY_CONSOLIDATED",
            f"Consolidated episodic memory '{doc_id}' in background task",
            agent_name=job.agent_name,
            session_id=job.session_id,
            metadata={"doc_id": doc_id, "project_slug": job.project_slug},
        )
        return doc_id

    def schedule_consolidation(self, job: MemoryConsolidationJob) -> asyncio.Task[str]:
        """Schedules fire-and-forget background consolidation via `asyncio.create_task`."""
        task = asyncio.create_task(
            self._consolidate_single_job(job),
            name=f"c4-memory-consolidate-{job.session_id}",
        )
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    async def flush(self, timeout: float = 5.0) -> list[str]:
        """Awaits all in-flight background consolidation tasks (used on shutdown and tests)."""
        if not self._pending_tasks:
            return []
        tasks = list(self._pending_tasks)
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=timeout)
        return list(results)


class AsyncMemoryConsolidationPlugin(BasePlugin):
    """ADK Plugin dispatching async memory consolidation after root agent invocations."""

    def __init__(
        self,
        consolidator: AsyncMemoryConsolidator | None = None,
        name: str = "async_memory_consolidation_plugin",
    ) -> None:
        super().__init__(name=name)
        self.consolidator = consolidator or get_async_memory_consolidator()

    async def after_agent_callback(
        self, *, agent: Any, callback_context: Any
    ) -> Optional[Any]:
        state = getattr(callback_context, "state", None)
        if state is None:
            return None

        project_slug = str(state.get("project_slug") or "default_c4_workspace")
        latest_summary = str(state.get("latest_c4_summary") or "")
        if not latest_summary:
            return None

        session = getattr(callback_context, "session", None)
        session_id = str(getattr(session, "id", "local-session"))
        agent_name = str(getattr(agent, "name", "c4_architect_agent"))

        job = MemoryConsolidationJob(
            session_id=session_id,
            project_slug=project_slug,
            agent_name=agent_name,
            summary_title=f"Session Memory Snapshot ({project_slug})",
            transcript_turns=[{"role": "assistant", "content": latest_summary}],
        )
        self.consolidator.schedule_consolidation(job)
        return None


@functools.lru_cache(maxsize=1)
def get_async_memory_consolidator() -> AsyncMemoryConsolidator:
    """Returns singleton AsyncMemoryConsolidator."""
    return AsyncMemoryConsolidator()
