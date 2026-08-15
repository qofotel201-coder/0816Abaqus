"""Safety primitives shared by the external Abaqus automation broker.

This module is intentionally independent of Abaqus and uses only the CPython 3
standard library.  It provides append-only JSON manifests, strict path
containment, content hashes, UUID validation, and two-phase confirmation tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import uuid
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = "abaqus-automation/1.0"
MAX_JSON_BYTES = 8 * 1024 * 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
SAFE_JOB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")


class AutomationError(RuntimeError):
    """A structured, user-safe automation failure."""

    def __init__(self, code: str, message: str, data: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = dict(data or {})

    def as_jsonrpc_error(self) -> dict[str, Any]:
        return {
            "code": -32000,
            "message": self.code,
            "data": {"detail": self.message, **self.data},
        }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def object_sha256(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(chunk_size)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def require_sha256(value: Any, field: str = "sha256") -> str:
    text = str(value or "").lower()
    if not SHA256_RE.fullmatch(text):
        raise AutomationError("INVALID_SHA256", "%s must be 64 lowercase hexadecimal characters" % field)
    return text


def require_uuid(value: Any, field: str = "id") -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        raise AutomationError("INVALID_UUID", "%s must be a UUID" % field)
    canonical = str(parsed)
    if str(value).lower() != canonical:
        raise AutomationError("NONCANONICAL_UUID", "%s must use canonical UUID text" % field)
    return canonical


def new_uuid() -> str:
    return str(uuid.uuid4())


def require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise AutomationError("INVALID_BOOLEAN", "%s must be true or false" % field)
    return value


def require_mapping(value: Any, field: str = "params") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AutomationError("INVALID_OBJECT", "%s must be a JSON object" % field)
    return value


def bounded_text(value: Any, field: str, max_length: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise AutomationError("INVALID_TEXT", "%s must be text" % field)
    text = value.strip()
    if not allow_empty and not text:
        raise AutomationError("EMPTY_TEXT", "%s may not be empty" % field)
    if len(text) > max_length:
        raise AutomationError("TEXT_TOO_LONG", "%s exceeds %d characters" % (field, max_length))
    if any(ord(character) < 32 for character in text):
        raise AutomationError("CONTROL_CHARACTER", "%s contains a control character" % field)
    return text


def safe_label(value: Any, field: str = "label") -> str:
    text = bounded_text(value, field, 80)
    if not SAFE_LABEL_RE.fullmatch(text):
        raise AutomationError("INVALID_LABEL", "%s contains unsupported characters" % field)
    return text


def safe_job_name(value: Any, field: str = "job_name") -> str:
    text = bounded_text(value, field, 80)
    if not SAFE_JOB_NAME_RE.fullmatch(text):
        raise AutomationError("INVALID_JOB_NAME", "%s contains unsupported characters" % field)
    return text


def _reject_network_path(path_text: str, field: str) -> None:
    normalized = path_text.replace("/", "\\")
    if normalized.startswith("\\\\"):
        raise AutomationError("NETWORK_PATH_DENIED", "%s may not be a UNC/network path" % field)


def _resolved_root(root: Path) -> Path:
    _reject_network_path(str(root), "configured root")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise AutomationError("ROOT_NOT_AVAILABLE", "Configured root is unavailable", {"path": str(root), "error": str(exc)})
    if not resolved.is_dir():
        raise AutomationError("ROOT_NOT_DIRECTORY", "Configured root is not a directory", {"path": str(resolved)})
    return resolved


def is_within(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
    except ValueError:
        return False


def resolve_allowed_file(
    path_value: Any,
    allowed_roots: Sequence[Path],
    allowed_suffixes: Iterable[str],
    field: str = "path",
) -> Path:
    path_text = bounded_text(path_value, field, 32767)
    _reject_network_path(path_text, field)
    candidate = Path(path_text)
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AutomationError("FILE_NOT_FOUND", "%s is unavailable" % field, {"path": path_text, "error": str(exc)})
    if not resolved.is_file():
        raise AutomationError("NOT_A_FILE", "%s is not a regular file" % field, {"path": str(resolved)})
    suffixes = {suffix.lower() for suffix in allowed_suffixes}
    if resolved.suffix.lower() not in suffixes:
        raise AutomationError(
            "EXTENSION_DENIED",
            "%s has an unsupported extension" % field,
            {"path": str(resolved), "allowed_suffixes": sorted(suffixes)},
        )
    roots = [_resolved_root(Path(root)) for root in allowed_roots]
    if not any(is_within(resolved, root) for root in roots):
        raise AutomationError("PATH_OUTSIDE_ALLOWLIST", "%s is outside the configured roots" % field, {"path": str(resolved)})
    return resolved


def managed_child(root: Path, child_name: str) -> Path:
    resolved_root = _resolved_root(root)
    if not SAFE_LABEL_RE.fullmatch(child_name):
        raise AutomationError("INVALID_MANAGED_NAME", "Managed child name is invalid")
    child = resolved_root / child_name
    if not is_within(child, resolved_root) or child.parent != resolved_root:
        raise AutomationError("MANAGED_PATH_ESCAPE", "Managed child path escaped its root")
    return child


def safe_relative_path(value: Any, field: str = "relative_path") -> Path:
    text = bounded_text(value, field, 512)
    if "\\" in text or ":" in text:
        raise AutomationError("INVALID_RELATIVE_PATH", "%s must use simple forward-slash relative syntax" % field)
    relative = Path(text)
    if relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts):
        raise AutomationError("INVALID_RELATIVE_PATH", "%s may not be absolute or contain traversal" % field)
    return relative


def file_manifest(path: Path, include_sha256: bool = True) -> dict[str, Any]:
    stat_result = path.stat()
    result: dict[str, Any] = {
        "path": str(path),
        "size": stat_result.st_size,
        "mtime_ns": stat_result.st_mtime_ns,
    }
    if include_sha256:
        result["sha256"] = sha256_file(path)
    return result


def _reject_json_constant(value: str) -> None:
    raise AutomationError("NONFINITE_JSON_NUMBER", "JSON may not contain %s" % value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AutomationError("DUPLICATE_JSON_KEY", "JSON object repeats a key", {"key": key})
        result[key] = value
    return result


def read_json(path: Path, max_bytes: int = MAX_JSON_BYTES) -> Any:
    stat_result = path.stat()
    if stat_result.st_size > max_bytes:
        raise AutomationError("JSON_TOO_LARGE", "JSON file exceeds the configured limit", {"path": str(path), "size": stat_result.st_size})
    try:
        with path.open("r", encoding="utf-8") as stream:
            return json.load(
                stream,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_unique_json_object,
            )
    except UnicodeDecodeError as exc:
        raise AutomationError("INVALID_JSON_ENCODING", "JSON must be UTF-8", {"path": str(path), "error": str(exc)})
    except json.JSONDecodeError as exc:
        raise AutomationError("INVALID_JSON", "JSON syntax is invalid", {"path": str(path), "error": str(exc)})


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_new_bytes(path: Path, payload: bytes) -> None:
    """Atomically publish a new file and refuse to replace an existing path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / (".%s.tmp-%s" % (path.name, new_uuid()))
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(str(temporary), str(path))
        except FileExistsError:
            raise AutomationError("NO_OVERWRITE", "Refusing to replace an existing file", {"path": str(path)})
        except OSError as exc:
            raise AutomationError("ATOMIC_PUBLISH_FAILED", "Could not atomically publish a new file", {"path": str(path), "error": str(exc)})
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_write_new_json(path: Path, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    atomic_write_new_bytes(path, payload)


def atomic_copy_new(source: Path, destination: Path, chunk_size: int = 8 * 1024 * 1024) -> dict[str, Any]:
    """Copy to a new path with an atomic, no-replacement final publish."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / (".%s.tmp-%s" % (destination.name, new_uuid()))
    digest = hashlib.sha256()
    size = 0
    try:
        with source.open("rb") as input_stream, temporary.open("xb") as output_stream:
            while True:
                block = input_stream.read(chunk_size)
                if not block:
                    break
                output_stream.write(block)
                digest.update(block)
                size += len(block)
            output_stream.flush()
            os.fsync(output_stream.fileno())
        try:
            os.link(str(temporary), str(destination))
        except FileExistsError:
            raise AutomationError("NO_OVERWRITE", "Refusing to replace an existing file", {"path": str(destination)})
        except OSError as exc:
            raise AutomationError("ATOMIC_PUBLISH_FAILED", "Could not atomically publish copied data", {"path": str(destination), "error": str(exc)})
        _fsync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return {"path": str(destination), "size": size, "sha256": digest.hexdigest()}


def _manifest_versions(directory: Path, stem: str) -> list[tuple[int, Path]]:
    pattern = re.compile(r"^%s\.v([0-9]{6})\.json$" % re.escape(stem))
    versions: list[tuple[int, Path]] = []
    if not directory.is_dir():
        return versions
    for candidate in directory.iterdir():
        match = pattern.fullmatch(candidate.name)
        if match and candidate.is_file():
            versions.append((int(match.group(1)), candidate))
    return sorted(versions)


def read_latest_manifest(directory: Path, stem: str) -> tuple[dict[str, Any], Path, str, int]:
    versions = _manifest_versions(directory, stem)
    if not versions:
        raise AutomationError("MANIFEST_NOT_FOUND", "No registered manifest was found", {"directory": str(directory), "stem": stem})
    version, path = versions[-1]
    value = read_json(path)
    if not isinstance(value, dict):
        raise AutomationError("INVALID_MANIFEST", "Manifest root must be an object", {"path": str(path)})
    if value.get("manifest_version") != version:
        raise AutomationError("MANIFEST_VERSION_MISMATCH", "Manifest filename and body disagree", {"path": str(path)})
    return value, path, sha256_file(path), version


def write_next_manifest(
    directory: Path,
    stem: str,
    value: Mapping[str, Any],
    expected_current_version: int | None,
) -> tuple[dict[str, Any], Path, str, int]:
    versions = _manifest_versions(directory, stem)
    current = versions[-1][0] if versions else 0
    if expected_current_version is not None and current != expected_current_version:
        raise AutomationError(
            "STALE_WORKSPACE_VERSION",
            "Manifest changed since it was inspected",
            {"expected": expected_current_version, "actual": current},
        )
    next_version = current + 1
    body = dict(value)
    body["manifest_version"] = next_version
    path = directory / ("%s.v%06d.json" % (stem, next_version))
    atomic_write_new_json(path, body)
    return body, path, sha256_file(path), next_version


def new_confirmation_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, sha256_bytes(token.encode("utf-8"))


def verify_confirmation_token(token: Any, expected_hash: Any) -> None:
    text = bounded_text(token, "confirmation_token", 256)
    expected = require_sha256(expected_hash, "confirmation_token_sha256")
    observed = sha256_bytes(text.encode("utf-8"))
    if not hmac.compare_digest(observed, expected):
        raise AutomationError("CONFIRMATION_TOKEN_MISMATCH", "Two-phase confirmation token does not match")


@dataclass(frozen=True)
class AutomationConfig:
    allowed_source_roots: tuple[Path, ...]
    allowed_subroutine_roots: tuple[Path, ...]
    workspace_root: Path
    plan_ttl_seconds: int = 1800
    max_subroutine_bytes: int = 10 * 1024 * 1024
    artifact_hash_limit_bytes: int = 256 * 1024 * 1024
    allow_job_kill: bool = False
    solver_execution_enabled: bool = False

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "AutomationConfig":
        source_values = value.get("allowed_source_roots")
        subroutine_values = value.get("allowed_subroutine_roots")
        if not isinstance(source_values, list) or not source_values:
            raise AutomationError("CONFIG_ERROR", "allowed_source_roots must be a non-empty array")
        if not isinstance(subroutine_values, list) or not subroutine_values:
            raise AutomationError("CONFIG_ERROR", "allowed_subroutine_roots must be a non-empty array")
        workspace_value = value.get("workspace_root")
        if not isinstance(workspace_value, str) or not workspace_value:
            raise AutomationError("CONFIG_ERROR", "workspace_root is required")
        source_roots = tuple(_resolved_root(Path(str(item))) for item in source_values)
        subroutine_roots = tuple(_resolved_root(Path(str(item))) for item in subroutine_values)
        workspace_root = _resolved_root(Path(workspace_value))
        if any(is_within(workspace_root, root) or is_within(root, workspace_root) for root in source_roots):
            raise AutomationError("CONFIG_ERROR", "workspace_root and source roots must be disjoint")
        ttl = int(value.get("plan_ttl_seconds", 1800))
        if ttl < 60 or ttl > 86400:
            raise AutomationError("CONFIG_ERROR", "plan_ttl_seconds must be between 60 and 86400")
        subroutine_limit = int(value.get("max_subroutine_bytes", 10 * 1024 * 1024))
        artifact_limit = int(value.get("artifact_hash_limit_bytes", 256 * 1024 * 1024))
        if subroutine_limit < 1024 or artifact_limit < 1024:
            raise AutomationError("CONFIG_ERROR", "Configured size limits are too small")
        return cls(
            allowed_source_roots=source_roots,
            allowed_subroutine_roots=subroutine_roots,
            workspace_root=workspace_root,
            plan_ttl_seconds=ttl,
            max_subroutine_bytes=subroutine_limit,
            artifact_hash_limit_bytes=artifact_limit,
            allow_job_kill=value.get("allow_job_kill", False) is True,
            solver_execution_enabled=value.get("solver_execution_enabled", False) is True,
        )


def load_config(path: Path) -> AutomationConfig:
    value = read_json(path)
    return AutomationConfig.from_mapping(require_mapping(value, "config"))
