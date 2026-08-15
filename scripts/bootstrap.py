#!/usr/bin/env python3
"""Generate machine-local, no-overwrite configuration for this repository.

Run this script with 64-bit Windows CPython 3.10 or newer. It verifies the
frozen CAE and optional source archive before writing configuration below
config/local. It never opens Abaqus and never starts a solver.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
from typing import Any


EXPECTED_CAE_SHA256 = "a32e7a72293fbf3e1b803d8b820965ce82e78f74a811729a613740c5c40029ac"
EXPECTED_CAE_SIZE = 1_262_108_672
EXPECTED_ARCHIVE_SHA256 = "79cd1a86152ebcacb7b53b891f82a8e25f9d8427137964fc2c30bb261a5e7adb"
EXPECTED_ARCHIVE_SIZE = 541_029_107
EXPECTED_FOR_SHA256 = "4889db06c2cf1f477cecaa7baf0ceabf6b5e8279f903fc080cf9d69edf3cfd43"
EXPECTED_FOR_SIZE = 17_438

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR = REPOSITORY_ROOT / "config" / "local"


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def local_path(value: str, field: str, *, must_exist: bool = True) -> Path:
    if value.replace("/", "\\").startswith("\\\\"):
        raise SystemExit("%s may not be a UNC/network path" % field)
    path = Path(value).expanduser()
    try:
        return path.resolve(strict=must_exist)
    except OSError as exc:
        raise SystemExit("%s is unavailable: %s" % (field, exc))


def is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath(
            (os.path.normcase(str(path)), os.path.normcase(str(root)))
        ) == os.path.normcase(str(root))
    except ValueError:
        return False


def verify_file(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    label: str,
) -> dict[str, Any]:
    if not path.is_file():
        raise SystemExit("%s is not a regular file: %s" % (label, path))
    observed_size = path.stat().st_size
    if observed_size != expected_size:
        raise SystemExit(
            "%s size mismatch: expected %d, observed %d"
            % (label, expected_size, observed_size)
        )
    observed_hash = sha256_file(path)
    if observed_hash != expected_sha256:
        raise SystemExit(
            "%s SHA-256 mismatch: expected %s, observed %s"
            % (label, expected_sha256, observed_hash)
        )
    return {
        "path": str(path),
        "size_bytes": observed_size,
        "sha256": observed_hash,
        "verified": True,
    }


def write_json_new(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create portable local configuration without opening Abaqus."
    )
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--source-cae", required=True)
    parser.add_argument("--abaqus-command", required=True)
    parser.add_argument("--source-archive")
    parser.add_argument("--subroutine", required=True)
    parser.add_argument("--config-dir", default=str(DEFAULT_CONFIG_DIR))
    return parser.parse_args()


def main() -> int:
    if sys.version_info < (3, 10):
        raise SystemExit("CPython 3.10 or newer is required")
    if os.name != "nt":
        raise SystemExit(
            "Run bootstrap.py with native Windows CPython; WSL/Linux Python is not supported."
        )

    args = parse_args()
    runtime_root = local_path(args.runtime_root, "runtime root", must_exist=False)
    source_cae = local_path(args.source_cae, "source CAE")
    abaqus_command = local_path(args.abaqus_command, "Abaqus command")
    subroutine = local_path(args.subroutine, "user subroutine")
    config_dir = local_path(args.config_dir, "configuration directory", must_exist=False)

    if source_cae.suffix.lower() != ".cae":
        raise SystemExit("--source-cae must end in .cae")
    if abaqus_command.suffix.lower() not in (".bat", ".cmd"):
        raise SystemExit("--abaqus-command must identify a .bat or .cmd launcher")
    if is_within(runtime_root, source_cae.parent) or is_within(
        source_cae.parent, runtime_root
    ):
        raise SystemExit("runtime root and frozen source directory must be disjoint")

    cae_manifest = verify_file(
        source_cae,
        expected_size=EXPECTED_CAE_SIZE,
        expected_sha256=EXPECTED_CAE_SHA256,
        label="frozen source CAE",
    )
    fortran_manifest = verify_file(
        subroutine,
        expected_size=EXPECTED_FOR_SIZE,
        expected_sha256=EXPECTED_FOR_SHA256,
        label="Phase 0 user subroutine",
    )
    archive_manifest = None
    if args.source_archive:
        source_archive = local_path(args.source_archive, "source archive")
        archive_manifest = verify_file(
            source_archive,
            expected_size=EXPECTED_ARCHIVE_SIZE,
            expected_sha256=EXPECTED_ARCHIVE_SHA256,
            label="source archive",
        )

    directories = {
        name: runtime_root / name
        for name in (
            "bridge-runs",
            "workspaces",
            "cases",
            "results",
            "reports",
        )
    }
    runtime_root.mkdir(parents=True, exist_ok=True)
    for directory in directories.values():
        directory.mkdir(exist_ok=True)

    bridge_config = {
        "abaqus_release": "2022",
        "abaqus_command": str(abaqus_command),
        "worker": str(REPOSITORY_ROOT / "abaqus_worker.py"),
        "workers": {
            "summary": str(REPOSITORY_ROOT / "abaqus_worker.py"),
            "audit": str(REPOSITORY_ROOT / "automation" / "cae_audit_worker.py"),
            "workspace": str(REPOSITORY_ROOT / "automation" / "workspace_worker.py"),
            "odb": str(REPOSITORY_ROOT / "automation" / "odb_worker.py"),
        },
        "run_root": str(directories["bridge-runs"]),
        "allowed_cae_roots": [
            str(source_cae.parent),
            str(directories["workspaces"]),
            str(directories["cases"]),
        ],
        "timeouts": {
            "ping": 180,
            "list_models": 600,
            "model_summary": 600,
            "deep_audit": 900,
        },
        "hard_timeout_seconds": 900,
        "max_response_bytes": 5_242_880,
        "keep_staged_cae": False,
    }
    automation_config = {
        "schema_version": "abaqus-automation/1.0",
        "allowed_source_roots": [
            str(source_cae.parent),
            str(directories["results"]),
            str(directories["cases"]),
        ],
        "allowed_subroutine_roots": [str(subroutine.parent)],
        "workspace_root": str(directories["workspaces"]),
        "plan_ttl_seconds": 1800,
        "max_subroutine_bytes": 10_485_760,
        "artifact_hash_limit_bytes": 268_435_456,
        "allow_job_kill": False,
        "solver_execution_enabled": False,
    }
    local_manifest = {
        "schema_version": "1.0.0",
        "generated_at": utc_now(),
        "generator": "scripts/bootstrap.py",
        "machine": {
            "hostname": platform.node(),
            "platform": platform.platform(),
            "python_executable": sys.executable,
            "python_version": sys.version.replace("\n", " "),
        },
        "runtime_root": str(runtime_root),
        "abaqus_command": str(abaqus_command),
        "source_cae": cae_manifest,
        "source_archive": archive_manifest,
        "subroutine": fortran_manifest,
        "safety": {
            "solver_execution_enabled": False,
            "job_kill_enabled": False,
            "source_open_policy": "MANDATORY_REQUEST_OWNED_STAGED_COPY",
        },
    }

    targets = {
        config_dir / "abaqus_bridge.json": bridge_config,
        config_dir / "automation.json": automation_config,
        config_dir / "source-manifest.json": local_manifest,
    }
    existing = [str(path) for path in targets if path.exists()]
    if existing:
        raise SystemExit(
            "Refusing to overwrite existing local configuration: %s"
            % ", ".join(existing)
        )
    for path, value in targets.items():
        write_json_new(path, value)

    print(
        json.dumps(
            {
                "status": "BOOTSTRAPPED_NO_SOLVER",
                "config_dir": str(config_dir),
                "files": [str(path) for path in targets],
                "source_cae_sha256": cae_manifest["sha256"],
                "subroutine_sha256": fortran_manifest["sha256"],
            },
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
