#!/usr/bin/env python3
"""Create one immutable-source workspace with the two-phase protocol.

The command copies the frozen CAE into a new UUID directory. It does not open
Abaqus, alter a model, run Data Check, or start a solver.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from automation.common import load_config, read_json, sha256_file  # noqa: E402
from automation.orchestrator import AutomationOrchestrator  # noqa: E402


EXPECTED_SOURCE_SHA256 = "a32e7a72293fbf3e1b803d8b820965ce82e78f74a811729a613740c5c40029ac"


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--automation-config",
        type=Path,
        default=REPOSITORY_ROOT / "config" / "local" / "automation.json",
    )
    parser.add_argument("--source-cae", type=Path, required=True)
    parser.add_argument("--label", default="portable-phase0-workspace")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output = args.output.absolute()
    if output.exists():
        raise SystemExit("output already exists: %s" % output)

    source = args.source_cae.resolve(strict=True)
    before_stat = source.stat()
    before_hash = sha256_file(source)
    if before_hash != EXPECTED_SOURCE_SHA256:
        raise SystemExit("frozen source CAE hash mismatch")

    config = load_config(args.automation_config.resolve(strict=True))
    orchestrator = AutomationOrchestrator(config)
    planned = orchestrator.workspace_plan(
        {
            "source_cae": str(source),
            "expected_source_sha256": EXPECTED_SOURCE_SHA256,
            "label": args.label,
        }
    )
    plan = read_json(Path(planned["evidence"]["plan_path"]))
    created = orchestrator.workspace_create(
        {
            "plan_id": planned["plan_id"],
            "plan_sha256": planned["plan_sha256"],
            "operation_id": plan["operation_id"],
            "confirmation_token": planned["confirmation_token"],
        }
    )
    inspected = orchestrator.workspace_inspect(
        {"workspace_id": planned["workspace_id"]}
    )

    after_stat = source.stat()
    after_hash = sha256_file(source)
    source_unchanged = (
        before_hash == after_hash == EXPECTED_SOURCE_SHA256
        and before_stat.st_size == after_stat.st_size
        and before_stat.st_mtime_ns == after_stat.st_mtime_ns
    )
    planned_public = dict(planned)
    planned_public.pop("confirmation_token", None)
    passed = (
        source_unchanged
        and created.get("status") == "CREATED"
        and inspected.get("status") == "READY"
        and inspected.get("gate", {}).get("status") == "GO"
    )
    report = {
        "schema_version": "1.0.0",
        "scope": "PORTABLE_WORKSPACE_CREATE_NO_SOLVER",
        "source": {
            "path": str(source),
            "sha256_before": before_hash,
            "sha256_after": after_hash,
            "size_before": before_stat.st_size,
            "size_after": after_stat.st_size,
            "mtime_ns_before": before_stat.st_mtime_ns,
            "mtime_ns_after": after_stat.st_mtime_ns,
            "unchanged": source_unchanged,
        },
        "planned": planned_public,
        "created": created,
        "inspected": inspected,
        "solver_submitted": False,
        "passed": passed,
    }
    write_json_new(output, report)
    print(
        json.dumps(
            {
                "passed": passed,
                "workspace_id": planned["workspace_id"],
                "workspace_dir": created.get("workspace_dir"),
                "output": str(output),
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
