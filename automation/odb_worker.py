# -*- coding: utf-8 -*-
"""Bounded, read-only Abaqus ODB JSON worker.

The script is intentionally compatible with the Python 2.7 interpreter
bundled with Abaqus 2022.  It exposes only two fixed methods:

``inventory``
    List bounded step/history-region/history-variable metadata.

``extract_history``
    Extract one exact scalar history variable from one exact region in one
    exact step.  The request fails instead of silently truncating data.

Typical invocation::

    abaqus python odb_worker.py request.json response.json
"""

from __future__ import print_function

import io
import json
import math
import os
import sys

try:
    from odbAccess import openOdb as _OPEN_ODB
except ImportError:
    # External CPython imports this module for contract tests.  Actual ODB
    # operations still fail closed unless Abaqus supplies odbAccess.
    _OPEN_ODB = None


JSONRPC_VERSION = "2.0"
WORKER_VERSION = "1.0.0"
DEFAULT_MAX_ROWS = 100000
HARD_MAX_ROWS = 1000000
DEFAULT_MAX_STEPS = 100
HARD_MAX_STEPS = 1000
DEFAULT_MAX_REGIONS = 500
HARD_MAX_REGIONS = 5000
DEFAULT_MAX_VARIABLES = 200
HARD_MAX_VARIABLES = 1000

try:
    STRING_TYPES = (basestring,)
except NameError:
    STRING_TYPES = (str,)

try:
    INTEGER_TYPES = (int, long)
except NameError:
    INTEGER_TYPES = (int,)


class OdbContractError(Exception):
    """Structured, user-correctable ODB request failure."""

    def __init__(self, code, message=None, data=None):
        Exception.__init__(self, message or code)
        self.code = code
        self.message = message or code
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
        # Native CPython 3 on Windows accepts text paths directly.  Encoding
        # here would make os.path.splitext return bytes and break extension
        # checks in the portable contract suite.
        return value
    if os.name == "nt" and isinstance(value, unicode_type):
        return value.encode("mbcs")
    return value


def _is_finite(value):
    return not (math.isnan(value) or math.isinf(value))


def _required_text(params, name):
    value = params.get(name)
    if not isinstance(value, STRING_TYPES) or not value:
        raise OdbContractError(
            "INVALID_PARAMETER",
            "%s must be a non-empty string" % name,
            {"parameter": name},
        )
    return _plain_text(value)


def _bounded_integer(params, name, default, hard_max):
    value = params.get(name, default)
    if isinstance(value, bool) or not isinstance(value, INTEGER_TYPES):
        raise OdbContractError(
            "INVALID_PARAMETER",
            "%s must be an integer" % name,
            {"parameter": name},
        )
    if value < 1 or value > hard_max:
        raise OdbContractError(
            "LIMIT_OUT_OF_RANGE",
            "%s must be between 1 and %d" % (name, hard_max),
            {"parameter": name, "value": value, "hard_max": hard_max},
        )
    return int(value)


def _repository_keys(repository):
    return list(repository.keys())


def _sorted_text_keys(repository):
    return sorted([_plain_text(key) for key in _repository_keys(repository)])


def _exact_repository_key(repository, requested, missing_code, kind):
    matches = []
    for native_key in _repository_keys(repository):
        if _plain_text(native_key) == requested:
            matches.append(native_key)
    if not matches:
        available = _sorted_text_keys(repository)
        raise OdbContractError(
            missing_code,
            "%s was not found by exact name" % kind,
            {
                "requested": requested,
                "available_count": len(available),
                "available": available[:50],
                "available_truncated": len(available) > 50,
            },
        )
    if len(matches) != 1:
        raise OdbContractError(
            "AMBIGUOUS_REPOSITORY_KEY",
            "%s has more than one exact textual key" % kind,
            {"requested": requested, "match_count": len(matches)},
        )
    return matches[0]


def _validate_odb_path(params):
    path_text = _required_text(params, "odb_path")
    native = _native_path(path_text)
    if not os.path.isabs(native):
        raise OdbContractError(
            "ODB_ABSOLUTE_PATH_REQUIRED",
            "odb_path must be absolute",
            {"odb_path": path_text},
        )
    if os.path.splitext(native)[1].lower() != ".odb":
        raise OdbContractError(
            "ODB_EXTENSION_REQUIRED",
            "odb_path must end in .odb",
            {"odb_path": path_text},
        )
    if not os.path.isfile(native):
        raise OdbContractError(
            "ODB_NOT_FOUND",
            "ODB file does not exist",
            {"odb_path": path_text},
        )
    return path_text, native


