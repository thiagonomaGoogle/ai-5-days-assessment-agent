"""Tests for Category 2: Context & Memory (constitution, compaction, persistence, async ops)."""

from __future__ import annotations

import tempfile
import unittest

from c4_architect_agent.memory.async_consolidator import (
    AsyncMemoryConsolidator,
    MemoryConsolidationJob,
)
from c4_architect_agent.memory.compaction import (
    HistoryCompactionEngine,
    build_adk_context_cache_config,
    build_adk_events_compaction_config,
)
from c4_architect_agent.memory.persistent_store import (
    PersistentArchitectureKnowledgeStore,
    build_persistent_session_service,
)
from c4_architect_agent.prompts.constitution import COORDINATOR_CONSTITUTION


class ContextAndMemoryTests(unittest.IsolatedAsyncioTestCase):
    def test_system_constitution_defines_persona_c4_laws_and_constraints(self) -> None:
        self.assertIn("<persona_and_mission>", COORDINATOR_CONSTITUTION)
        self.assertIn("<c4_domain_laws>", COORDINATOR_CONSTITUTION)
        self.assertIn("<security_and_governance_constraints>", COORDINATOR_CONSTITUTION)
        self.assertIn("{project_slug?}", COORDINATOR_CONSTITUTION)

    def test_history_compaction_reduces_token_bloat_and_retains_c4_decisions(self) -> None:
        engine = HistoryCompactionEngine(
            token_budget=120,
            retain_recent_turns=2,
            overlap_turns=1,
        )
        turns = [
            {
                "role": "user",
                "content": (
                    "System: Global Checkout C4 architecture with Container payment_api and "
                    "Spanner database. " + ("Detailed legacy migration constraints and SLA notes. " * 6)
                ),
            },
            {
                "role": "assistant",
                "content": (
                    "ADR Decision: Adopted mTLS gRPC between API gateway and payment container "
                    "with Pub/Sub queue. " + ("Extended capacity planning rationale and benchmarks. " * 6)
                ),
            },
            {
                "role": "user",
                "content": "Add a Redis cache container for low-latency cart lookups and explain failover behavior in detail " * 3,
            },
            {
                "role": "assistant",
                "content": "Added ContainerDb session_cache using Memorystore Redis with TLS 1.3 and regional replicas.",
            },
            {
                "role": "user",
                "content": "Now generate the Level 3 component diagram for payment_api.",
            },
        ]
        result = engine.compact_messages(turns)
        self.assertTrue(result.compacted_triggered)
        self.assertLess(result.retained_turn_count, result.original_turn_count)
        self.assertLess(result.tokens_after, result.tokens_before)
        self.assertIn("Compacted Architectural Context Summary", result.summary_digest)
        self.assertIn("Spanner", result.summary_digest)

    def test_adk_compaction_and_context_cache_configs_and_session_service(self) -> None:
        comp_cfg = build_adk_events_compaction_config()
        cache_cfg = build_adk_context_cache_config()
        self.assertIsNotNone(comp_cfg)
        self.assertIsNotNone(cache_cfg)

        with tempfile.TemporaryDirectory() as tmp:
            svc = build_persistent_session_service(f"sqlite+aiosqlite:///{tmp}/sessions.db")
            self.assertIsNotNone(svc)

    async def test_async_memory_consolidator_executes_in_background_without_blocking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PersistentArchitectureKnowledgeStore(db_path=f"{tmp}/async_mem.sqlite3")
            consolidator = AsyncMemoryConsolidator(store=store)
            job = MemoryConsolidationJob(
                session_id="sess-async-001",
                project_slug="c4_async_project",
                agent_name="c4_architecture_coordinator",
                summary_title="Async Episodic Snapshot",
                transcript_turns=[
                    {
                        "role": "assistant",
                        "content": "Container billing_service writes to Cloud SQL over TLS for user john@example.com.",
                    }
                ],
            )
            task = consolidator.schedule_consolidation(job)
            self.assertFalse(task.done() and consolidator.completed_consolidations == 0)
            doc_ids = await consolidator.flush()
            self.assertEqual(len(doc_ids), 1)
            self.assertEqual(consolidator.completed_consolidations, 1)

            hits = store.search("billing_service Cloud SQL", project_slug="c4_async_project")
            self.assertGreaterEqual(len(hits), 1)
            self.assertEqual(hits[0]["project_slug"], "c4_async_project")
            self.assertIn("[REDACTED_EMAIL]", hits[0]["content"])


if __name__ == "__main__":
    unittest.main()
