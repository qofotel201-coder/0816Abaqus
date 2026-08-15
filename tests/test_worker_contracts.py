"""Static safety-contract tests for the two Abaqus Python 2.7 workers."""

from __future__ import annotations

import ast
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
AUDIT_WORKER = ROOT / "automation" / "cae_audit_worker.py"
WORKSPACE_WORKER = ROOT / "automation" / "workspace_worker.py"


def method_keys(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(target, ast.Name) and target.id == "METHODS" for target in node.targets):
                if not isinstance(node.value, ast.Dict):
                    raise AssertionError("METHODS must be a literal dictionary")
                values: set[str] = set()
                for key in node.value.keys:
                    if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                        raise AssertionError("Every METHODS key must be a literal string")
                    values.add(key.value)
                return values
    raise AssertionError("METHODS mapping not found")


class WorkerContractTests(unittest.TestCase):
    def test_audit_worker_has_only_fixed_read_methods(self) -> None:
        self.assertEqual(
            method_keys(AUDIT_WORKER),
            {"system.capabilities", "model.deep_audit"},
        )

    def test_workspace_worker_has_only_fixed_write_methods(self) -> None:
        self.assertEqual(
            method_keys(WORKSPACE_WORKER),
            {
                "system.capabilities",
                "workspace.inspect_cae",
                "model.clone.commit",
                "job.bind_subroutine.commit",
                "job.write_input",
            },
        )

    def test_audit_worker_requires_staged_copy(self) -> None:
        source = AUDIT_WORKER.read_text(encoding="utf-8")
        self.assertIn("STAGED_COPY_CONFIRMATION_REQUIRED", source)
        self.assertNotIn(".save(", source)
        self.assertNotIn(".saveAs(", source)
        self.assertNotIn(".submit(", source)

    def test_workspace_worker_has_commit_and_no_solver_submission(self) -> None:
        source = WORKSPACE_WORKER.read_text(encoding="utf-8")
        self.assertIn("PLAN_HASH_MISMATCH", source)
        self.assertIn("COMMIT_TOKEN_REQUIRED", source)
        self.assertIn("OUTPUT_ALREADY_EXISTS", source)
        self.assertNotIn(".submit(", source)
        self.assertNotIn("taskkill", source.lower())

    def test_workers_do_not_accept_dynamic_code(self) -> None:
        for path in (AUDIT_WORKER, WORKSPACE_WORKER):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            calls = [
                node.func.id
                for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            ]
            self.assertNotIn("eval", calls, path.name)
            self.assertNotIn("exec", calls, path.name)
            self.assertNotIn("__import__", calls, path.name)

    def test_workers_remain_python_27_syntax_compatible_by_construction(self) -> None:
        # These tokens are reliable guards against the Python-3-only constructs
        # most likely to break Abaqus 2022's embedded Python 2.7.
        for path in (AUDIT_WORKER, WORKSPACE_WORKER):
            source = path.read_text(encoding="utf-8")
            self.assertNotIn("from pathlib", source, path.name)
            self.assertNotIn("async def ", source, path.name)
            self.assertNotIn("await ", source, path.name)
            self.assertNotIn("->", source, path.name)


if __name__ == "__main__":
    unittest.main()
