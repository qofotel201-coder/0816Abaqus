# -*- coding: utf-8 -*-
"""Fixed-method JSON-RPC worker executed by the Abaqus 2022 Python 2.7 kernel."""

from __future__ import print_function

import io
import json
import os
import platform
import socket
import sys
import time
import traceback

from abaqus import mdb, openMdb, session
from caeModules import *


WORKER_VERSION = "1.0.1"
JSONRPC_VERSION = "2.0"


class RpcError(Exception):
    def __init__(self, code, message, data=None):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.data = data or {}


def _plain_text(value):
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = str
    if isinstance(value, unicode_type):
        return value
    return unicode_type(value)


def _native_path(value):
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = str
    if os.name == "nt" and isinstance(value, unicode_type):
        return value.encode("mbcs")
    return value


def _repository_names(repository, max_items):
    names = sorted([_plain_text(name) for name in list(repository.keys())])
    return {
        "count": len(names),
        "items": names[:max_items],
        "truncated": len(names) > max_items,
    }


def _repository_native_key(repository, requested_name):
    """Resolve a JSON unicode name to the repository's native key object."""
    requested_text = _plain_text(requested_name)
    for native_key in list(repository.keys()):
        if _plain_text(native_key) == requested_text:
            return native_key
    return None


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
    temporary = path + ".tmp"
    payload = json.dumps(response, ensure_ascii=True, indent=2, allow_nan=False)
    if not isinstance(payload, bytes):
        payload = payload.encode("utf-8")
    with open(temporary, "wb") as stream:
        stream.write(payload)
        stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    if os.path.exists(path):
        os.remove(path)
    os.rename(temporary, path)


def _open_optional_mdb(params):
    cae_path = params.get("cae_path")
    if not cae_path:
        return mdb, None
    native = _native_path(cae_path)
    if not os.path.isfile(native):
        raise RpcError(-32001, "CAE_NOT_FOUND", {"cae_path": cae_path})
    if os.path.splitext(native)[1].lower() != ".cae":
        raise RpcError(-32602, "CAE_EXTENSION_REQUIRED")
    started = time.time()
    try:
        opened = openMdb(pathName=native)
    except Exception as exc:
        raise RpcError(-32002, "CAE_OPEN_FAILED", {"message": _plain_text(exc)})
    return opened, {"cae_path": cae_path, "open_seconds": round(time.time() - started, 3)}


def ping(params):
    version_attrs = {}
    for name in ("productName", "productVersion", "release"):
        try:
            version_attrs[name] = _plain_text(getattr(session, name))
        except Exception:
            pass
    return {
        "connected": True,
        "fixed_read_methods": True,
        "source_cae_isolated_by_broker": True,
        "worker_version": WORKER_VERSION,
        "python_version": sys.version.replace("\n", " "),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "cwd": os.getcwd(),
        "session": version_attrs,
        "default_models": sorted([_plain_text(name) for name in list(mdb.models.keys())]),
        "default_jobs": sorted([_plain_text(name) for name in list(mdb.jobs.keys())]),
    }


def list_models(params):
    opened, metadata = _open_optional_mdb(params)
    max_items = int(params.get("max_items", 500))
    result = {
        "models": _repository_names(opened.models, max_items),
        "jobs": _repository_names(opened.jobs, max_items),
    }
    if metadata:
        result["source"] = metadata
    return result