def _open_read_only_odb(params):
    path_text, native = _validate_odb_path(params)
    if _OPEN_ODB is None:
        raise OdbContractError(
            "ODB_ACCESS_UNAVAILABLE",
            "odbAccess is available only under an Abaqus Python runtime",
        )
    try:
        odb = _OPEN_ODB(path=native, readOnly=True)
    except Exception as exc:
        raise OdbContractError(
            "ODB_OPEN_FAILED",
            "Abaqus could not open the ODB in read-only mode",
            {"type": type(exc).__name__, "message": _plain_text(exc)},
        )
    return path_text, native, odb


def _close_odb(odb):
    if odb is not None:
        odb.close()


def _bounded_names(repository, limit):
    names = _sorted_text_keys(repository)
    return {
        "count": len(names),
        "items": names[:limit],
        "truncated": len(names) > limit,
    }


def inventory(params):
    """Return bounded metadata without reading history data rows."""

    max_steps = _bounded_integer(
        params, "max_steps", DEFAULT_MAX_STEPS, HARD_MAX_STEPS
    )
    max_regions = _bounded_integer(
        params, "max_regions_per_step", DEFAULT_MAX_REGIONS, HARD_MAX_REGIONS
    )
    max_variables = _bounded_integer(
        params,
        "max_variables_per_region",
        DEFAULT_MAX_VARIABLES,
        HARD_MAX_VARIABLES,
    )

    path_text, native, odb = _open_read_only_odb(params)
    try:
        native_step_keys = _repository_keys(odb.steps)
        native_step_keys.sort(key=lambda key: _plain_text(key))
        step_items = []
        for native_step_key in native_step_keys[:max_steps]:
            step = odb.steps[native_step_key]
            native_region_keys = _repository_keys(step.historyRegions)
            native_region_keys.sort(key=lambda key: _plain_text(key))
            region_items = []
            for native_region_key in native_region_keys[:max_regions]:
                region = step.historyRegions[native_region_key]
                region_items.append(
                    {
                        "name": _plain_text(native_region_key),
                        "variables": _bounded_names(
                            region.historyOutputs, max_variables
                        ),
                    }
                )
            step_item = {
                "name": _plain_text(native_step_key),
                "frame_count": len(step.frames),
                "history_regions": {
                    "count": len(native_region_keys),
                    "items": region_items,
                    "truncated": len(native_region_keys) > max_regions,
                },
            }
            time_period = getattr(step, "timePeriod", None)
            if time_period is not None:
                try:
                    numeric_period = float(time_period)
                    if _is_finite(numeric_period):
                        step_item["time_period"] = numeric_period
                except (TypeError, ValueError):
                    pass
            step_items.append(step_item)
        return {
            "worker_version": WORKER_VERSION,
            "source": {
                "odb_path": path_text,
                "size_bytes": os.path.getsize(native),
                "read_only": True,
            },
            "steps": {
                "count": len(native_step_keys),
                "items": step_items,
                "truncated": len(native_step_keys) > max_steps,
            },
            "limits": {
                "max_steps": max_steps,
                "max_regions_per_step": max_regions,
                "max_variables_per_region": max_variables,
            },
        }
    finally:
        _close_odb(odb)


def extract_history(params):
    """Extract one exact scalar history series with a hard row limit."""

    step_name = _required_text(params, "step")
    region_name = _required_text(params, "region")
    variable_name = _required_text(params, "variable")
    max_rows = _bounded_integer(
        params, "max_rows", DEFAULT_MAX_ROWS, HARD_MAX_ROWS
    )

    path_text, native, odb = _open_read_only_odb(params)
    try:
        native_step_key = _exact_repository_key(
            odb.steps, step_name, "STEP_NOT_FOUND", "step"
        )
        step = odb.steps[native_step_key]
        native_region_key = _exact_repository_key(
            step.historyRegions,
            region_name,
            "REGION_NOT_FOUND",
            "history region",
        )
        region = step.historyRegions[native_region_key]
        native_variable_key = _exact_repository_key(
            region.historyOutputs,
            variable_name,
            "VARIABLE_NOT_FOUND",
            "history variable",
        )
        history = region.historyOutputs[native_variable_key]
        available_rows = len(history.data)
        if available_rows > max_rows:
            raise OdbContractError(
                "ROW_LIMIT_EXCEEDED",
                "history data exceeds max_rows; no partial data were returned",
                {"available_rows": available_rows, "max_rows": max_rows},
            )

        rows = []
        for index, pair in enumerate(history.data):
            try:
                pair_length = len(pair)
            except TypeError:
                pair_length = -1
            if pair_length != 2:
                raise OdbContractError(
                    "INVALID_HISTORY_ROW",
                    "history row must contain frame value and scalar value",
                    {"row_index": index},
                )
            try:
                frame_value = float(pair[0])
                scalar_value = float(pair[1])
            except (TypeError, ValueError, OverflowError):
                raise OdbContractError(
                    "NON_SCALAR_HISTORY_DATA",
                    "history output must be scalar",
                    {"row_index": index},
                )
            if not _is_finite(frame_value) or not _is_finite(scalar_value):
                raise OdbContractError(
                    "NON_FINITE_HISTORY_DATA",
                    "NaN and Infinity are not allowed",
                    {"row_index": index},
                )
            rows.append([frame_value, scalar_value])

        return {
            "worker_version": WORKER_VERSION,
            "source": {
                "odb_path": path_text,
                "size_bytes": os.path.getsize(native),
                "read_only": True,
            },
            "selection": {
                "step": _plain_text(native_step_key),
                "region": _plain_text(native_region_key),
                "variable": _plain_text(native_variable_key),
            },
            "columns": ["frame_value", "value"],
            "row_count": len(rows),
            "max_rows": max_rows,
            "truncated": False,
            "nan_count": 0,
            "rows": rows,
        }
    finally:
        _close_odb(odb)


