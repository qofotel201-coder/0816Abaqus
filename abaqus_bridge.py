#!/usr/bin/env python3
"""External CPython 3 broker for Abaqus/CAE 2022.

The broker launches a short-lived Abaqus/CAE noGUI worker and exchanges a
JSON-RPC request/response pair.  CAE files are always opened from an isolated
per-request copy because Abaqus ``openMdb`` can change a file even when the
worker does not call ``save``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid


BRIDGE_VERSION = "1.1.1"
PROTOCOL_VERSION = "2.0"
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = SCRIPT_DIR / "abaqus_bridge_config.json"


class BridgeError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 125) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise BridgeError("JSON root must be an object: %s" % path, 10)
    return value


def load_config(path: Path) -> dict:
    config = _load_json(path)
    required = ("abaqus_command", "worker", "run_root", "allowed_cae_roots")
    missing = [name for name in required if name not in config]
    if missing:
        raise BridgeError("Missing config keys: %s" % ", ".join(missing), 10)
    return config


def _windows_path(path_text: str, must_exist: bool = True) -> Path:
    path = Path(path_text).expanduser()
    if must_exist and not path.exists():
        raise BridgeError("Path does not exist: %s" % path, 10)
    return path.resolve()


def _validate_cae_path(path_text: str, allowed_roots: list[str]) -> Path:
    path = _windows_path(path_text)
    if path.suffix.lower() != ".cae":
        raise BridgeError("Only .cae files are accepted: %s" % path, 10)
    if not path.is_file():
        raise BridgeError("CAE path is not a regular file: %s" % path, 10)

    candidate = os.path.normcase(str(path))
    for root_text in allowed_roots:
        root = _windows_path(root_text)
        try:
            if os.path.commonpath((candidate, os.path.normcase(str(root)))) == os.path.normcase(str(root)):
                return path
        except ValueError:
            continue
    raise BridgeError("CAE file is outside allowed_cae_roots: %s" % path, 10)


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _new_run_directory(run_root: Path, request_id: str) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    run_dir = run_root / (stamp + "-" + request_id)
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _clean_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    return environment


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
        )
    except Exception:
        process.kill()


def _finalize_cae_isolation(
    source_info: dict | None,
    source_cae: Path | None,
    staged_cae: Path | None,
    hash_cae: bool,
    keep_staged_cae: bool,
) -> None:
    """Record post-open integrity and remove only the generated staged CAE."""
    if source_info is None or source_cae is None:
        return

    try:
        source_stat = source_cae.stat()
        source_info["size_after"] = source_stat.st_size
        source_info["mtime_ns_after"] = source_stat.st_mtime_ns
        source_unchanged = (
            source_stat.st_size == source_info["size"]
            and source_stat.st_mtime_ns == source_info["mtime_ns"]
        )
        if hash_cae:
            source_info["sha256_after"] = _sha256(source_cae)
            source_unchanged = (
                source_unchanged
                and source_info["sha256_after"] == source_info["sha256"]
            )
        source_info["source_unchanged"] = source_unchanged
    except Exception as exc:
        source_info["source_postcheck_error"] = "%s: %s" % (type(exc).__name__, exc)
        source_info["source_unchanged"] = False

    if staged_cae is None:
        return
    try:
        if staged_cae.is_file():
            source_info["staged_size_after"] = staged_cae.stat().st_size
            if hash_cae:
                source_info["staged_sha256_after"] = _sha256(staged_cae)
                source_info["staged_changed_by_open"] = (
                    source_info["staged_sha256_after"] != source_info["staged_sha256"]
                )
            if not keep_staged_cae:
                staged_cae.unlink()
                source_info["staged_removed"] = True
            else:
                source_info["staged_removed"] = False
        else:
            source_info["staged_removed"] = True
    except Exception as exc:
        source_info["staged_cleanup_error"] = "%s: %s" % (type(exc).__name__, exc)
        source_info["staged_removed"] = False


def run_rpc(
    config: dict,
    method: str,
    params: dict,
    timeout_seconds: float,
    stage_copy: bool = False,
    hash_cae: bool = False,
) -> tuple[dict, int]:
    if os.name != "nt":
        raise BridgeError(
            "Run this broker with Windows CPython, not Linux/WSL Python.",
            10,
        )

    abaqus_command = _windows_path(str(config["abaqus_command"]))
    worker = _windows_path(str(config["worker"]))
    run_root = _windows_path(str(config["run_root"]), must_exist=False)
    run_root.mkdir(parents=True, exist_ok=True)

    request_id = str(uuid.uuid4())
    run_dir = _new_run_directory(run_root, request_id)
    request_path = run_dir / "request.json"
    response_path = run_dir / "response.json"
    abaqus_log = run_dir / "abaqus.log"

    source_info = None
    source_cae = None
    staged_cae = None
    if params.get("cae_path"):
        source_cae = _validate_cae_path(
            str(params["cae_path"]), [str(value) for value in config["allowed_cae_roots"]]
        )
        source_info = {
            "path": str(source_cae),
            "size": source_cae.stat().st_size,
            "mtime_ns": source_cae.stat().st_mtime_ns,
            "isolation": "mandatory_staged_copy",
        }
        if hash_cae:
            source_info["sha256"] = _sha256(source_cae)

        # ``stage_copy`` is retained in the Python API for compatibility, but
        # direct CAE opening is deliberately not available.
        source_info["stage_copy_requested"] = bool(stage_copy)
        staged_cae = run_dir / "model.cae"
        shutil.copy2(source_cae, staged_cae)
        params["cae_path"] = str(staged_cae)
        source_info["staged_path"] = str(staged_cae)
        source_info["staged_size"] = staged_cae.stat().st_size
        if hash_cae:
            source_info["staged_sha256"] = _sha256(staged_cae)
            if source_info["sha256"] != source_info["staged_sha256"]:
                staged_cae.unlink()
                raise BridgeError("Staged CAE SHA-256 does not match its source", 126)

    request = {
        "jsonrpc": PROTOCOL_VERSION,
        "id": request_id,
        "method": method,
        "params": params,
        "meta": {
            "bridge_version": BRIDGE_VERSION,
            "configured_abaqus_release": str(config.get("abaqus_release", "2022")),
            "fixed_read_methods": True,
            "source_cae_isolated": bool(source_info),
            "cae_open_may_mutate_staged_copy": bool(source_info),
        },
    }
    _atomic_json(request_path, request)

    abaqus_args = [
        str(abaqus_command),
        "cae",
        "noGUI=" + str(worker),
        "--",
        str(request_path),
        str(response_path),
    ]
    command_line = subprocess.list2cmdline(abaqus_args)
    launcher = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command_line]

    started = time.monotonic()
    with abaqus_log.open("wb") as log_stream:
        try:
            process = subprocess.Popen(
                launcher,
                cwd=str(run_dir),
                env=_clean_environment(),
                stdin=subprocess.DEVNULL,
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                shell=False,
            )
        except OSError as exc:
            _finalize_cae_isolation(
                source_info,
                source_cae,
                staged_cae,
                hash_cae,
                bool(config.get("keep_staged_cae", False)),
            )
            raise BridgeError("Could not start Abaqus: %s" % exc, 125)

        try:
            return_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(process)
            elapsed = time.monotonic() - started
            _finalize_cae_isolation(
                source_info,
                source_cae,
                staged_cae,
                hash_cae,
                bool(config.get("keep_staged_cae", False)),
            )
            timeout_result = {
                "jsonrpc": PROTOCOL_VERSION,
                "id": request_id,
                "error": {
                    "code": -32004,
                    "message": "ABAQUS_TIMEOUT",
                    "data": {"timeout_seconds": timeout_seconds},
                },
                "_bridge": {
                    "version": BRIDGE_VERSION,
                    "elapsed_seconds": round(elapsed, 3),
                    "run_directory": str(run_dir),
                    "abaqus_log": str(abaqus_log),
                    "source_cae": source_info,
                },
            }
            return timeout_result, 124

    elapsed = time.monotonic() - started
    _finalize_cae_isolation(
        source_info,
        source_cae,
        staged_cae,
        hash_cae,
        bool(config.get("keep_staged_cae", False)),
    )
    if not response_path.is_file():
        missing = {
            "jsonrpc": PROTOCOL_VERSION,
            "id": request_id,
            "error": {
                "code": -32005,
                "message": "RESPONSE_MISSING",
                "data": {"abaqus_return_code": return_code},
            },
            "_bridge": {
                "version": BRIDGE_VERSION,
                "elapsed_seconds": round(elapsed, 3),
                "run_directory": str(run_dir),
                "abaqus_log": str(abaqus_log),
                "source_cae": source_info,
            },
        }
        return missing, 126

    max_response_bytes = int(config.get("max_response_bytes", 5 * 1024 * 1024))
    if response_path.stat().st_size > max_response_bytes:
        raise BridgeError("Abaqus response exceeds configured limit", 126)
    response = _load_json(response_path)
    if response.get("jsonrpc") != PROTOCOL_VERSION or response.get("id") != request_id:
        raise BridgeError("Abaqus response protocol/id mismatch", 126)

    if source_info is not None and source_info.get("source_unchanged") is not True:
        response = {
            "jsonrpc": PROTOCOL_VERSION,
            "id": request_id,
            "error": {
                "code": -32007,
                "message": "SOURCE_CAE_INTEGRITY_CHECK_FAILED",
                "data": {"source_cae": source_info},
            },
        }
        return_code = 126

    response["_bridge"] = {
        "version": BRIDGE_VERSION,
        "elapsed_seconds": round(elapsed, 3),
        "abaqus_return_code": return_code,
        "run_directory": str(run_dir),
        "abaqus_log": str(abaqus_log),
        "request": str(request_path),
        "response": str(response_path),
        "source_cae": source_info,
    }
    if return_code != 0 and "error" not in response:
        response["error"] = {
            "code": -32006,
            "message": "ABAQUS_NONZERO_EXIT",
            "data": {"abaqus_return_code": return_code},
        }
        response.pop("result", None)
    audit_path = run_dir / "audit.json"
    response["_bridge"]["audit"] = str(audit_path)
    _atomic_json(audit_path, response)
    return response, (0 if "result" in response and return_code == 0 else 20)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Connect external CPython 3 to the Abaqus/CAE 2022 Python kernel."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Bridge JSON config")
    parser.add_argument("--abaqus", help="Override Abaqus .bat path")
    parser.add_argument("--timeout", type=float, help="Override operation timeout in seconds")
    parser.add_argument("--compact", action="store_true", help="Emit compact JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ping", help="Start the CAE kernel and return environment details")

    list_parser = subparsers.add_parser("list-models", help="List models/jobs in a CAE file")
    list_parser.add_argument("--cae", help="Optional .cae path; omitted uses the empty CAE session")
    list_parser.add_argument(
        "--stage-copy",
        action="store_true",
        help="Compatibility flag; CAE staging is now mandatory",
    )
    list_parser.add_argument("--hash-cae", action="store_true", help="SHA-256 source/staged CAE")

    summary_parser = subparsers.add_parser("model-summary", help="Read a bounded model summary")
    summary_parser.add_argument("--cae", required=True, help=".cae path")
    summary_parser.add_argument("--model", required=True, help="Exact model name")
    summary_parser.add_argument("--max-items", type=int, default=200, help="Maximum names per repository")
    summary_parser.add_argument(
        "--stage-copy",
        action="store_true",
        help="Compatibility flag; CAE staging is now mandatory",
    )
    summary_parser.add_argument("--hash-cae", action="store_true", help="SHA-256 source/staged CAE")
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    try:
        config = load_config(_windows_path(args.config))
        if args.abaqus:
            config["abaqus_command"] = args.abaqus

        default_timeouts = config.get("timeouts", {})
        if args.command == "ping":
            method = "ping"
            params = {}
            timeout = args.timeout or float(default_timeouts.get("ping", 180))
            stage_copy = False
            hash_cae = False
        elif args.command == "list-models":
            method = "list_models"
            params = {"cae_path": args.cae} if args.cae else {}
            timeout = args.timeout or float(default_timeouts.get("list_models", 600))
            stage_copy = bool(args.cae)
            hash_cae = args.hash_cae
        else:
            if not 1 <= args.max_items <= 500:
                raise BridgeError("--max-items must be between 1 and 500", 10)
            method = "model_summary"
            params = {
                "cae_path": args.cae,
                "model_name": args.model,
                "max_items": args.max_items,
            }
            timeout = args.timeout or float(default_timeouts.get("model_summary", 600))
            stage_copy = True
            hash_cae = args.hash_cae

        if timeout <= 0 or timeout > float(config.get("hard_timeout_seconds", 900)):
            raise BridgeError("Timeout is outside the configured range", 10)

        response, exit_code = run_rpc(
            config,
            method,
            params,
            timeout_seconds=timeout,
            stage_copy=stage_copy,
            hash_cae=hash_cae,
        )
    except BridgeError as exc:
        response = {
            "jsonrpc": PROTOCOL_VERSION,
            "id": None,
            "error": {"code": -32000, "message": str(exc)},
            "_bridge": {"version": BRIDGE_VERSION},
        }
        exit_code = exc.exit_code
    except Exception as exc:
        response = {
            "jsonrpc": PROTOCOL_VERSION,
            "id": None,
            "error": {"code": -32099, "message": "%s: %s" % (type(exc).__name__, exc)},
            "_bridge": {"version": BRIDGE_VERSION},
        }
        exit_code = 30

    text = json.dumps(
        response,
        ensure_ascii=False,
        indent=None if args.compact else 2,
        separators=(",", ":") if args.compact else None,
        allow_nan=False,
    )
    try:
        print(text)
    except UnicodeEncodeError:
        print(json.dumps(response, ensure_ascii=True, indent=2, allow_nan=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
