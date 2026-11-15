"""Tests for Category 1: Tool & Interface Design (docstrings, naming, schemas, guided errors)."""

from __future__ import annotations

import copy
import inspect
import tempfile
import unittest

from c4_architect_agent.memory.persistent_store import PersistentArchitectureKnowledgeStore
from c4_architect_agent.schemas.c4_schemas import C4ArchitectureSpecInput
from c4_architect_agent.tools import (
    extract_c4_entities_from_requirements,
    generate_c4_mermaid_and_structurizr_dsl,
    publish_approved_c4_architecture_bundle,
    record_architecture_decision_record,
    search_reference_architecture_patterns,
    validate_c4_architecture_compliance,
)
from evaluations.run_offline_eval import SAMPLE_GOLDEN_SPEC


class ToolAndSchemaDesignTests(unittest.TestCase):
    def test_all_tools_have_comprehensive_docstrings_and_specific_names(self) -> None:
        tools = [
            extract_c4_entities_from_requirements,
            search_reference_architecture_patterns,
            generate_c4_mermaid_and_structurizr_dsl,
            validate_c4_architecture_compliance,
            record_architecture_decision_record,
            publish_approved_c4_architecture_bundle,
        ]
        for fn in tools:
            doc = inspect.getdoc(fn) or ""
            self.assertGreater(len(doc), 180, f"Docstring too short on {fn.__name__}")
            self.assertIn("Args:", doc, f"Missing Args section on {fn.__name__}")
            self.assertIn("Returns:", doc, f"Missing Returns section on {fn.__name__}")
            self.assertGreaterEqual(len(fn.__name__.split("_")), 3)

    def test_explicit_json_schema_generation_and_extra_forbid(self) -> None:
        schema = C4ArchitectureSpecInput.model_json_schema()
        self.assertEqual(schema["type"], "object")
        self.assertIn("project_slug", schema["properties"])
        self.assertIn("containers", schema["required"])

        bad_spec = copy.deepcopy(SAMPLE_GOLDEN_SPEC)
        bad_spec["unexpected_hallucinated_field"] = "should_be_rejected"
        res = generate_c4_mermaid_and_structurizr_dsl(bad_spec)
        self.assertEqual(res["status"], "error")
        self.assertEqual(res["error_code"], "C4_SCHEMA_VALIDATION_FAILED")
        self.assertIn("recovery_instructions", res)

    def test_generate_l1_to_l4_c4_diagrams_and_structurizr_dsl(self) -> None:
        res = generate_c4_mermaid_and_structurizr_dsl(SAMPLE_GOLDEN_SPEC)
        self.assertEqual(res["status"], "success")
        self.assertIn("C4Context", res["c4_context_mermaid"])
        self.assertIn("C4Container", res["c4_container_mermaid"])
        self.assertIn("ContainerDb(ledger_database", res["c4_container_mermaid"])
        self.assertIn("ContainerQueue(ledger_event_bus", res["c4_container_mermaid"])
        self.assertIn("C4Component", res["c4_component_mermaid"])
        self.assertIn("classDiagram", res["c4_code_mermaid"])
        self.assertIn('workspace "OmniPay Checkout Platform"', res["structurizr_dsl"])
        self.assertEqual(res["element_counts"]["containers"], 4)

    def test_guided_error_handling_on_dangling_relationship_and_bad_slug(self) -> None:
        bad_slug = extract_c4_entities_from_requirements(
            project_slug="INVALID SLUG!!",
            requirements_brief="Valid brief with web portal and database.",
        )
        self.assertEqual(bad_slug["status"], "error")
        self.assertEqual(bad_slug["error_code"], "INVALID_PROJECT_SLUG")
        self.assertIn("snake_case", bad_slug["recovery_instructions"])

        dangling_spec = copy.deepcopy(SAMPLE_GOLDEN_SPEC)
        dangling_spec["relationships"].append(
            {
                "source_alias": "payment_api_service",
                "target_alias": "nonexistent_ghost_service",
                "label": "Calls",
                "protocol": "gRPC/mTLS",
            }
        )
        dangling_res = generate_c4_mermaid_and_structurizr_dsl(dangling_spec)
        self.assertEqual(dangling_res["status"], "error")
        self.assertEqual(dangling_res["error_code"], "DANGLING_C4_RELATIONSHIP_ALIAS")
        self.assertIn("defined aliases", dangling_res["recovery_instructions"])

    def test_compliance_validator_catches_direct_db_access_and_plaintext_http(self) -> None:
        insecure_spec = copy.deepcopy(SAMPLE_GOLDEN_SPEC)
        insecure_spec["relationships"].append(
            {
                "source_alias": "online_shopper",
                "target_alias": "ledger_database",
                "label": "Queries ledger directly",
                "protocol": "HTTP plaintext",
            }
        )
        report = validate_c4_architecture_compliance(insecure_spec)
        self.assertEqual(report["status"], "success")
        self.assertFalse(report["passed"])
        self.assertLess(report["score"], 60)
        rule_ids = {f["rule_id"] for f in report["findings"]}
        self.assertIn("C4-SEC-001", rule_ids)
        self.assertIn("C4-SEC-002", rule_ids)

    def test_isolated_knowledge_store_persists_adr_across_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_file = f"{tmp}/test_store.sqlite3"
            store1 = PersistentArchitectureKnowledgeStore(db_path=db_file)
            doc_id = store1.upsert_document(
                kind="adr",
                project_slug="c4_demo",
                domain="fintech",
                title="ADR-010: Event-Driven Saga Pattern",
                content="Contact lead@corp.example.com. Adopted choreography saga over Pub/Sub.",
            )
            store2 = PersistentArchitectureKnowledgeStore(db_path=db_file)
            hits = store2.search("choreography saga pubsub", project_slug="c4_demo", top_k=2)
            self.assertGreaterEqual(len(hits), 1)
            self.assertEqual(hits[0]["doc_id"], doc_id)
            self.assertIn("[REDACTED_EMAIL]", hits[0]["content"])


if __name__ == "__main__":
    unittest.main()
