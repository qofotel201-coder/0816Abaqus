# -*- coding: utf-8 -*-
"""Controlled Abaqus/CAE 2022 workspace mutation worker.

The worker is intentionally separate from the read-only audit worker and the
solver job manager.  It exposes only fixed methods for inspecting a workspace
copy, cloning a model/job, binding an approved user subroutine, saving to a new
CAE and generating an input file.  It never submits or kills a solver job.

All mutating calls require a canonical two-phase plan, its SHA-256, an
expected input hash, an explicit commit token and a target that does not yet
exist.  The external CPython 3 orchestrator owns path-root authorization,
workspace versioning and one-time-token consumption.
"""

from __future__ import print_function

import hashlib
import hmac
import io
import json
import os
import platform
import socket
import sys
import time
import traceback

from abaqus import mdb, openMdb, session
from abaqusConstants import *
from caeModules import *


WORKER_VERSION = "1.0.0"
JSONRPC_VERSION = "2.0"
CHUNK_SIZE = 8 * 1024 * 1024


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data or {}


def _text(value):
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = str
    if isinstance(value, unicode_type):
        return value
    try:
        return unicode_type(value)
    except Exception:
        return unicode_type(repr(value))


def _native_path(value):
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = str
    if os.name == "nt" and isinstance(value, unicode_type):
        return value.encode("mbcs")
    return value


def _native_ascii_name(value, field):
    text = _text(value)
    try:
        encoded = text.encode("ascii")
    except UnicodeEncodeError:
        raise RpcError(-32602, "ASCII_NAME_REQUIRED", {"field": field})
    return encoded if sys.version_info[0] < 3 else text


def _repo_key(repository, requested):
    wanted = _text(requested)
    for key in list(repository.keys()):
        if _text(key) == wanted:
            return key
    return None


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        while True:
            block = stream.read(CHUNK_SIZE)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    if not isinstance(payload, bytes):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _artifact(path):
    return {
        "path": _text(path),
        "size": os.path.getsize(path),
        "sha256": _sha256(path),
    }


def _read_request(path):
    with io.open(path, "r", encoding="utf-8") as stream:
        request = json.load(stream)
    if not isinstance(request, dict):
        raise RpcError(-32600, "INVALID_REQUEST")
    if request.get("jsonrpc") != JSONRPC_VERSION:
        raise RpcError(-32600, "INVALID_JSONRPC_VERSION")
    if not request.get("id"):
        raise RpcError(-32600, "REQUEST_ID_REQUIRED")
    if not isinstance(request.get("params", {}), dict):
        raise RpcError(-32602, "PARAMS_MUST_BE_OBJECT")
    return request


def _atomic_response(path, response):
    if os.path.exists(path):
        raise RpcError(-32020, "RESPONSE_TARGET_ALREADY_EXISTS")
    temporary = path + ".tmp"
    payload = json.dumps(response, ensure_ascii=True, indent=2, allow_nan=False)
    if not isinstance(payload, bytes):
        payload = payload.encode("utf-8")
    with open(temporary, "wb") as stream:
        stream.write(payload)
        stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(temporary, path)


def _require_workspace_cae(params, expected_hash_required=True):
    if params.get("workspace_copy_confirmed") is not True:
        raise RpcError(-32010, "WORKSPACE_COPY_CONFIRMATION_REQUIRED")
    path = params.get("cae_path")
    if not path:
        raise RpcError(-32602, "CAE_PATH_REQUIRED")
    native = _native_path(path)
    if not os.path.isfile(native):
        raise RpcError(-32001, "CAE_NOT_FOUND", {"cae_path": path})
    if os.path.splitext(native)[1].lower() != ".cae":
        raise RpcError(-32602, "CAE_EXTENSION_REQUIRED")
    expected = params.get("expected_cae_sha256")
    if expected_hash_required and not expected:
        raise RpcError(-32602, "EXPECTED_CAE_SHA256_REQUIRED")
    actual = _sha256(native)
    if expected and actual.lower() != _text(expected).lower():
        raise RpcError(-32011, "CAE_HASH_MISMATCH", {
            "expected": _text(expected), "actual": actual,
        })
    return native, actual


