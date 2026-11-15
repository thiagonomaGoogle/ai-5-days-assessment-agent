"""Tests for Category 5: Infrastructure, Secret Management, Code Hygiene, and Golden Eval Suite."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import unittest

from c4_architect_agent.config.secrets import SecretManagerResolver, SecretResolutionError
from evaluations.run_offline_eval import run_evaluation_suite


REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeSecretPayload:
    def __init__(self, raw: bytes) -> None:
        self.data = raw


class _FakeSecretResponse:
    def __init__(self, value: str) -> None:
        self.payload = _FakeSecretPayload(value.encode("utf-8"))


class _FakeSecretManagerClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def access_secret_version(self, request: dict[str, str]) -> _FakeSecretResponse:
        self.calls.append(request["name"])
        return _FakeSecretResponse("resolved-from-secret-manager")


class InfraSecurityAndEvalTests(unittest.TestCase):
    def test_secret_manager_resolver_fetches_and_caches_without_hardcoded_keys(self) -> None:
        fake_client = _FakeSecretManagerClient()
        resolver = SecretManagerResolver(
            project_id="c4-prod-project",
            ttl_seconds=120,
            client_factory=lambda: fake_client,
        )
        val1 = resolver.get_secret("c4-architect-publishing-webhook-token", required=True)
        val2 = resolver.get_secret("c4-architect-publishing-webhook-token", required=True)
        self.assertEqual(val1, "resolved-from-secret-manager")
        self.assertEqual(val2, "resolved-from-secret-manager")
        self.assertEqual(len(fake_client.calls), 1)

        empty_resolver = SecretManagerResolver(project_id="")
        with self.assertRaises(SecretResolutionError):
            empty_resolver.get_secret("missing-secret", required=True)

    def test_no_print_statements_or_hardcoded_secrets_in_agent_package(self) -> None:
        pkg_dir = REPO_ROOT / "c4_architect_agent"
        secret_regex = re.compile(
            r"(?:AIza[0-9A-Za-z\-_]{20,}|sk-[0-9A-Za-z]{20,}|BEGIN (?:RSA )?PRIVATE KEY)"
        )
        for py_file in pkg_dir.rglob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            self.assertIsNone(
                secret_regex.search(source),
                f"Potential hardcoded secret pattern found in {py_file}",
            )
            tree = ast.parse(source, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    self.assertNotEqual(
                        node.func.id,
                        "print",
                        f"Disallowed print() statement in {py_file}:{node.lineno}; use structured logger.",
                    )

    def test_terraform_and_ci_configurations_exist(self) -> None:
        self.assertTrue((REPO_ROOT / "terraform" / "main.tf").is_file())
        self.assertTrue((REPO_ROOT / "terraform" / "variables.tf").is_file())
        self.assertTrue((REPO_ROOT / "terraform" / "outputs.tf").is_file())
        self.assertTrue((REPO_ROOT / "Dockerfile").is_file())
        self.assertTrue((REPO_ROOT / ".github" / "workflows" / "ci.yml").is_file())

    def test_golden_evaluation_suite_passes_all_scenarios(self) -> None:
        report = run_evaluation_suite()
        self.assertTrue(report["passed"])
        self.assertEqual(report["score"], 1.0)


if __name__ == "__main__":
    unittest.main()
