#!/usr/bin/env python3
"""Generate a fresh Windows resource report without starting a solver."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any


GIB = 1024 ** 3


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def windows_hardware(drive_letter: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z]", drive_letter):
        raise RuntimeError("runtime root must be on a local drive-letter volume")
    command = (
        "$c=Get-CimInstance Win32_Processor; "
        "$o=Get-CimInstance Win32_OperatingSystem; "
        "$v=Get-Volume -DriveLetter '%s'; "
        "[pscustomobject]@{"
        "PhysicalCores=($c|Measure-Object NumberOfCores -Sum).Sum; "
        "LogicalProcessors=($c|Measure-Object NumberOfLogicalProcessors -Sum).Sum; "
        "TotalMemoryBytes=[int64]$o.TotalVisibleMemorySize*1KB; "
        "FreeMemoryBytes=[int64]$o.FreePhysicalMemory*1KB; "
        "FileSystem=$v.FileSystem; "
        "DriveLetter=$v.DriveLetter"
        "} | ConvertTo-Json -Compress"
    ) % drive_letter.upper()
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError("Windows hardware query failed: %s" % completed.stderr[-2000:])
    return json.loads(completed.stdout)


def check(
    identifier: str,
    status: str,
    reason: str,
    **observations: Any,
) -> dict[str, Any]:
    return {
        "id": identifier,
        "status": status,
        "reason": reason,
        **observations,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--phase0-report", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--requested-cpus", type=int, default=18)
    parser.add_argument("--minimum-memory-gib", type=float, default=8.0)
    parser.add_argument("--storage-budget-gib", type=float, default=140.0)
    return parser.parse_args()


def main() -> int:
    if os.name != "nt":
        raise SystemExit("Run resource_preflight.py with native Windows CPython")
    args = parse_args()
    output = args.output.absolute()
    if output.exists():
        raise SystemExit("output already exists: %s" % output)
    runtime_root = args.runtime_root.resolve(strict=True)
    if runtime_root.drive.startswith("\\\\") or not runtime_root.drive:
        raise SystemExit("runtime root must be a local drive-letter path")

    hardware = windows_hardware(runtime_root.drive[0])
    disk = shutil.disk_usage(runtime_root)
    required_memory = int(args.minimum_memory_gib * GIB)
    required_storage = int(args.storage_budget_gib * GIB)
    requested_cpus = args.requested_cpus
    if requested_cpus < 1:
        raise SystemExit("--requested-cpus must be positive")

    checks = [
        check(
            "LOCAL_NTFS",
            "PASS" if str(hardware.get("FileSystem", "")).upper() == "NTFS" else "FAIL",
            "Runtime artifacts require a local NTFS volume.",
            observed=hardware.get("FileSystem"),
        ),
        check(
            "DISK_BUDGET",
            "PASS" if disk.free >= required_storage else "FAIL",
            "Free space must cover staged CAE, short runs, four ODBs, and reserve.",
            observed_free_bytes=disk.free,
            required_bytes=required_storage,
        ),
        check(
            "LOGICAL_CPU_CAPACITY",
            "PASS" if int(hardware["LogicalProcessors"]) >= requested_cpus else "FAIL",
            "Logical processor capacity for the configured domain count.",
            observed=int(hardware["LogicalProcessors"]),
            requested=requested_cpus,
        ),
        check(
            "PHYSICAL_CORE_OVERSUBSCRIPTION",
            "PASS" if int(hardware["PhysicalCores"]) >= requested_cpus else "WARN",
            "WARN means domains exceed physical cores but fit logical processors.",
            observed=int(hardware["PhysicalCores"]),
            requested=requested_cpus,
        ),
        check(
            "AVAILABLE_MEMORY",
            "PASS" if int(hardware["FreeMemoryBytes"]) >= required_memory else "FAIL",
            "Available memory is checked at execution time and may change.",
            observed_bytes=int(hardware["FreeMemoryBytes"]),
            required_bytes=required_memory,
        ),
    ]

    phase0 = None
    cae_status = "BLOCKED"
    cae_reason = "No fresh Phase 0 report was supplied."
    if args.phase0_report:
        phase0 = read_json(args.phase0_report.resolve(strict=True))
        if (
            phase0.get("status") == "PASS_WITH_NO_SOLVER"
            and phase0.get("solver_submitted") is False
        ):
            cae_status = "PASS"
            cae_reason = "Fresh Phase 0 report proves the CAE worker returned successfully."
        else:
            cae_status = "FAIL"
            cae_reason = "The supplied Phase 0 report did not pass."
    checks.extend(
        [
            check("CAE_LICENSE_HANDSHAKE", cae_status, cae_reason),
            check(
                "EXPLICIT_SOLVER_TOKEN_CAPACITY",
                "BLOCKED",
                "Not measured because this release never starts Data Check or a solver.",
            ),
        ]
    )

    blockers = [
        row for row in checks if row["status"] in {"FAIL", "BLOCKED"}
    ]
    report = {
        "schema_version": "1.0.0",
        "generated_at": utc_now(),
        "scope": "PORTABLE_MACHINE_RESOURCE_PREFLIGHT_NO_SOLVER",
        "runtime_root": str(runtime_root),
        "hardware": hardware,
        "disk": {
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
        },
        "requested": {
            "cpus": requested_cpus,
            "minimum_available_memory_bytes": required_memory,
            "storage_budget_bytes": required_storage,
        },
        "checks": checks,
        "gate": "NO_GO" if blockers else "GO",
        "blocking_reasons": [row["id"] for row in blockers],
        "solver_submitted": False,
    }
    write_json_new(output, report)
    print(
        json.dumps(
            {
                "gate": report["gate"],
                "blocking_reasons": report["blocking_reasons"],
                "output": str(output),
                "solver_submitted": False,
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )
    return 0 if report["gate"] == "GO" else 20


if __name__ == "__main__":
    sys.exit(main())