METHODS = {
    "inventory": inventory,
    "extract_history": extract_history,
}


def dispatch_request(request):
    if not isinstance(request, dict):
        raise OdbContractError("INVALID_REQUEST", "JSON root must be an object")
    if request.get("jsonrpc") != JSONRPC_VERSION:
        raise OdbContractError("INVALID_JSONRPC_VERSION")
    if "id" not in request or request.get("id") is None:
        raise OdbContractError("REQUEST_ID_REQUIRED")
    method_name = request.get("method")
    if not isinstance(method_name, STRING_TYPES) or method_name not in METHODS:
        raise OdbContractError(
            "METHOD_NOT_FOUND",
            "Only inventory and extract_history are allowed",
            {"method": method_name},
        )
    params = request.get("params", {})
    if not isinstance(params, dict):
        raise OdbContractError("PARAMS_MUST_BE_OBJECT")
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": request["id"],
        "result": METHODS[method_name](params),
    }


def _read_json(path):
    with io.open(path, "r", encoding="utf-8") as stream:
        return json.load(stream)


def _atomic_json(path, value):
    temporary = path + ".tmp"
    if os.path.exists(path) or os.path.exists(temporary):
        raise OdbContractError(
            "RESPONSE_TARGET_EXISTS",
            "response and temporary targets must not already exist",
            {"response_path": path},
        )
    payload = json.dumps(value, ensure_ascii=True, indent=2, allow_nan=False)
    if not isinstance(payload, bytes):
        payload = payload.encode("utf-8")
    with open(temporary, "wb") as stream:
        stream.write(payload)
        stream.write(b"\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(temporary, path)


def _script_arguments():
    if "--" in sys.argv:
        values = sys.argv[sys.argv.index("--") + 1 :]
    else:
        values = sys.argv[-2:]
    if len(values) != 2:
        raise OdbContractError(
            "BAD_ARGUMENTS",
            "expected request.json and response.json paths",
            {"argument_count": len(values)},
        )
    return _native_path(values[0]), _native_path(values[1])


def main():
    try:
        request_path, response_path = _script_arguments()
    except OdbContractError as exc:
        sys.__stderr__.write("ODB_WORKER_%s\n" % exc.code)
        return 10

    request = None
    try:
        request = _read_json(request_path)
        response = dispatch_request(request)
        exit_code = 0
    except OdbContractError as exc:
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request.get("id") if isinstance(request, dict) else None,
            "error": {
                "code": -32020,
                "message": exc.code,
                "data": dict(exc.data, detail=exc.message),
            },
        }
        exit_code = 20
    except Exception as exc:
        response = {
            "jsonrpc": JSONRPC_VERSION,
            "id": request.get("id") if isinstance(request, dict) else None,
            "error": {
                "code": -32099,
                "message": "WORKER_INTERNAL_ERROR",
                "data": {"type": type(exc).__name__, "detail": _plain_text(exc)},
            },
        }
        exit_code = 30

    try:
        _atomic_json(response_path, response)
    except OdbContractError as exc:
        sys.__stderr__.write("ODB_WORKER_%s\n" % exc.code)
        return 30
    sys.__stderr__.write(
        "ODB_WORKER_COMPLETE method=%s exit=%d\n"
        % (
            request.get("method") if isinstance(request, dict) else "unknown",
            exit_code,
        )
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
