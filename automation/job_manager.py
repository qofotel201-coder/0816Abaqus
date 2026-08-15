"""Registered Abaqus job inspection and tightly gated control primitives.

No method in this module launches an Abaqus solver.  Data Check and submit are
hard-disabled in this release even if a configuration flag is accidentally set.
Kill support is two-phase, disabled by default, and requires an immutable job
manifest plus a matching live-process creation token.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time
from typing import Any, Mapping

try:
    from .common import (
        AutomationConfig,
        AutomationError,
        SCHEMA_VERSION,
        atomic_write_new_json,
        bounded_text,
        file_manifest,
        is_within,
        managed_child,
        new_confirmation_token,
        new_uuid,
        object_sha256,
        read_json,
        read_latest_manifest,
        require_bool,
        require_mapping,
        require_sha256,
        require_uuid,
        safe_job_name,
        safe_relative_path,
        sha256_file,
        utc_now,
        verify_confirmation_token,
    )
except ImportError:  # Direct script execution support.
    from common import (  # type: ignore
        AutomationConfig,
        AutomationError,
        SCHEMA_VERSION,
        atomic_write_new_json,
        bounded_text,
        file_manifest,
        is_within,
        managed_child,
        new_confirmation_token,
        new_uuid,
        object_sha256,
        read_json,
        read_latest_manifest,
        require_bool,
        require_mapping,
        require_sha256,
        require_uuid,
        safe_job_name,
        safe_relative_path,
        sha256_file,
        utc_now,
        verify_confirmation_token,
    )


JOB_MANIFEST_STEM = "job.manifest"
ACTIVE_STATES = {"REGISTERED", "QUEUED", "STARTING", "RUNNING", "TERMINATING"}
FINAL_STATES = {"COMPLETED", "FAILED", "ABORTED", "KILLED"}
ARTIFACT_SUFFIXES = (
    ".inp",
    ".for",
    ".f",
    ".f90",
    ".dat",
    ".log",
    ".sta",
    ".msg",
    ".odb",
    ".prt",
    ".sim",
    ".lck",
    ".com",
    ".mdl",
    ".stt",
    ".res",
    ".pac",
    ".sel",
    ".abq",
)


class JobManager:
    def __init__(self, config: AutomationConfig) -> None:
        self.config = config

    def _registered_job(
        self,
        params: Mapping[str, Any],
        require_expected_hash: bool = False,
    ) -> tuple[dict[str, Any], Path, Path, Path, str, int]:
        workspace_id = require_uuid(params.get("workspace_id"), "workspace_id")
        job_id = require_uuid(params.get("job_id"), "job_id")
        workspace_dir = managed_child(self.config.workspace_root, workspace_id)
        if not workspace_dir.is_dir():
            raise AutomationError("WORKSPACE_NOT_FOUND", "Workspace does not exist", {"workspace_id": workspace_id})
        resolved_workspace = workspace_dir.resolve(strict=True)
        if os.path.normcase(str(resolved_workspace)) != os.path.normcase(str(workspace_dir)):
            raise AutomationError("WORKSPACE_SYMLINK_DENIED", "Workspace directory may not be a symlink or junction")
        jobs_root = workspace_dir / "jobs"
        job_dir = jobs_root / job_id
        if job_dir.parent != jobs_root or not is_within(job_dir, workspace_dir):
            raise AutomationError("JOB_PATH_ESCAPE", "Registered job path escaped its workspace")
        if not job_dir.is_dir():
            raise AutomationError("JOB_NOT_REGISTERED", "Job has no registered immutable manifest", {"job_id": job_id})
        resolved_job_dir = job_dir.resolve(strict=True)
        if not is_within(resolved_job_dir, resolved_workspace) or os.path.normcase(str(resolved_job_dir)) != os.path.normcase(str(job_dir)):
            raise AutomationError("JOB_SYMLINK_DENIED", "Registered job directory may not be a symlink or junction")
        manifest, manifest_path, manifest_hash, version = read_latest_manifest(job_dir, JOB_MANIFEST_STEM)
        if manifest.get("schema") != SCHEMA_VERSION:
            raise AutomationError("JOB_SCHEMA_MISMATCH", "Job manifest schema is unsupported")
        if manifest.get("job_id") != job_id or manifest.get("workspace_id") != workspace_id:
            raise AutomationError("JOB_IDENTITY_MISMATCH", "Job manifest identity does not match its directory")
        if require_expected_hash:
            expected = require_sha256(params.get("expected_manifest_sha256"), "expected_manifest_sha256")
            if manifest_hash != expected:
                raise AutomationError(
                    "JOB_MANIFEST_HASH_MISMATCH",
                    "Registered job manifest changed",
                    {"expected": expected, "actual": manifest_hash},
                )
        run_relative = safe_relative_path(manifest.get("run_directory", "run"), "run_directory")
        run_dir = job_dir / run_relative
        if not is_within(run_dir, job_dir):
            raise AutomationError("JOB_RUN_PATH_ESCAPE", "Job run directory escaped its registry")
        if not run_dir.is_dir():
            raise AutomationError("JOB_RUN_DIRECTORY_MISSING", "Registered job run directory does not exist")
        resolved_run_dir = run_dir.resolve(strict=True)
        if not is_within(resolved_run_dir, resolved_job_dir) or os.path.normcase(str(resolved_run_dir)) != os.path.normcase(str(run_dir)):
            raise AutomationError("JOB_RUN_SYMLINK_DENIED", "Job run directory may not be a symlink or junction")
        safe_job_name(manifest.get("job_name"), "manifest job_name")
        return manifest, manifest_path, job_dir, run_dir, manifest_hash, version

    @staticmethod
    def _tail_text(path: Path, max_bytes: int = 65536) -> str:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(-max_bytes, os.SEEK_END)
            raw = stream.read(max_bytes)
        return raw.decode("utf-8", errors="replace")

    @staticmethod
    def _infer_artifact_state(run_dir: Path, job_name: str, process_alive: bool | None) -> tuple[str, list[str]]:
        reasons: list[str] = []
        log_path = run_dir / (job_name + ".log")
        lock_path = run_dir / (job_name + ".lck")
        text = JobManager._tail_text(log_path).lower() if log_path.is_file() else ""
        if "completed" in text and "exited with an error" not in text and "abaqus error" not in text:
            return "COMPLETED", reasons
        if "aborted" in text:
            return "ABORTED", reasons
        if "exited with an error" in text or "abaqus error" in text or "fatal" in text:
            return "FAILED", reasons
        if process_alive is True:
            return "RUNNING", reasons
        if lock_path.is_file():
            reasons.append("lock file exists but registered process is not confirmed alive")
            return "UNKNOWN", reasons
        if process_alive is False:
            reasons.append("registered process is no longer alive and no final log marker was found")
            return "UNKNOWN", reasons
        return "REGISTERED", reasons

    def status(self, params: Mapping[str, Any]) -> dict[str, Any]:
        manifest, manifest_path, _job_dir, run_dir, manifest_hash, version = self._registered_job(params)
        process = manifest.get("process")
        process_status: dict[str, Any] = {"registered": isinstance(process, Mapping)}
        alive: bool | None = None
        if isinstance(process, Mapping) and process.get("pid") is not None:
            try:
                pid = int(process.get("pid"))
                identity = self._process_identity(pid)
                alive = identity is not None
                process_status.update({"pid": pid, "alive": alive})
                if identity:
                    process_status["observed_identity"] = identity
                    expected_start = str(process.get("start_token", ""))
                    process_status["identity_matches"] = (
                        expected_start == identity["start_token"]
                        and self._same_executable(str(process.get("executable", "")), identity["executable"])
                    )
            except (ValueError, TypeError):
                process_status["error"] = "invalid registered pid"
                alive = None
        job_name = safe_job_name(manifest.get("job_name"), "manifest job_name")
        inferred, reasons = self._infer_artifact_state(run_dir, job_name, alive)
        recorded = str(manifest.get("lifecycle_state", "REGISTERED")).upper()
        if recorded in FINAL_STATES:
            state = recorded
            if inferred in FINAL_STATES and inferred != recorded:
                reasons.append("artifact state disagrees with final manifest state")
        else:
            state = inferred
        return {
            "schema": SCHEMA_VERSION,
            "status": state,
            "recorded_status": recorded,
            "job_id": manifest.get("job_id"),
            "workspace_id": manifest.get("workspace_id"),
            "job_name": job_name,
            "process": process_status,
            "reasons": reasons,
            "evidence": {
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_hash,
                "manifest_version": version,
                "run_directory": str(run_dir),
            },
        }

    def artifacts(self, params: Mapping[str, Any]) -> dict[str, Any]:
        manifest, manifest_path, _job_dir, run_dir, manifest_hash, version = self._registered_job(params)
        hash_large = params.get("hash_large", False)
        if not isinstance(hash_large, bool):
            raise AutomationError("INVALID_BOOLEAN", "hash_large must be true or false")
        job_name = safe_job_name(manifest.get("job_name"), "manifest job_name")
        artifacts = []
        for suffix in ARTIFACT_SUFFIXES:
            candidate = run_dir / (job_name + suffix)
            if not candidate.exists():
                continue
            try:
                resolved = candidate.resolve(strict=True)
            except OSError:
                continue
            if not resolved.is_file() or not is_within(resolved, run_dir.resolve(strict=True)):
                artifacts.append({"name": candidate.name, "status": "DENIED_PATH_ESCAPE"})
                continue
            info = file_manifest(resolved, include_sha256=False)
            if info["size"] <= self.config.artifact_hash_limit_bytes or hash_large:
                info["sha256"] = sha256_file(resolved)
                info["hash_status"] = "COMPUTED"
            else:
                info["sha256"] = None
                info["hash_status"] = "SKIPPED_SIZE_LIMIT"
            info["kind"] = suffix[1:].upper()
            artifacts.append(info)
        return {
            "schema": SCHEMA_VERSION,
            "status": "OK",
            "job_id": manifest.get("job_id"),
            "workspace_id": manifest.get("workspace_id"),
            "artifacts": artifacts,
            "evidence": {
                "manifest_path": str(manifest_path),
                "manifest_sha256": manifest_hash,
                "manifest_version": version,
            },
        }

    @staticmethod
    def _same_executable(expected: str, observed: str) -> bool:
        if not expected or not observed:
            return False
        return os.path.normcase(os.path.abspath(expected)) == os.path.normcase(os.path.abspath(observed))

    @staticmethod
    def _process_identity(pid: int) -> dict[str, str] | None:
        if pid <= 0:
            return None
        if os.name == "nt":
            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = (
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
                ctypes.POINTER(wintypes.FILETIME),
            )
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.QueryFullProcessImageNameW.argtypes = (
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            )
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return None
            try:
                creation = wintypes.FILETIME()
                exit_time = wintypes.FILETIME()
                kernel_time = wintypes.FILETIME()
                user_time = wintypes.FILETIME()
                if not kernel32.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                    return None
                buffer = ctypes.create_unicode_buffer(32768)
                length = wintypes.DWORD(len(buffer))
                if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
                    return None
                start_token = str((creation.dwHighDateTime << 32) | creation.dwLowDateTime)
                return {"start_token": start_token, "executable": buffer.value}
            finally:
                kernel32.CloseHandle(handle)

        proc_root = Path("/proc") / str(pid)
        stat_path = proc_root / "stat"
        if not stat_path.is_file():
            return None
        try:
            stat_text = stat_path.read_text(encoding="utf-8")
            closing = stat_text.rfind(")")
            fields = stat_text[closing + 2 :].split()
            start_token = fields[19]
            executable = os.readlink(str(proc_root / "exe"))
            return {"start_token": start_token, "executable": executable}
        except (OSError, IndexError, ValueError):
            return None

    def _validate_process_ownership(
        self,
        manifest: Mapping[str, Any],
        job_dir: Path,
    ) -> tuple[int, dict[str, str]]:
        process = require_mapping(manifest.get("process"), "registered process")
        try:
            pid = int(process.get("pid"))
        except (ValueError, TypeError):
            raise AutomationError("INVALID_REGISTERED_PID", "Registered job PID is invalid")
        expected_start = bounded_text(process.get("start_token"), "process.start_token", 128)
        expected_executable = bounded_text(process.get("executable"), "process.executable", 32767)
        marker_relative = safe_relative_path(process.get("owner_marker"), "process.owner_marker")
        marker_path = job_dir / marker_relative
        if not marker_path.is_file() or not is_within(marker_path.resolve(strict=True), job_dir.resolve(strict=True)):
            raise AutomationError("OWNER_MARKER_MISSING", "Registered process owner marker is missing")
        marker_hash = require_sha256(process.get("owner_marker_sha256"), "process.owner_marker_sha256")
        if sha256_file(marker_path) != marker_hash:
            raise AutomationError("OWNER_MARKER_HASH_MISMATCH", "Registered process owner marker changed")
        marker = require_mapping(read_json(marker_path), "owner marker")
        if marker.get("job_id") != manifest.get("job_id") or marker.get("workspace_id") != manifest.get("workspace_id"):
            raise AutomationError("OWNER_MARKER_IDENTITY_MISMATCH", "Owner marker does not identify this job")
        observed = self._process_identity(pid)
        if observed is None:
            raise AutomationError("PROCESS_NOT_RUNNING", "Registered process is not running")
        if observed["start_token"] != expected_start or not self._same_executable(expected_executable, observed["executable"]):
            raise AutomationError(
                "PROCESS_IDENTITY_MISMATCH",
                "PID was reused or executable identity changed",
                {"registered": {"start_token": expected_start, "executable": expected_executable}, "observed": observed},
            )
        return pid, observed

    def kill_plan(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.config.allow_job_kill:
            raise AutomationError("OPERATION_GATED", "Job termination is disabled by configuration")
        manifest, manifest_path, job_dir, run_dir, manifest_hash, version = self._registered_job(params, require_expected_hash=True)
        state = str(manifest.get("lifecycle_state", "REGISTERED")).upper()
        if state not in ACTIVE_STATES:
            raise AutomationError("JOB_NOT_ACTIVE", "Only an active registered job may be terminated", {"state": state})
        reason = bounded_text(params.get("reason"), "reason", 500)
        force = params.get("force", False)
        if not isinstance(force, bool):
            raise AutomationError("INVALID_BOOLEAN", "force must be true or false")
        pid, identity = self._validate_process_ownership(manifest, job_dir)
        plan_id = new_uuid()
        operation_id = new_uuid()
        token, token_hash = new_confirmation_token()
        control_dir = job_dir / "control"
        control_dir.mkdir(parents=False, exist_ok=True)
        resolved_control = control_dir.resolve(strict=True)
        if not is_within(resolved_control, job_dir.resolve(strict=True)) or os.path.normcase(str(resolved_control)) != os.path.normcase(str(control_dir)):
            raise AutomationError("CONTROL_PATH_DENIED", "Job control directory may not be a symlink or junction")
        plan = {
            "schema": SCHEMA_VERSION,
            "kind": "job.kill",
            "plan_id": plan_id,
            "operation_id": operation_id,
            "created_at": utc_now(),
            "expires_at_epoch": int(time.time()) + min(self.config.plan_ttl_seconds, 900),
            "workspace_id": manifest.get("workspace_id"),
            "job_id": manifest.get("job_id"),
            "job_name": manifest.get("job_name"),
            "manifest_sha256": manifest_hash,
            "manifest_version": version,
            "pid": pid,
            "process_identity": identity,
            "run_directory": str(run_dir),
            "reason": reason,
            "force": force,
            "confirmation_token_sha256": token_hash,
        }
        plan_hash = object_sha256(plan)
        plan_path = control_dir / ("kill-plan-" + plan_id + ".json")
        atomic_write_new_json(plan_path, plan)
        return {
            "schema": SCHEMA_VERSION,
            "status": "PLANNED",
            "plan_id": plan_id,
            "operation_id": operation_id,
            "plan_sha256": plan_hash,
            "confirmation_token": token,
            "target": {"job_id": manifest.get("job_id"), "pid": pid, "process_identity": identity},
            "evidence": {"plan_path": str(plan_path), "plan_file_sha256": sha256_file(plan_path), "job_manifest_path": str(manifest_path)},
            "gate": {"status": "GO", "reasons": []},
        }

    def kill_commit(self, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self.config.allow_job_kill:
            raise AutomationError("OPERATION_GATED", "Job termination is disabled by configuration")
        manifest, _manifest_path, job_dir, run_dir, manifest_hash, _version = self._registered_job(params, require_expected_hash=True)
        plan_id = require_uuid(params.get("plan_id"), "plan_id")
        operation_id = require_uuid(params.get("operation_id"), "operation_id")
        plan_hash = require_sha256(params.get("plan_sha256"), "plan_sha256")
        plan_path = job_dir / "control" / ("kill-plan-" + plan_id + ".json")
        if not plan_path.is_file():
            raise AutomationError("PLAN_NOT_FOUND", "Kill plan does not exist")
        plan = require_mapping(read_json(plan_path), "kill plan")
        if object_sha256(plan) != plan_hash:
            raise AutomationError("PLAN_HASH_MISMATCH", "Kill plan changed")
        if plan.get("kind") != "job.kill" or plan.get("operation_id") != operation_id:
            raise AutomationError("PLAN_CONTRACT_MISMATCH", "Kill plan identity is invalid")
        if plan.get("manifest_sha256") != manifest_hash:
            raise AutomationError("JOB_MANIFEST_CHANGED", "Job manifest changed after kill.plan")
        if int(plan.get("expires_at_epoch", 0)) < int(time.time()):
            raise AutomationError("PLAN_EXPIRED", "Kill plan expired")
        verify_confirmation_token(params.get("confirmation_token"), plan.get("confirmation_token_sha256"))
        pid, identity = self._validate_process_ownership(manifest, job_dir)
        if pid != int(plan.get("pid")) or identity != plan.get("process_identity"):
            raise AutomationError("PROCESS_CHANGED_AFTER_PLAN", "Process identity changed after kill.plan")

        claim_path = job_dir / "control" / ("kill-claim-" + plan_id + ".json")
        atomic_write_new_json(
            claim_path,
            {"schema": SCHEMA_VERSION, "plan_id": plan_id, "operation_id": operation_id, "claimed_at": utc_now()},
        )
        if os.name != "nt":
            raise AutomationError("UNSUPPORTED_PLATFORM", "Safe process-tree termination is implemented only for Windows")
        command = ["taskkill.exe", "/PID", str(pid), "/T"]
        if plan.get("force") is True:
            command.append("/F")
        try:
            completed = subprocess.run(
                command,
                cwd=str(run_dir),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                shell=False,
                timeout=30,
                check=False,
            )
            output = completed.stdout.decode("utf-8", errors="replace")[-8192:]
            return_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b"").decode("utf-8", errors="replace")[-8192:]
            return_code = 124
        receipt = {
            "schema": SCHEMA_VERSION,
            "kind": "job.kill.receipt",
            "plan_id": plan_id,
            "operation_id": operation_id,
            "completed_at": utc_now(),
            "job_id": manifest.get("job_id"),
            "pid": pid,
            "return_code": return_code,
            "output_tail": output,
            "force": plan.get("force") is True,
        }
        receipt_path = job_dir / "control" / ("kill-receipt-" + plan_id + ".json")
        atomic_write_new_json(receipt_path, receipt)
        return {
            "schema": SCHEMA_VERSION,
            "status": "TERMINATION_REQUESTED" if return_code == 0 else "TERMINATION_FAILED",
            "return_code": return_code,
            "job_id": manifest.get("job_id"),
            "pid": pid,
            "evidence": {"receipt_path": str(receipt_path), "receipt_sha256": sha256_file(receipt_path), "claim_path": str(claim_path)},
            "gate": {"status": "GO" if return_code == 0 else "NO_GO", "reasons": [] if return_code == 0 else ["taskkill returned nonzero"]},
        }

    def _solver_operation_gated(self, operation: str, params: Mapping[str, Any]) -> dict[str, Any]:
        # Require an immutable registered manifest and explicit acknowledgements
        # before reporting the deliberate hard gate.  No process is constructed.
        _manifest, manifest_path, _job_dir, _run_dir, manifest_hash, version = self._registered_job(params, require_expected_hash=True)
        acknowledgements = (
            ("confirm_registered_manifest", params.get("confirm_registered_manifest")),
            ("allow_native_code", params.get("allow_native_code")),
            ("allow_solver_execution", params.get("allow_solver_execution")),
        )
        for field, value in acknowledgements:
            if require_bool(value, field) is not True:
                raise AutomationError("EXPLICIT_ACK_REQUIRED", "%s must explicitly equal true" % field)
        if not self.config.solver_execution_enabled:
            raise AutomationError(
                "SOLVER_EXECUTION_GATED",
                "%s is disabled by configuration; no solver was started" % operation,
                {"manifest_path": str(manifest_path), "manifest_sha256": manifest_hash, "manifest_version": version},
            )
        raise AutomationError(
            "SOLVER_EXECUTION_NOT_IMPLEMENTED",
            "%s remains hard-disabled in this release; no solver was started" % operation,
            {"manifest_path": str(manifest_path), "manifest_sha256": manifest_hash, "manifest_version": version},
        )

    def data_check_gated(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._solver_operation_gated("Data Check", params)

    def submit_gated(self, params: Mapping[str, Any]) -> dict[str, Any]:
        return self._solver_operation_gated("submit", params)
