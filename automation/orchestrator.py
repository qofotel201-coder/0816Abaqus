"""External CPython 3 orchestration API for safe Abaqus workspaces.

The module deliberately contains no Abaqus imports.  It exposes a fixed
JSON-RPC method table and delegates registered-job inspection to job_manager.
Solver launch methods are present only as hard-gated stubs in this release.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable, Mapping

try:
    from .common import (
        AutomationConfig,
        AutomationError,
        SCHEMA_VERSION,
        atomic_copy_new,
        atomic_write_new_json,
        bounded_text,
        file_manifest,
        is_within,
        load_config,
        managed_child,
        new_confirmation_token,
        new_uuid,
        object_sha256,
        read_json,
        read_latest_manifest,
        require_mapping,
        require_sha256,
        require_uuid,
        resolve_allowed_file,
        safe_label,
        sha256_file,
        utc_now,
        verify_confirmation_token,
        write_next_manifest,
    )
    from .job_manager import JobManager
except ImportError:  # Direct script execution with the automation directory on sys.path.
    from common import (  # type: ignore
        AutomationConfig,
        AutomationError,
        SCHEMA_VERSION,
        atomic_copy_new,
        atomic_write_new_json,
        bounded_text,
        file_manifest,
        is_within,
        load_config,
        managed_child,
        new_confirmation_token,
        new_uuid,
        object_sha256,
        read_json,
        read_latest_manifest,
        require_mapping,
        require_sha256,
        require_uuid,
        resolve_allowed_file,
        safe_label,
        sha256_file,
        utc_now,
        verify_confirmation_token,
        write_next_manifest,
    )
    from job_manager import JobManager  # type: ignore


ORCHESTRATOR_VERSION = "0.1.0"
JSONRPC_VERSION = "2.0"
PLAN_STEM = "workspace-plan"
WORKSPACE_MANIFEST_STEM = "workspace.manifest"
FORTRAN_ENTRY_RE = re.compile(r"(?im)^\s*(?:[0-9]{1,5}\s*)?subroutine\s+([a-z][a-z0-9_]*)\s*\(")
SUSPICIOUS_FORTRAN_RE = {
    "system_call": re.compile(r"(?i)\b(?:call\s+system|execute_command_line|runqq|createprocess)\b"),
    "dynamic_library": re.compile(r"(?i)\b(?:loadlibrary|dlopen)\b"),
    "shell_reference": re.compile(r"(?i)\b(?:cmd\.exe|powershell|/bin/sh)\b"),
}


class AutomationOrchestrator:
    """Fixed-method external orchestration service."""

    def __init__(self, config: AutomationConfig) -> None:
        self.config = config
        self.jobs = JobManager(config)
        self._methods: dict[str, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
            "system.capabilities": self.system_capabilities,
            "source.manifest": self.source_manifest,
            "subroutine.validate": self.subroutine_validate,
            "workspace.plan": self.workspace_plan,
            "workspace.plan_create": self.workspace_plan,
            "workspace.create": self.workspace_create,
            "workspace.inspect": self.workspace_inspect,
            "job.status": self.jobs.status,
            "job.artifacts": self.jobs.artifacts,
            "job.kill.plan": self.jobs.kill_plan,
            "job.kill.commit": self.jobs.kill_commit,
            "job.data_check": self.jobs.data_check_gated,
            "job.submit": self.jobs.submit_gated,
        }

    @property
    def plan_root(self) -> Path:
        return self.config.workspace_root / "_plans"

    def dispatch(self, method: Any, params: Any) -> dict[str, Any]:
        if not isinstance(method, str) or method not in self._methods:
            raise AutomationError("METHOD_NOT_FOUND", "Method is not in the fixed automation allowlist", {"method": method})
        return self._methods[method](require_mapping(params, "params"))

    def system_capabilities(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if params:
            raise AutomationError("UNEXPECTED_PARAMETER", "system.capabilities accepts no parameters")
        return {
            "schema": SCHEMA_VERSION,
            "orchestrator_version": ORCHESTRATOR_VERSION,
            "python_executable": sys.executable,
            "python_version": sys.version.replace("\n", " "),
            "fixed_methods": sorted(self._methods),
            "roots": {
                "allowed_source_roots": [str(path) for path in self.config.allowed_source_roots],
                "allowed_subroutine_roots": [str(path) for path in self.config.allowed_subroutine_roots],
                "workspace_root": str(self.config.workspace_root),
            },
            "policies": {
                "no_overwrite": True,
                "append_only_manifests": True,
                "two_phase_workspace_create": True,
                "two_phase_job_kill": True,
                "allow_job_kill": self.config.allow_job_kill,
                "solver_execution_configured": self.config.solver_execution_enabled,
                "solver_execution_implemented": False,
                "solver_started_by_this_release": False,
                "arbitrary_code_requests": False,
            },
        }

    def source_manifest(self, params: Mapping[str, Any]) -> dict[str, Any]:
        kind = bounded_text(params.get("kind", "cae"), "kind", 32).lower()
        kind_rules: dict[str, tuple[tuple[str, ...], tuple[Path, ...]]] = {
            "cae": ((".cae",), self.config.allowed_source_roots),
            "odb": ((".odb",), self.config.allowed_source_roots),
            "input": ((".inp",), self.config.allowed_source_roots),
            "subroutine": ((".for", ".f", ".f90"), self.config.allowed_subroutine_roots),
        }
        if kind not in kind_rules:
            raise AutomationError("INVALID_SOURCE_KIND", "Unsupported source kind", {"kind": kind})
        suffixes, roots = kind_rules[kind]
        path = resolve_allowed_file(params.get("path"), roots, suffixes)
        if kind == "subroutine" and is_within(path, self.config.workspace_root):
            raise AutomationError("WORKSPACE_SOURCE_DENIED", "A workspace-generated file is not an approved subroutine source")
        manifest = file_manifest(path, include_sha256=True)
        expected = params.get("expected_sha256")
        if expected is not None and manifest["sha256"] != require_sha256(expected, "expected_sha256"):
            raise AutomationError(
                "SOURCE_HASH_MISMATCH",
                "Source content does not match expected_sha256",
                {"path": str(path), "observed_sha256": manifest["sha256"]},
            )
        return {
            "schema": SCHEMA_VERSION,
            "kind": kind,
            "source": manifest,
            "gate": {"status": "GO", "reasons": []},
        }

    def subroutine_validate(self, params: Mapping[str, Any]) -> dict[str, Any]:
        path = resolve_allowed_file(
            params.get("path"),
            self.config.allowed_subroutine_roots,
            (".for", ".f", ".f90"),
            "path",
        )
        if is_within(path, self.config.workspace_root):
            raise AutomationError("WORKSPACE_SOURCE_DENIED", "A workspace-generated file is not an approved subroutine source")
        expected_hash = require_sha256(params.get("expected_sha256"), "expected_sha256")
        manifest = file_manifest(path, include_sha256=True)
        if manifest["sha256"] != expected_hash:
            raise AutomationError(
                "SUBROUTINE_HASH_MISMATCH",
                "Fortran source does not match expected_sha256",
                {"observed_sha256": manifest["sha256"]},
            )
        if manifest["size"] > self.config.max_subroutine_bytes:
            raise AutomationError(
                "SUBROUTINE_TOO_LARGE",
                "Fortran source exceeds the configured static-validation limit",
                {"size": manifest["size"], "limit": self.config.max_subroutine_bytes},
            )
        raw = path.read_bytes()
        try:
            source = raw.decode("utf-8-sig")
            encoding = "utf-8"
        except UnicodeDecodeError:
            source = raw.decode("latin-1")
            encoding = "latin-1-lossless"

        # Remove whole-line fixed/free-form comments before scanning entry points.
        code_lines = []
        for line in source.splitlines():
            if line and line[0] in ("c", "C", "*", "!"):
                continue
            code_lines.append(line.split("!", 1)[0])
        code = "\n".join(code_lines)
        entry_points = sorted({match.group(1).upper() for match in FORTRAN_ENTRY_RE.finditer(code)})
        requested = params.get("required_symbols", ["VUAMP", "VUSDFLD"])
        if not isinstance(requested, list) or not requested:
            raise AutomationError("INVALID_REQUIRED_SYMBOLS", "required_symbols must be a non-empty array")
        allowed_required = {"VUAMP", "VUSDFLD"}
        required_symbols = []
        for symbol in requested:
            normalized = bounded_text(symbol, "required_symbols item", 32).upper()
            if normalized not in allowed_required:
                raise AutomationError("SYMBOL_NOT_ALLOWED", "Only VUAMP and VUSDFLD may be required", {"symbol": normalized})
            required_symbols.append(normalized)
        required_symbols = sorted(set(required_symbols))
        missing = [symbol for symbol in required_symbols if symbol not in entry_points]
        suspicious = [name for name, pattern in SUSPICIOUS_FORTRAN_RE.items() if pattern.search(code)]
        gate_status = "GO_STATIC_ONLY" if not missing and not suspicious else "NO_GO"
        reasons = []
        if missing:
            reasons.append("missing required entry points: %s" % ", ".join(missing))
        if suspicious:
            reasons.append("native-code security review required: %s" % ", ".join(suspicious))
        return {
            "schema": SCHEMA_VERSION,
            "source": manifest,
            "encoding": encoding,
            "entry_points": entry_points,
            "required_symbols": required_symbols,
            "missing_symbols": missing,
            "security_findings": suspicious,
            "approved_for_compilation": False,
            "approved_for_execution": False,
            "gate": {"status": gate_status, "reasons": reasons},
        }

    def workspace_plan(self, params: Mapping[str, Any]) -> dict[str, Any]:
        source = resolve_allowed_file(params.get("source_cae"), self.config.allowed_source_roots, (".cae",), "source_cae")
        expected_hash = require_sha256(params.get("expected_source_sha256"), "expected_source_sha256")
        source_info = file_manifest(source, include_sha256=True)
        if source_info["sha256"] != expected_hash:
            raise AutomationError(
                "SOURCE_HASH_MISMATCH",
                "Source CAE differs from expected_source_sha256",
                {"observed_sha256": source_info["sha256"]},
            )
        label = safe_label(params.get("label", "abaqus-workspace"))
        workspace_id = new_uuid()
        plan_id = new_uuid()
        operation_id = new_uuid()
        confirmation_token, confirmation_hash = new_confirmation_token()
        destination = managed_child(self.config.workspace_root, workspace_id)
        self.plan_root.mkdir(parents=False, exist_ok=True)
        if not is_within(self.plan_root.resolve(strict=True), self.config.workspace_root):
            raise AutomationError("PLAN_ROOT_ESCAPE", "Plan directory escaped workspace_root")
        plan = {
            "schema": SCHEMA_VERSION,
            "kind": "workspace.create",
            "plan_id": plan_id,
            "operation_id": operation_id,
            "created_at": utc_now(),
            "expires_at_epoch": int(time.time()) + self.config.plan_ttl_seconds,
            "workspace_id": workspace_id,
            "label": label,
            "source": source_info,
            "destination": {
                "workspace_dir": str(destination),
                "cae_path": str(destination / "model.cae"),
            },
            "confirmation_token_sha256": confirmation_hash,
            "contracts": {
                "no_overwrite": True,
                "source_must_remain_unchanged": True,
                "append_only_manifest": True,
            },
        }
        plan_sha256 = object_sha256(plan)
        plan_path = self.plan_root / (PLAN_STEM + "-" + plan_id + ".json")
        atomic_write_new_json(plan_path, plan)
        return {
            "schema": SCHEMA_VERSION,
            "status": "PLANNED",
            "plan_id": plan_id,
            "plan_sha256": plan_sha256,
            "confirmation_token": confirmation_token,
            "workspace_id": workspace_id,
            "source": source_info,
            "destination": plan["destination"],
            "expires_at_epoch": plan["expires_at_epoch"],
            "evidence": {"plan_path": str(plan_path), "plan_file_sha256": sha256_file(plan_path)},
            "gate": {"status": "GO", "reasons": []},
        }

    def workspace_create(self, params: Mapping[str, Any]) -> dict[str, Any]:
        plan_id = require_uuid(params.get("plan_id"), "plan_id")
        requested_plan_hash = require_sha256(params.get("plan_sha256"), "plan_sha256")
        operation_id = require_uuid(params.get("operation_id"), "operation_id")
        plan_path = self.plan_root / (PLAN_STEM + "-" + plan_id + ".json")
        if not plan_path.is_file():
            raise AutomationError("PLAN_NOT_FOUND", "Workspace plan does not exist", {"plan_id": plan_id})
        plan = require_mapping(read_json(plan_path), "workspace plan")
        if plan.get("kind") != "workspace.create" or plan.get("plan_id") != plan_id:
            raise AutomationError("PLAN_CONTRACT_MISMATCH", "Workspace plan identity is invalid")
        observed_plan_hash = object_sha256(plan)
        if observed_plan_hash != requested_plan_hash:
            raise AutomationError("PLAN_HASH_MISMATCH", "Workspace plan content changed")
        if operation_id != plan.get("operation_id"):
            raise AutomationError("OPERATION_ID_MISMATCH", "operation_id does not match the plan")
        verify_confirmation_token(params.get("confirmation_token"), plan.get("confirmation_token_sha256"))
        if int(plan.get("expires_at_epoch", 0)) < int(time.time()):
            raise AutomationError("PLAN_EXPIRED", "Workspace plan has expired")

        workspace_id = require_uuid(plan.get("workspace_id"), "workspace_id")
        workspace_dir = managed_child(self.config.workspace_root, workspace_id)
        if str(workspace_dir) != str(plan.get("destination", {}).get("workspace_dir")):
            raise AutomationError("PLAN_DESTINATION_MISMATCH", "Planned workspace path is not canonical")
        claim_path = self.plan_root / (PLAN_STEM + "-" + plan_id + ".claim.json")
        atomic_write_new_json(
            claim_path,
            {"schema": SCHEMA_VERSION, "plan_id": plan_id, "operation_id": operation_id, "claimed_at": utc_now()},
        )

        source_path = resolve_allowed_file(plan.get("source", {}).get("path"), self.config.allowed_source_roots, (".cae",), "source_cae")
        expected_source_hash = require_sha256(plan.get("source", {}).get("sha256"), "planned source sha256")
        before = file_manifest(source_path, include_sha256=True)
        if before["sha256"] != expected_source_hash:
            raise AutomationError("SOURCE_CHANGED_AFTER_PLAN", "Source CAE changed after workspace.plan", {"observed": before["sha256"]})

        try:
            workspace_dir.mkdir(parents=False, exist_ok=False)
            staged = atomic_copy_new(source_path, workspace_dir / "model.cae")
            after = file_manifest(source_path, include_sha256=True)
            if after["sha256"] != expected_source_hash or after["size"] != before["size"] or after["mtime_ns"] != before["mtime_ns"]:
                raise AutomationError("SOURCE_CHANGED_DURING_COPY", "Source CAE changed while creating the workspace", {"before": before, "after": after})
            if staged["sha256"] != expected_source_hash:
                raise AutomationError("STAGED_COPY_HASH_MISMATCH", "Workspace CAE does not match its source")
            manifest_value = {
                "schema": SCHEMA_VERSION,
                "kind": "workspace",
                "workspace_id": workspace_id,
                "label": plan.get("label"),
                "created_at": utc_now(),
                "lifecycle_state": "READY",
                "source": before,
                "files": {"cae": staged},
                "jobs": {},
                "contracts": plan.get("contracts"),
            }
            manifest, manifest_path, manifest_hash, version = write_next_manifest(
                workspace_dir,
                WORKSPACE_MANIFEST_STEM,
                manifest_value,
                expected_current_version=0,
            )
            receipt_path = workspace_dir / "workspace-create.receipt.json"
            receipt = {
                "schema": SCHEMA_VERSION,
                "plan_id": plan_id,
                "plan_sha256": requested_plan_hash,
                "operation_id": operation_id,
                "completed_at": utc_now(),
                "workspace_id": workspace_id,
                "workspace_manifest_sha256": manifest_hash,
            }
            atomic_write_new_json(receipt_path, receipt)
        except Exception as exc:
            failure_path = self.plan_root / (PLAN_STEM + "-" + plan_id + ".failure.json")
            failure = {
                "schema": SCHEMA_VERSION,
                "plan_id": plan_id,
                "operation_id": operation_id,
                "failed_at": utc_now(),
                "error_type": type(exc).__name__,
                "error": str(exc),
                "partial_workspace": str(workspace_dir),
                "cleanup_automatic": False,
            }
            try:
                atomic_write_new_json(failure_path, failure)
            except AutomationError:
                pass
            raise

        return {
            "schema": SCHEMA_VERSION,
            "status": "CREATED",
            "workspace_id": workspace_id,
            "workspace_version": version,
            "workspace_dir": str(workspace_dir),
            "source_unchanged": True,
            "files": manifest["files"],
            "evidence": {
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_hash,
                "receipt_path": str(receipt_path),
                "receipt_sha256": sha256_file(receipt_path),
                "claim_path": str(claim_path),
            },
            "gate": {"status": "GO", "reasons": []},
        }

    def workspace_inspect(self, params: Mapping[str, Any]) -> dict[str, Any]:
        workspace_id = require_uuid(params.get("workspace_id"), "workspace_id")
        workspace_dir = managed_child(self.config.workspace_root, workspace_id)
        if not workspace_dir.is_dir():
            raise AutomationError("WORKSPACE_NOT_FOUND", "Registered workspace directory does not exist", {"workspace_id": workspace_id})
        resolved_workspace = workspace_dir.resolve(strict=True)
        if os.path.normcase(str(resolved_workspace)) != os.path.normcase(str(workspace_dir)):
            raise AutomationError("WORKSPACE_SYMLINK_DENIED", "Workspace directory may not be a symlink or junction")
        manifest, manifest_path, manifest_hash, version = read_latest_manifest(workspace_dir, WORKSPACE_MANIFEST_STEM)
        if manifest.get("workspace_id") != workspace_id or manifest.get("kind") != "workspace":
            raise AutomationError("INVALID_WORKSPACE_MANIFEST", "Workspace manifest identity does not match its directory")
        recorded_cae = require_mapping(manifest.get("files", {}).get("cae"), "workspace CAE manifest")
        expected_path = workspace_dir / "model.cae"
        if str(recorded_cae.get("path")) != str(expected_path) or not expected_path.is_file():
            integrity = False
            observed_cae: dict[str, Any] = {"path": str(expected_path), "missing": not expected_path.is_file()}
        else:
            resolved_cae = expected_path.resolve(strict=True)
            if not is_within(resolved_cae, resolved_workspace) or resolved_cae != expected_path:
                integrity = False
                observed_cae = {"path": str(expected_path), "path_escape_or_link": True}
            else:
                observed_cae = file_manifest(expected_path, include_sha256=True)
                integrity = (
                    observed_cae["sha256"] == recorded_cae.get("sha256")
                    and observed_cae["size"] == recorded_cae.get("size")
                )
        known_names = {"model.cae", "workspace-create.receipt.json", manifest_path.name, "jobs", "control"}
        unexpected = sorted(
            child.name
            for child in workspace_dir.iterdir()
            if child.name not in known_names and not child.name.startswith(WORKSPACE_MANIFEST_STEM + ".v")
        )
        return {
            "schema": SCHEMA_VERSION,
            "status": "READY" if integrity else "INTEGRITY_FAILED",
            "workspace_id": workspace_id,
            "workspace_version": version,
            "manifest": manifest,
            "observed_cae": observed_cae,
            "unexpected_entries": unexpected,
            "evidence": {"manifest_path": str(manifest_path), "manifest_sha256": manifest_hash},
            "gate": {
                "status": "GO" if integrity else "NO_GO",
                "reasons": [] if integrity else ["workspace CAE differs from its registered manifest"],
            },
        }


def handle_jsonrpc(orchestrator: AutomationOrchestrator, request: Any) -> dict[str, Any]:
    request_id: Any = None
    try:
        body = require_mapping(request, "request")
        request_id = require_uuid(body.get("id"), "id")
        if body.get("jsonrpc") != JSONRPC_VERSION:
            raise AutomationError("INVALID_JSONRPC_VERSION", "jsonrpc must equal 2.0")
        result = orchestrator.dispatch(body.get("method"), body.get("params", {}))
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}
    except AutomationError as exc:
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": exc.as_jsonrpc_error()}
    except Exception as exc:
        error = AutomationError("INTERNAL_ERROR", "%s: %s" % (type(exc).__name__, exc))
        return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error.as_jsonrpc_error()}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safe external CPython 3 Abaqus automation orchestrator")
    parser.add_argument("--config", required=True, help="Automation JSON configuration")
    parser.add_argument("--request", required=True, help="JSON-RPC request file")
    parser.add_argument("--response", help="New response path; existing files are never replaced")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        config = load_config(Path(args.config).resolve(strict=True))
        request_path = Path(args.request).resolve(strict=True)
        request = read_json(request_path)
        response = handle_jsonrpc(AutomationOrchestrator(config), request)
        if args.response:
            response_path = Path(args.response).absolute()
            response_parent = response_path.parent.resolve(strict=True)
            if not is_within(response_parent, config.workspace_root):
                raise AutomationError("RESPONSE_PATH_DENIED", "Response path must be under workspace_root")
            atomic_write_new_json(response_path, response)
        print(json.dumps(response, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return 0 if "result" in response else 20
    except AutomationError as exc:
        response = {"jsonrpc": JSONRPC_VERSION, "id": None, "error": exc.as_jsonrpc_error()}
        print(json.dumps(response, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
        return 20


if __name__ == "__main__":
    raise SystemExit(main())