def _require_commit(params, operation):
    token = params.get("commit_token")
    version = params.get("expected_workspace_version")
    plan = params.get("plan")
    supplied_sha = params.get("plan_sha256")
    if not token or len(_text(token)) < 16:
        raise RpcError(-32602, "COMMIT_TOKEN_REQUIRED")
    if version is None:
        raise RpcError(-32602, "EXPECTED_WORKSPACE_VERSION_REQUIRED")
    if not isinstance(plan, dict) or not supplied_sha:
        raise RpcError(-32602, "PLAN_AND_PLAN_SHA256_REQUIRED")
    if plan.get("operation") != operation:
        raise RpcError(-32602, "PLAN_OPERATION_MISMATCH")
    computed = _canonical_sha256(plan)
    if computed.lower() != _text(supplied_sha).lower():
        raise RpcError(-32012, "PLAN_HASH_MISMATCH", {
            "expected": _text(supplied_sha), "actual": computed,
        })
    if plan.get("expected_workspace_version") != version:
        raise RpcError(-32017, "WORKSPACE_VERSION_PLAN_MISMATCH")
    expected_token_hash = plan.get("confirmation_token_sha256")
    if not expected_token_hash:
        raise RpcError(-32602, "PLAN_CONFIRMATION_TOKEN_HASH_REQUIRED")
    observed_token_hash = hashlib.sha256(_text(token).encode("utf-8")).hexdigest()
    expected_token_digest = _text(expected_token_hash).lower().encode("ascii")
    if not hmac.compare_digest(observed_token_hash.lower(), expected_token_digest):
        raise RpcError(-32018, "CONFIRMATION_TOKEN_MISMATCH")
    claim_path = _native_path(plan.get("claim_path", ""))
    if not claim_path or os.path.splitext(claim_path)[1].lower() != ".json":
        raise RpcError(-32602, "PLAN_CLAIM_PATH_REQUIRED")
    claim_parent = os.path.dirname(claim_path)
    if not os.path.isdir(claim_parent):
        raise RpcError(-32019, "CLAIM_PARENT_NOT_FOUND")
    claim = {
        "operation": operation,
        "plan_sha256": computed,
        "expected_workspace_version": version,
    }
    claim_payload = json.dumps(
        claim, ensure_ascii=True, sort_keys=True, allow_nan=False
    ).encode("utf-8") + b"\n"
    try:
        descriptor = os.open(claim_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except OSError:
        raise RpcError(-32021, "COMMIT_ALREADY_CLAIMED", {
            "claim_path": _text(plan.get("claim_path")),
        })
    try:
        os.write(descriptor, claim_payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return plan, computed


def _open_workspace(params):
    path, cae_hash = _require_workspace_cae(params)
    started = time.time()
    try:
        opened = openMdb(pathName=path)
    except Exception as exc:
        raise RpcError(-32002, "CAE_OPEN_FAILED", {"message": _text(exc)})
    return opened, path, cae_hash, round(time.time() - started, 3)


def _close_workspace(opened):
    try:
        opened.close()
    except Exception as exc:
        raise RpcError(-32036, "CAE_CLOSE_FAILED", {"message": _text(exc)})


def _safe_attrs(obj, names):
    output = {}
    for name in names:
        try:
            value = getattr(obj, name)
            if value.__class__.__name__ == "SymbolicConstant":
                output[name] = value
            elif isinstance(value, (str, unicode, int, long, float, bool)) or value is None:
                output[name] = value
            else:
                output[name] = _text(value)
        except Exception:
            pass
    return output


def _model_summary(opened, model_key):
    model = opened.models[model_key]
    jobs = []
    for key in list(opened.jobs.keys()):
        try:
            if _text(opened.jobs[key].model) == _text(model_key):
                jobs.append(_text(key))
        except Exception:
            pass
    return {
        "model": _text(model_key),
        "parts": sorted([_text(key) for key in list(model.parts.keys())]),
        "materials": sorted([_text(key) for key in list(model.materials.keys())]),
        "steps": sorted([_text(key) for key in list(model.steps.keys())]),
        "jobs": sorted(jobs),
    }


def system_capabilities(params):
    session_info = {}
    for name in ("productName", "productVersion", "release"):
        try:
            session_info[name] = _text(getattr(session, name))
        except Exception:
            pass
    return {
        "connected": True,
        "mutation_worker": True,
        "submits_solver_jobs": False,
        "kills_processes": False,
        "two_phase_commit_required": True,
        "worker_version": WORKER_VERSION,
        "python_version": sys.version.replace("\n", " "),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "session": session_info,
        "methods": sorted(METHODS.keys()),
    }


def workspace_inspect_cae(params):
    opened, path, cae_hash, elapsed = _open_workspace(params)
    model_name = params.get("model_name")
    if not model_name:
        raise RpcError(-32602, "MODEL_NAME_REQUIRED")
    key = _repo_key(opened.models, model_name)
    if key is None:
        raise RpcError(-32003, "MODEL_NOT_FOUND")
    return {
        "artifact_before_open": {
            "path": _text(path), "sha256": cae_hash,
        },
        "open_seconds": elapsed,
        "summary": _model_summary(opened, key),
        "warning": "openMdb may mutate this workspace CAE",
    }


def model_clone_commit(params):
    plan, plan_hash = _require_commit(params, "model.clone")
    opened, input_path, input_hash, elapsed = _open_workspace(params)
    source_model_name = plan.get("source_model_name")
    target_model_name = plan.get("target_model_name")
    source_job_name = plan.get("source_job_name")
    target_job_name = plan.get("target_job_name")
    output_path = _native_path(plan.get("output_cae_path", ""))
    if not source_model_name or not target_model_name or not output_path:
        raise RpcError(-32602, "MODEL_CLONE_PLAN_INCOMPLETE")
    if os.path.exists(output_path):
        raise RpcError(-32013, "OUTPUT_ALREADY_EXISTS", {"path": _text(output_path)})
    if os.path.abspath(output_path) == os.path.abspath(input_path):
        raise RpcError(-32014, "INPUT_OUTPUT_MUST_DIFFER")
    parent = os.path.dirname(output_path)
    if not os.path.isdir(parent):
        raise RpcError(-32015, "OUTPUT_PARENT_NOT_FOUND")
    source_model_key = _repo_key(opened.models, source_model_name)
    if source_model_key is None:
        raise RpcError(-32003, "SOURCE_MODEL_NOT_FOUND")
    if _repo_key(opened.models, target_model_name) is not None:
        raise RpcError(-32016, "TARGET_MODEL_ALREADY_EXISTS")
    try:
        opened.Model(
            name=_native_ascii_name(target_model_name, "target_model_name"),
            objectToCopy=opened.models[source_model_key],
        )
    except Exception as exc:
        raise RpcError(-32030, "MODEL_CLONE_FAILED", {"message": _text(exc)})

    created_job = None
    if source_job_name or target_job_name:
        if not source_job_name or not target_job_name:
            raise RpcError(-32602, "SOURCE_AND_TARGET_JOB_REQUIRED_TOGETHER")
        source_job_key = _repo_key(opened.jobs, source_job_name)
        if source_job_key is None:
            raise RpcError(-32031, "SOURCE_JOB_NOT_FOUND")
        if _repo_key(opened.jobs, target_job_name) is not None:
            raise RpcError(-32032, "TARGET_JOB_ALREADY_EXISTS")
        source_job = opened.jobs[source_job_key]
        kwargs = _safe_attrs(source_job, (
            "description", "type", "queue", "waitHours", "waitMinutes",
            "memory", "memoryUnits", "getMemoryFromAnalysis", "numCpus",
            "numDomains", "parallelizationMethodExplicit", "multiprocessingMode",
            "explicitPrecision", "nodalOutputPrecision", "resultsFormat",
            "userSubroutine", "scratch", "echoPrint", "modelPrint",
            "contactPrint", "historyPrint", "activateLoadBalancing",
            "numThreadsPerMpiProcess",
        ))
        kwargs["name"] = _native_ascii_name(target_job_name, "target_job_name")
        kwargs["model"] = _native_ascii_name(target_model_name, "target_model_name")
        try:
            opened.Job(**kwargs)
        except Exception as exc:
            raise RpcError(-32033, "JOB_CLONE_FAILED", {
                "message": _text(exc), "copied_attributes": sorted(kwargs.keys()),
            })
        created_job = _text(target_job_name)

    try:
        opened.saveAs(pathName=output_path)
    except Exception as exc:
        raise RpcError(-32034, "CAE_SAVE_AS_FAILED", {"message": _text(exc)})
    if not os.path.isfile(output_path):
        raise RpcError(-32035, "CAE_SAVE_AS_OUTPUT_MISSING")
    _close_workspace(opened)
    return {
        "status": "SUCCEEDED",
        "plan_sha256": plan_hash,
        "expected_workspace_version": params.get("expected_workspace_version"),
        "input_cae_sha256_before_open": input_hash,
        "open_seconds": elapsed,
        "created_model": _text(target_model_name),
        "created_job": created_job,
        "artifact": _artifact(output_path),
    }


def bind_subroutine_commit(params):
    plan, plan_hash = _require_commit(params, "job.bind_subroutine")
    opened, input_path, input_hash, elapsed = _open_workspace(params)
    job_name = plan.get("job_name")
    subroutine_path = _native_path(plan.get("subroutine_path", ""))
    expected_for_hash = plan.get("expected_subroutine_sha256")
    output_path = _native_path(plan.get("output_cae_path", ""))
    if not job_name or not subroutine_path or not expected_for_hash or not output_path:
        raise RpcError(-32602, "BIND_SUBROUTINE_PLAN_INCOMPLETE")
    if not os.path.isfile(subroutine_path):
        raise RpcError(-32040, "SUBROUTINE_NOT_FOUND")
    if os.path.splitext(subroutine_path)[1].lower() not in (".for", ".f"):
        raise RpcError(-32041, "SUBROUTINE_EXTENSION_NOT_ALLOWED")
    actual_for_hash = _sha256(subroutine_path)
    if actual_for_hash.lower() != _text(expected_for_hash).lower():
        raise RpcError(-32042, "SUBROUTINE_HASH_MISMATCH", {
            "expected": _text(expected_for_hash), "actual": actual_for_hash,
        })
    if os.path.exists(output_path):
        raise RpcError(-32013, "OUTPUT_ALREADY_EXISTS", {"path": _text(output_path)})
    if os.path.abspath(output_path) == os.path.abspath(input_path):
        raise RpcError(-32014, "INPUT_OUTPUT_MUST_DIFFER")
    if not os.path.isdir(os.path.dirname(output_path)):
        raise RpcError(-32015, "OUTPUT_PARENT_NOT_FOUND")
    job_key = _repo_key(opened.jobs, job_name)
    if job_key is None:
        raise RpcError(-32043, "JOB_NOT_FOUND")
    job = opened.jobs[job_key]
    old_value = _text(getattr(job, "userSubroutine", ""))
    expected_old = plan.get("expected_old_subroutine")
    if expected_old is not None and old_value != _text(expected_old):
        raise RpcError(-32044, "OLD_SUBROUTINE_MISMATCH", {
            "expected": _text(expected_old), "actual": old_value,
        })
    try:
        job.setValues(userSubroutine=subroutine_path)
        opened.saveAs(pathName=output_path)
    except Exception as exc:
        raise RpcError(-32045, "BIND_OR_SAVE_FAILED", {"message": _text(exc)})
    if not os.path.isfile(output_path):
        raise RpcError(-32035, "CAE_SAVE_AS_OUTPUT_MISSING")
    _close_workspace(opened)
    return {
        "status": "SUCCEEDED",
        "plan_sha256": plan_hash,
        "expected_workspace_version": params.get("expected_workspace_version"),
        "input_cae_sha256_before_open": input_hash,
        "open_seconds": elapsed,
        "old_user_subroutine": old_value,
        "new_user_subroutine": _text(plan.get("subroutine_path")),
        "subroutine": _artifact(subroutine_path),
        "artifact": _artifact(output_path),
    }


def job_write_input(params):
    plan, plan_hash = _require_commit(params, "job.write_input")
    opened, input_path, input_hash, elapsed = _open_workspace(params)
    job_name = plan.get("job_name")
    output_directory = _native_path(plan.get("output_directory", ""))
    if not job_name or not output_directory:
        raise RpcError(-32602, "WRITE_INPUT_PLAN_INCOMPLETE")
    if not os.path.isdir(output_directory):
        raise RpcError(-32050, "OUTPUT_DIRECTORY_NOT_FOUND")
    job_key = _repo_key(opened.jobs, job_name)
    if job_key is None:
        raise RpcError(-32043, "JOB_NOT_FOUND")
    expected_inp = os.path.join(output_directory, _text(job_name) + ".inp")
    if os.path.exists(expected_inp):
        raise RpcError(-32013, "OUTPUT_ALREADY_EXISTS", {"path": _text(expected_inp)})
    old_cwd = os.getcwd()
    try:
        os.chdir(output_directory)
        opened.jobs[job_key].writeInput(consistencyChecking=ON)
    except Exception as exc:
        raise RpcError(-32051, "WRITE_INPUT_FAILED", {"message": _text(exc)})
    finally:
        os.chdir(old_cwd)
    if not os.path.isfile(expected_inp):
        raise RpcError(-32052, "INPUT_FILE_MISSING_AFTER_WRITE")
    _close_workspace(opened)
    return {
        "status": "SUCCEEDED",
        "plan_sha256": plan_hash,
        "expected_workspace_version": params.get("expected_workspace_version"),
        "input_cae_sha256_before_open": input_hash,
        "open_seconds": elapsed,
        "artifact": _artifact(expected_inp),
    }


METHODS = {
    "system.capabilities": system_capabilities,
    "workspace.inspect_cae": workspace_inspect_cae,
    "model.clone.commit": model_clone_commit,
    "job.bind_subroutine.commit": bind_subroutine_commit,
    "job.write_input": job_write_input,
}


def main():
    separator = None
    try:
        separator = sys.argv.index("--")
    except ValueError:
        pass
    arguments = sys.argv[separator + 1:] if separator is not None else sys.argv[-2:]
    if len(arguments) != 2:
        sys.__stderr__.write("WORKSPACE_WORKER_BAD_ARGUMENTS: %r\n" % (arguments,))
        return 10
    request_path = _native_path(arguments[0])
    response_path = _native_path(arguments[1])
    request = None
    exit_code = 30
    try:
        request = _read_request(request_path)
        method_name = request.get("method")
        if method_name not in METHODS:
            raise RpcError(-32601, "METHOD_NOT_FOUND", {"method": method_name})
        result = METHODS[method_name](request.get("params", {}))
        response = {"jsonrpc": JSONRPC_VERSION, "id": request["id"], "result": result}
        exit_code = 0
    except RpcError as exc:
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request.get("id") if isinstance(request, dict) else None,
            "error": {"code": exc.code, "message": exc.message, "data": exc.data},
        }
        exit_code = 20
    except Exception as exc:
        error_log = os.path.join(os.path.dirname(response_path), "worker_error.log")
        with io.open(error_log, "w", encoding="utf-8") as stream:
            stream.write(_text(traceback.format_exc()))
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request.get("id") if isinstance(request, dict) else None,
            "error": {
                "code": -32099,
                "message": "WORKER_INTERNAL_ERROR",
                "data": {"type": type(exc).__name__, "error_log": "worker_error.log"},
            },
        }
        exit_code = 30
    try:
        _atomic_response(response_path, response)
    except Exception:
        sys.__stderr__.write(_text(traceback.format_exc()) + "\n")
        return 40
    sys.__stderr__.write("WORKSPACE_WORKER_COMPLETE exit=%s\n" % exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