def model_summary(params):
    if not params.get("cae_path"):
        raise RpcError(-32602, "CAE_PATH_REQUIRED")
    model_name = params.get("model_name")
    if not model_name:
        raise RpcError(-32602, "MODEL_NAME_REQUIRED")
    max_items = int(params.get("max_items", 200))
    if max_items < 1 or max_items > 500:
        raise RpcError(-32602, "MAX_ITEMS_OUT_OF_RANGE")

    opened, metadata = _open_optional_mdb(params)
    available = sorted([_plain_text(name) for name in list(opened.models.keys())])
    native_model_key = _repository_native_key(opened.models, model_name)
    if native_model_key is None:
        raise RpcError(-32003, "MODEL_NOT_FOUND", {"available_models": available[:max_items]})
    model = opened.models[native_model_key]

    parts = []
    for part_name in sorted(list(model.parts.keys()), key=_plain_text)[:max_items]:
        part = model.parts[part_name]
        parts.append({
            "name": _plain_text(part_name),
            "nodes": len(part.nodes),
            "elements": len(part.elements),
            "cells": len(part.cells),
            "faces": len(part.faces),
        })

    job_names = []
    for job_name in sorted(list(opened.jobs.keys())):
        try:
            if _plain_text(opened.jobs[job_name].model) == _plain_text(model_name):
                job_names.append(_plain_text(job_name))
        except Exception:
            pass

    result = {
        "source": metadata,
        "model_name": _plain_text(model_name),
        "repositories": {
            "parts": _repository_names(model.parts, max_items),
            "materials": _repository_names(model.materials, max_items),
            "sections": _repository_names(model.sections, max_items),
            "steps": _repository_names(model.steps, max_items),
            "amplitudes": _repository_names(model.amplitudes, max_items),
            "loads": _repository_names(model.loads, max_items),
            "boundary_conditions": _repository_names(model.boundaryConditions, max_items),
            "interactions": _repository_names(model.interactions, max_items),
            "constraints": _repository_names(model.constraints, max_items),
            "predefined_fields": _repository_names(model.predefinedFields, max_items),
            "field_outputs": _repository_names(model.fieldOutputRequests, max_items),
            "history_outputs": _repository_names(model.historyOutputRequests, max_items),
        },
        "parts": parts,
        "jobs_for_model": {"count": len(job_names), "items": job_names[:max_items]},
        "assembly": {
            "instances": _repository_names(model.rootAssembly.instances, max_items),
            "sets": _repository_names(model.rootAssembly.sets, max_items),
            "surfaces": _repository_names(model.rootAssembly.surfaces, max_items),
        },
    }
    return result


METHODS = {
    "ping": ping,
    "list_models": list_models,
    "model_summary": model_summary,
}


def main():
    separator = None
    try:
        separator = sys.argv.index("--")
    except ValueError:
        pass
    # Abaqus 2022 on this machine removes the literal ``--`` and appends the
    # user arguments after its own -cae/-noGUI/-lmlog/-tmpdir arguments.
    # Newer launchers may preserve ``--``.  Support both layouts.
    arguments = sys.argv[separator + 1:] if separator is not None else sys.argv[-2:]
    if len(arguments) != 2:
        sys.__stderr__.write("ABAQUS_BRIDGE_WORKER_BAD_ARGUMENTS: %r\n" % (arguments,))
        return 10

    request_path = _native_path(arguments[0])
    response_path = _native_path(arguments[1])
    request = None
    try:
        request = _read_request(request_path)
        method_name = request.get("method")
        if method_name not in METHODS:
            raise RpcError(-32601, "METHOD_NOT_FOUND", {"method": method_name})
        result = METHODS[method_name](request.get("params", {}))
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request["id"],
            "result": result,
        }
        exit_code = 0
    except RpcError as exc:
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request.get("id") if isinstance(request, dict) else None,
            "error": {"code": exc.code, "message": exc.message, "data": exc.data},
        }
        exit_code = 20
    except Exception as exc:
        run_dir = os.path.dirname(response_path)
        error_log = os.path.join(run_dir, "worker_error.log")
        with io.open(error_log, "w", encoding="utf-8") as stream:
            stream.write(_plain_text(traceback.format_exc()))
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

    _atomic_response(response_path, response)
    sys.__stderr__.write("ABAQUS_BRIDGE_WORKER_COMPLETE method=%s exit=%s\n" % (
        request.get("method") if isinstance(request, dict) else "unknown",
        exit_code,
    ))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
