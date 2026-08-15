"""Functional and security tests for the external CPython 3 orchestrator."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
import uuid

from automation.common import (
    AutomationConfig,
    AutomationError,
    object_sha256,
    read_json,
    sha256_file,
)
from automation.orchestrator import AutomationOrchestrator, handle_jsonrpc


class OrchestratorContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="phase0-orchestrator-")
        self.root = Path(self.temporary.name)
        self.source_root = self.root / "source"
        self.fortran_root = self.root / "fortran"
        self.workspace_root = self.root / "workspaces"
        self.source_root.mkdir()
        self.fortran_root.mkdir()
        self.workspace_root.mkdir()
        self.source_cae = self.source_root / "model.cae"
        self.source_cae.write_bytes(b"immutable-test-cae\x00\x01")
        self.subroutine = self.fortran_root / "approved.for"
        self.subroutine.write_text(
            "      subroutine vuamp()\n"
            "      return\n"
            "      end\n"
            "      subroutine vusdfld()\n"
            "      return\n"
            "      end\n",
            encoding="ascii",
        )
        config = AutomationConfig(
            allowed_source_roots=(self.source_root,),
            allowed_subroutine_roots=(self.fortran_root,),
            workspace_root=self.workspace_root,
            allow_job_kill=False,
            solver_execution_enabled=False,
        )
        self.orchestrator = AutomationOrchestrator(config)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_fixed_method_surface_and_solver_policy(self) -> None:
        result = self.orchestrator.system_capabilities({})
        self.assertEqual(
            set(result["fixed_methods"]),
            {
                "system.capabilities",
                "source.manifest",
                "subroutine.validate",
                "workspace.plan",
                "workspace.plan_create",
                "workspace.create",
                "workspace.inspect",
                "job.status",
                "job.artifacts",
                "job.kill.plan",
                "job.kill.commit",
                "job.data_check",
                "job.submit",
            },
        )
        self.assertFalse(result["policies"]["solver_execution_implemented"])
        self.assertFalse(result["policies"]["solver_started_by_this_release"])
        self.assertFalse(result["policies"]["arbitrary_code_requests"])

    def test_source_manifest_and_subroutine_static_gate(self) -> None:
        cae_hash = sha256_file(self.source_cae)
        source = self.orchestrator.source_manifest(
            {"kind": "cae", "path": str(self.source_cae), "expected_sha256": cae_hash}
        )
        self.assertEqual(source["gate"]["status"], "GO")
        validated = self.orchestrator.subroutine_validate(
            {
                "path": str(self.subroutine),
                "expected_sha256": sha256_file(self.subroutine),
                "required_symbols": ["VUAMP", "VUSDFLD"],
            }
        )
        self.assertEqual(validated["entry_points"], ["VUAMP", "VUSDFLD"])
        self.assertEqual(validated["gate"]["status"], "GO_STATIC_ONLY")
        self.assertFalse(validated["approved_for_compilation"])
        self.assertFalse(validated["approved_for_execution"])

    def test_workspace_plan_create_inspect_and_replay_rejection(self) -> None:
        original_hash = sha256_file(self.source_cae)
        original_stat = self.source_cae.stat()
        planned = self.orchestrator.workspace_plan(
            {
                "source_cae": str(self.source_cae),
                "expected_source_sha256": original_hash,
                "label": "phase0-test",
            }
        )
        plan_path = Path(planned["evidence"]["plan_path"])
        plan = read_json(plan_path)
        created = self.orchestrator.workspace_create(
            {
                "plan_id": planned["plan_id"],
                "plan_sha256": planned["plan_sha256"],
                "operation_id": plan["operation_id"],
                "confirmation_token": planned["confirmation_token"],
            }
        )
        self.assertEqual(created["status"], "CREATED")
        self.assertTrue(created["source_unchanged"])
        inspected = self.orchestrator.workspace_inspect(
            {"workspace_id": planned["workspace_id"]}
        )
        self.assertEqual(inspected["status"], "READY")
        self.assertEqual(inspected["gate"]["status"], "GO")
        self.assertEqual(sha256_file(self.source_cae), original_hash)
        self.assertEqual(self.source_cae.stat().st_mtime_ns, original_stat.st_mtime_ns)
        with self.assertRaises(AutomationError) as caught:
            self.orchestrator.workspace_create(
                {
                    "plan_id": planned["plan_id"],
                    "plan_sha256": planned["plan_sha256"],
                    "operation_id": plan["operation_id"],
                    "confirmation_token": planned["confirmation_token"],
                }
            )
        self.assertIn(
            caught.exception.code,
            {"TARGET_EXISTS", "FILE_EXISTS", "NO_OVERWRITE"},
        )

    def test_wrong_hash_unknown_method_and_unknown_job_fail_closed(self) -> None:
        with self.assertRaises(AutomationError) as caught:
            self.orchestrator.source_manifest(
                {"kind": "cae", "path": str(self.source_cae), "expected_sha256": "0" * 64}
            )
        self.assertEqual(caught.exception.code, "SOURCE_HASH_MISMATCH")
        with self.assertRaises(AutomationError) as caught:
            self.orchestrator.dispatch("python.eval", {})
        self.assertEqual(caught.exception.code, "METHOD_NOT_FOUND")
        with self.assertRaises(AutomationError):
            self.orchestrator.dispatch(
                "job.status",
                {
                    "workspace_id": str(uuid.uuid4()),
                    "job_id": str(uuid.uuid4()),
                },
            )

    def test_jsonrpc_id_and_nonfinite_or_duplicate_json_are_rejected(self) -> None:
        request_id = str(uuid.uuid4())
        response = handle_jsonrpc(
            self.orchestrator,
            {"jsonrpc": "2.0", "id": request_id, "method": "system.capabilities", "params": {}},
        )
        self.assertEqual(response["id"], request_id)
        self.assertIn("result", response)

        duplicate = self.root / "duplicate.json"
        duplicate.write_text('{"a": 1, "a": 2}', encoding="utf-8")
        with self.assertRaises(AutomationError) as caught:
            read_json(duplicate)
        self.assertEqual(caught.exception.code, "DUPLICATE_JSON_KEY")
        nonfinite = self.root / "nonfinite.json"
        nonfinite.write_text('{"a": NaN}', encoding="utf-8")
        with self.assertRaises(AutomationError) as caught:
            read_json(nonfinite)
        self.assertEqual(caught.exception.code, "NONFINITE_JSON_NUMBER")

    def test_plan_hash_is_canonical(self) -> None:
        value = {"b": 2, "a": [1, 3]}
        reordered = json.loads('{"a":[1,3],"b":2}')
        self.assertEqual(object_sha256(value), object_sha256(reordered))


if __name__ == "__main__":
    unittest.main()
