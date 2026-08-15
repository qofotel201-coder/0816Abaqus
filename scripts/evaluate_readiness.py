#!/usr/bin/env python3
"""Combine fresh local evidence into a strict preparation-readiness result."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def row(identifier: str, status: str, reason: str) -> dict[str, Any]:
    return {
        "id": identifier,
        "required": True,
        "status": status,
        "reason": reason,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-manifest", type=Path, required=True)
    parser.add_argument("--phase0-report", type=Path, required=True)
    parser.add_argument("--resource-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.absolute()
    if output.exists():
        raise SystemExit("output already exists: %s" % output)
    bootstrap = read_json(args.bootstrap_manifest.resolve(strict=True))
    phase0 = read_json(args.phase0_report.resolve(strict=True))
    resources = read_json(args.resource_report.resolve(strict=True))

    input_ok = (
        bootstrap.get("source_cae", {}).get("verified") is True
        and bootstrap.get("subroutine", {}).get("verified") is True
        and bootstrap.get("safety", {}).get("solver_execution_enabled") is False
    )
    phase0_ok = (
        phase0.get("status") == "PASS_WITH_NO_SOLVER"
        and phase0.get("solver_submitted") is False
    )
    resource_rows = {
        item.get("id"): item for item in resources.get("checks", [])
    }

    checks = [
        row(
            "INPUT_HASHES",
            "PASS" if input_ok else "FAIL",
            "Private CAE and user subroutine match the frozen manifest."
            if input_ok
            else "One or more private inputs failed hash validation.",
        ),
        row(
            "PYTHON_AND_ABAQUS_PHASE0",
            "PASS" if phase0_ok else "FAIL",
            "Portable Phase 0 checks passed without solver submission."
            if phase0_ok
            else "Portable Phase 0 evidence is missing or failed.",
        ),
    ]
    for identifier in (
        "LOCAL_NTFS",
        "DISK_BUDGET",
        "LOGICAL_CPU_CAPACITY",
        "PHYSICAL_CORE_OVERSUBSCRIPTION",
        "AVAILABLE_MEMORY",
        "CAE_LICENSE_HANDSHAKE",
        "EXPLICIT_SOLVER_TOKEN_CAPACITY",
    ):
        item = resource_rows.get(identifier)
        checks.append(
            row(
                identifier,
                item.get("status", "BLOCKED") if item else "BLOCKED",
                item.get("reason", "Fresh resource evidence is missing.")
                if item
                else "Fresh resource evidence is missing.",
            )
        )
    checks.extend(
        [
            row(
                "DATA_CHECK",
                "BLOCKED",
                "Data Check and user-subroutine compilation are not implemented or authorized in this release.",
            ),
            row(
                "REAL_ODB_EXTRACTION",
                "BLOCKED",
                "No successful solver ODB is available for a positive extraction test.",
            ),
            row(
                "FOUR_CASE_MATRIX",
                "BLOCKED",
                "The B00/B10/B01/B11 cases have not been materialized and compared.",
            ),
        ]
    )

    counts = {
        status: sum(1 for item in checks if item["status"] == status)
        for status in ("PASS", "WARN", "FAIL", "BLOCKED")
    }
    ready = all(item["status"] == "PASS" for item in checks)
    result = {
        "$schema": "../schemas/preparation-readiness.schema.json",
        "schema_version": "1.0.0",
        "generated_at": utc_now(),
        "overall_status": "READY" if ready else "NOT_READY",
        "counts": counts,
        "checks": checks,
        "solver_submitted": False,
    }
    write_json_new(output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if ready else 20


if __name__ == "__main__":
    sys.exit(main())
