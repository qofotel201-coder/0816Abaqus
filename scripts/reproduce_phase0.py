#!/usr/bin/env python3
"""Reproduce the safe Phase 0 connection and model-audit checks.

This script runs unit tests and bounded Abaqus/CAE workers. It never performs
Data Check, compiles a subroutine, or starts an Explicit solver analysis.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from abaqus_bridge import load_config, run_rpc  # noqa: E402


EXPECTED_CAE_SHA256 = "a32e7a72293fbf3e1b803d8b820965ce82e78f74a811729a613740c5c40029ac"
EXPECTED_MODEL_FINGERPRINT = "ad20e17095b3c44b283c9753558c32b75c73a5467692306d971d79377a6c1c0d"
DEFAULT_MODEL = "Model-driven-pile-all-18"
DEFAULT_JOB = "Job-5"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def worker_call(
    base_config: dict[str, Any],
    worker_name: str,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float,
    hash_cae: bool = False,
) -> tuple[dict[str, Any], int]:
    workers = base_config.get("workers")
    if not isinstance(workers, dict) or worker_name not in workers:
        raise RuntimeError("bridge configuration is missing workers.%s" % worker_name)
    call_config = dict(base_config)
    call_config["worker"] = str(workers[worker_name])
    call_config["keep_staged_cae"] = False
    return run_rpc(
        call_config,
        method,
        dict(params),
        timeout_seconds=timeout,
        stage_copy=bool(params.get("cae_path")),
        hash_cae=hash_cae,
    )


def require_success(name: str, response: dict[str, Any], exit_code: int) -> None:
    if exit_code != 0 or "result" not in response:
        raise RuntimeError(
            "%s failed: %s"
            % (name, json.dumps(response, ensure_ascii=False, allow_nan=False))
        )


def source_integrity(response: dict[str, Any]) -> bool:
    bridge = response.get("_bridge", {})
    source = bridge.get("source_cae") if isinstance(bridge, dict) else None
    return bool(
        isinstance(source, dict)
        and source.get("source_unchanged") is True
        and source.get("sha256") == EXPECTED_CAE_SHA256
        and source.get("sha256_after") == EXPECTED_CAE_SHA256
        and source.get("staged_removed") is True
    )


def run_unit_tests() -> dict[str, Any]:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-v",
        ],
        cwd=str(REPOSITORY_ROOT),
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    output = completed.stdout
    return {
        "return_code": completed.returncode,
        "passed": completed.returncode == 0,
        "output_tail": output[-50_000:],
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bridge-config",
        type=Path,
        default=REPOSITORY_ROOT / "config" / "local" / "abaqus_bridge.json",
    )
    parser.add_argument("--source-cae", type=Path, required=True)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--job", default=DEFAULT_JOB)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=900.0)
    return parser.parse_args()


def main() -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("CPython 3.10 or newer is required")
    if os.name != "nt":
        raise SystemExit(
            "Run reproduce_phase0.py with native Windows CPython, not WSL/Linux Python."
        )

    args = parse_args()
    output = args.output.absolute()
    if output.exists():
        raise SystemExit("output already exists: %s" % output)
    source_cae = args.source_cae.resolve(strict=True)
    if sha256_file(source_cae) != EXPECTED_CAE_SHA256:
        raise SystemExit("frozen source CAE SHA-256 does not match the project manifest")

    config = load_config(args.bridge_config.resolve(strict=True))
    started = utc_now()
    checks: list[dict[str, Any]] = []
    evidence: dict[str, Any] = {}

    unit = run_unit_tests()
    evidence["unit_tests"] = unit
    checks.append(
        {
            "id": "PYTHON_CONTRACT_TESTS",
            "status": "PASS" if unit["passed"] else "FAIL",
        }
    )

    ping, ping_code = worker_call(
        config, "summary", "ping", {}, timeout=min(args.timeout, 180.0)
    )
    require_success("Abaqus ping", ping, ping_code)
    evidence["ping"] = {
        "bridge": ping.get("_bridge"),
        "result": ping.get("result"),
    }
    ping_ok = (
        ping["result"].get("connected") is True
        and "2.7" in str(ping["result"].get("python_version", ""))
    )
    checks.append(
        {
            "id": "ABAQUS_2022_PYTHON27_CONNECTION",
            "status": "PASS" if ping_ok else "FAIL",
        }
    )

    summary, summary_code = worker_call(
        config,
        "summary",
        "model_summary",
        {
            "cae_path": str(source_cae),
            "model_name": args.model,
            "max_items": 200,
        },
        timeout=min(args.timeout, 600.0),
        hash_cae=True,
    )
    require_success("model summary", summary, summary_code)
    summary_ok = (
        summary["result"].get("model_name") == args.model
        and args.job in summary["result"].get("jobs_for_model", {}).get("items", [])
        and source_integrity(summary)
    )
    evidence["model_summary"] = summary
    checks.append(
        {
            "id": "STAGED_MODEL_SUMMARY_AND_SOURCE_INTEGRITY",
            "status": "PASS" if summary_ok else "FAIL",
        }
    )

    audit, audit_code = worker_call(
        config,
        "audit",
        "model.deep_audit",
        {
            "cae_path": str(source_cae),
            "model_name": args.model,
            "staged_copy_confirmed": True,
            "max_items": 500,
            "max_table_rows": 1000,
            "max_keyword_blocks": 1000,
            "max_keyword_chars": 1_048_576,
        },
        timeout=args.timeout,
        hash_cae=True,
    )
    require_success("deep model audit", audit, audit_code)
    fingerprint = audit["result"].get("model_fingerprint_sha256")
    audit_jobs = [row.get("name") for row in audit["result"].get("jobs_for_model", [])]
    audit_ok = (
        fingerprint == EXPECTED_MODEL_FINGERPRINT
        and args.job in audit_jobs
        and source_integrity(audit)
    )
    evidence["deep_audit"] = {
        "bridge": audit.get("_bridge"),
        "model_name": audit["result"].get("model_name"),
        "model_fingerprint_sha256": fingerprint,
        "jobs_for_model": audit_jobs,
        "keyword_audit": audit["result"].get("keyword_audit"),
    }
    checks.append(
        {
            "id": "DEEP_MODEL_FINGERPRINT",
            "status": "PASS" if audit_ok else "FAIL",
            "observed": fingerprint,
            "expected": EXPECTED_MODEL_FINGERPRINT,
        }
    )

    workspace, workspace_code = worker_call(
        config,
        "workspace",
        "system.capabilities",
        {},
        timeout=min(args.timeout, 180.0),
    )
    require_success("workspace worker capabilities", workspace, workspace_code)
    workspace_ok = (
        workspace["result"].get("connected") is True
        and workspace["result"].get("submits_solver_jobs") is False
    )
    evidence["workspace_worker"] = workspace
    checks.append(
        {
            "id": "WORKSPACE_WORKER_NO_SOLVER",
            "status": "PASS" if workspace_ok else "FAIL",
        }
    )

    missing_odb = source_cae.parent / "__phase0_expected_missing__.odb"
    odb, odb_code = worker_call(
        config,
        "odb",
        "inventory",
        {
            "odb_path": str(missing_odb),
            "max_steps": 10,
            "max_regions_per_step": 10,
            "max_variables_per_region": 10,
        },
        timeout=min(args.timeout, 180.0),
    )
    odb_ok = (
        odb_code == 20
        and odb.get("error", {}).get("message") == "ODB_NOT_FOUND"
    )
    evidence["odb_access_negative_contract"] = odb
    checks.append(
        {
            "id": "ODB_ACCESS_LOADED_AND_MISSING_FILE_REJECTED",
            "status": "PASS" if odb_ok else "FAIL",
        }
    )

    passed = all(row["status"] == "PASS" for row in checks)
    report = {
        "schema_version": "1.0.0",
        "scope": "PORTABLE_PHASE0_NO_SOLVER",
        "started_at": started,
        "completed_at": utc_now(),
        "source_cae": {
            "path": str(source_cae),
            "sha256": EXPECTED_CAE_SHA256,
        },
        "model": args.model,
        "job": args.job,
        "solver_submitted": False,
        "checks": checks,
        "status": "PASS_WITH_NO_SOLVER" if passed else "FAIL",
        "next_gate": "DATA_CHECK_REMAINS_DISABLED",
        "evidence": evidence,
    }
    write_json_new(output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "output": str(output),
                "checks": checks,
                "solver_submitted": False,
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )
    return 0 if passed else 20


if __name__ == "__main__":
    raise SystemExit(main())
