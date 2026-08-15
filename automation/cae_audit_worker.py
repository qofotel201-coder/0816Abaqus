# -*- coding: utf-8 -*-
"""Read-only, fixed-method Abaqus/CAE 2022 audit worker.

This module is executed by the Abaqus Python 2.7 kernel.  The external
orchestrator must copy a CAE into an isolated run directory before calling
``model.deep_audit``.  Abaqus can change a CAE merely by opening it, so this
worker deliberately refuses a request that does not carry the staged-copy
confirmation flag.
"""

from __future__ import print_function

import hashlib
import io
import json
import math
import os
import platform
import socket
import sys
import time
import traceback

from abaqus import mdb, openMdb, session
from caeModules import *


WORKER_VERSION = "1.0.0"
JSONRPC_VERSION = "2.0"
MAX_ITEMS_LIMIT = 2000
MAX_TABLE_ROWS = 2000
MAX_KEYWORD_BLOCKS = 2000
MAX_KEYWORD_CHARS = 2 * 1024 * 1024


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


def _class_name(value):
    return value.__class__.__name__


def _repo_key(repository, requested):
    wanted = _text(requested)
    for key in list(repository.keys()):
        if _text(key) == wanted:
            return key
    return None


def _repo_names(repository, max_items):
    names = sorted([_text(name) for name in list(repository.keys())])
    return {
        "count": len(names),
        "items": names[:max_items],
        "truncated": len(names) > max_items,
    }


def _finite_number(value):
    try:
        numeric = float(value)
        if math.isnan(numeric) or math.isinf(numeric):
            return _text(value)
        return value
    except Exception:
        return None


def _plain(value, depth=0, max_sequence=200):
    """Convert selected Abaqus values to bounded JSON-safe data."""
    if depth > 4:
        return _text(value)
    if value is None or isinstance(value, bool):
        return value
    # Abaqus SymbolicConstant objects can report themselves as numeric
    # subclasses while their repr is an unquoted token such as OFF/ON.  Keep
    # only exact built-in numeric types as JSON numbers; stringify subclasses.
    if type(value) in (int, long, float):
        finite = _finite_number(value)
        return finite if finite is not None else _text(value)
    if isinstance(value, (int, long, float)):
        return _text(value)
    try:
        unicode_type = unicode
    except NameError:
        unicode_type = str
    if isinstance(value, (str, unicode_type)):
        return _text(value)
    if isinstance(value, dict):
        output = {}
        keys = sorted(list(value.keys()), key=_text)[:max_sequence]
        for key in keys:
            output[_text(key)] = _plain(value[key], depth + 1, max_sequence)
        return output
    if isinstance(value, (tuple, list)):
        rows = list(value)
        return [_plain(item, depth + 1, max_sequence) for item in rows[:max_sequence]]
    # Several Abaqus API values are sequence proxies rather than tuple/list.
    # Convert only bounded, sized iterables; ordinary model objects fall back
    # to their stable textual representation.
    try:
        if hasattr(value, "__len__") and hasattr(value, "__iter__"):
            rows = list(value)
            return [_plain(item, depth + 1, max_sequence) for item in rows[:max_sequence]]
    except Exception:
        pass
    return _text(value)


def _known_attrs(obj, names):
    result = {}
    for name in names:
        try:
            result[name] = _plain(getattr(obj, name))
        except Exception:
            pass
    return result


def _table_component(obj, max_rows):
    result = {"class": _class_name(obj)}
    result.update(_known_attrs(obj, (
        "dependencies", "temperatureDependency", "type", "rate", "law",
        "moduli", "strainRangeDependency", "properties", "time", "n",
        "numStateVariables", "deleteVar", "outputVariables",
    )))
    try:
        rows = list(obj.table)
        result["table_row_count"] = len(rows)
        result["table"] = _plain(rows[:max_rows], max_sequence=max_rows)
        result["table_truncated"] = len(rows) > max_rows
    except Exception:
        result["table_row_count"] = None
    return result


def _material_audit(name, material, max_rows):
    result = {"name": _text(name), "class": _class_name(material), "components": {}}
    component_names = (
        "density", "elastic", "plastic", "depvar", "userDefinedField",
        "mohrCoulombPlasticity", "druckerPrager", "hyperelastic",
        "viscoelastic", "concreteDamagedPlasticity", "capPlasticity",
        "clayPlasticity", "porousElastic", "permeability", "expansion",
        "conductivity", "specificHeat", "damping",
    )
    for component_name in component_names:
        try:
            component = getattr(material, component_name)
            result["components"][component_name] = _table_component(component, max_rows)
        except Exception:
            pass
    # Combined hardening is nested under Plastic in several releases.
    try:
        hardening = material.plastic.combinedHardening
        result["components"]["plastic.combinedHardening"] = _table_component(
            hardening, max_rows
        )
    except Exception:
        pass
    try:
        hardening = material.plastic.kinematicHardening
        result["components"]["plastic.kinematicHardening"] = _table_component(
            hardening, max_rows
        )
    except Exception:
        pass
    return result


def _part_audit(name, part):
    result = {
        "name": _text(name),
        "class": _class_name(part),
        "nodes": len(part.nodes),
        "elements": len(part.elements),
        "cells": len(part.cells),
        "faces": len(part.faces),
        "edges": len(part.edges),
        "sets": _repo_names(part.sets, 1000),
        "surfaces": _repo_names(part.surfaces, 1000),
        "element_types": [],
    }
    types = {}
    for element in part.elements:
        try:
            label = _text(element.type)
        except Exception:
            label = "UNKNOWN"
        types[label] = types.get(label, 0) + 1
    result["element_types"] = [
        {"type": key, "count": types[key]} for key in sorted(types.keys())
    ]
    try:
        bbox = part.nodes.getBoundingBox()
        result["node_bounding_box"] = _plain(bbox)
    except Exception:
        pass
    return result


def _state_values(obj, step_names, attrs):
    states = {}
    for step_name in step_names:
        value = None
        source = None
        try:
            value = obj.getValuesInStep(stepName=_text(step_name))
            source = "getValuesInStep"
        except Exception:
            try:
                value = obj.getState(stepName=_text(step_name))
                source = "getState"
            except Exception:
                continue
        if isinstance(value, dict):
            serialized = _plain(value)
        else:
            serialized = {"class": _class_name(value)}
            serialized.update(_known_attrs(value, attrs))
            if len(serialized) == 1:
                serialized["value"] = _plain(value)
        states[_text(step_name)] = {"source": source, "values": serialized}
    return states


def _named_objects(repository, max_items, attrs, step_names=None):
    rows = []
    keys = sorted(list(repository.keys()), key=_text)
    for key in keys[:max_items]:
        obj = repository[key]
        row = {"name": _text(key), "class": _class_name(obj)}
        row.update(_known_attrs(obj, attrs))
        if step_names:
            states = _state_values(obj, step_names, attrs)
            if states:
                row["step_states"] = states
        rows.append(row)
    return {
        "count": len(keys),
        "items": rows,
        "truncated": len(keys) > max_items,
    }


def _job_audit(name, job):
    result = {"name": _text(name), "class": _class_name(job)}
    result.update(_known_attrs(job, (
        "model", "description", "type", "queue", "waitHours", "waitMinutes",
        "memory", "memoryUnits", "getMemoryFromAnalysis", "numCpus",
        "numDomains", "parallelizationMethodExplicit", "multiprocessingMode",
        "explicitPrecision", "nodalOutputPrecision", "resultsFormat",
        "userSubroutine", "scratch", "echoPrint", "modelPrint", "contactPrint",
        "historyPrint", "activateLoadBalancing", "numThreadsPerMpiProcess",
    )))
    path = result.get("userSubroutine")
    if path:
        native = _native_path(path)
        result["user_subroutine_exists"] = bool(os.path.isfile(native))
    else:
        result["user_subroutine_exists"] = False
    return result


def _canonical_sha256(value):
    payload = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    if not isinstance(payload, bytes):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _keyword_audit(model, max_blocks, max_chars):
    """Capture bounded, physics-relevant keyword blocks without mesh data."""
    allowed = (
        "*AMPLITUDE", "*BOUNDARY", "*BULK VISCOSITY", "*CLOAD",
        "*CONTACT", "*COUPLING", "*DAMPING", "*DENSITY", "*DEPVAR",
        "*DYNAMIC", "*ELASTIC", "*ELEMENT OUTPUT", "*ENERGY OUTPUT",
        "*EULERIAN", "*FIELD", "*FRICTION", "*GRAVITY",
        "*HISTORY OUTPUT", "*INITIAL CONDITIONS", "*MASS SCALING",
        "*MATERIAL", "*NODE OUTPUT", "*OUTPUT", "*PLASTIC",
        "*RIGID BODY", "*SECTION", "*SOLID SECTION", "*STEP",
        "*SURFACE BEHAVIOR", "*SURFACE INTERACTION", "*SURFACE OUTPUT",
        "*TIE", "*USER DEFINED FIELD", "*VARIABLE MASS SCALING",
    )
    try:
        model.keywordBlock.synchVersions(storeNodesAndElements=False)
        raw_blocks = list(model.keywordBlock.sieBlocks)
    except Exception as exc:
        return {"available": False, "error": _text(exc)}
    selected = []
    total_chars = 0
    matching_count = 0
    truncated = False
    for index, block in enumerate(raw_blocks):
        text_block = _text(block)
        first_line = text_block.splitlines()[0].strip().upper() if text_block else ""
        if not any(first_line.startswith(prefix) for prefix in allowed):
            continue
        matching_count += 1
        if len(selected) >= max_blocks or total_chars + len(text_block) > max_chars:
            truncated = True
            continue
        selected.append({
            "index": index,
            "keyword": first_line.split(",", 1)[0],
            "text": text_block,
        })
        total_chars += len(text_block)
    canonical = [row["text"] for row in selected]
    return {
        "available": True,
        "source_block_count": len(raw_blocks),
        "matching_block_count": matching_count,
        "captured_block_count": len(selected),
        "captured_chars": total_chars,
        "truncated": truncated,
        "blocks": selected,
        "sha256": _canonical_sha256(canonical),
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
        raise RpcError(-32020, "RESPONSE_TARGET_ALREADY_EXISTS")
    os.rename(temporary, path)


def _open_staged(params):
    if params.get("staged_copy_confirmed") is not True:
        raise RpcError(-32010, "STAGED_COPY_CONFIRMATION_REQUIRED")
    path = params.get("cae_path")
    if not path:
        raise RpcError(-32602, "CAE_PATH_REQUIRED")
    native = _native_path(path)
    if not os.path.isfile(native):
        raise RpcError(-32001, "CAE_NOT_FOUND", {"cae_path": path})
    if os.path.splitext(native)[1].lower() != ".cae":
        raise RpcError(-32602, "CAE_EXTENSION_REQUIRED")
    started = time.time()
    try:
        opened = openMdb(pathName=native)
    except Exception as exc:
        raise RpcError(-32002, "CAE_OPEN_FAILED", {"message": _text(exc)})
    return opened, {
        "cae_path": _text(path),
        "open_seconds": round(time.time() - started, 3),
        "staged_copy_confirmed": True,
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
        "read_only_worker": True,
        "worker_version": WORKER_VERSION,
        "python_version": sys.version.replace("\n", " "),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "hostname": socket.gethostname(),
        "cwd": os.getcwd(),
        "session": session_info,
        "methods": sorted(METHODS.keys()),
    }


def model_deep_audit(params):
    model_name = params.get("model_name")
    if not model_name:
        raise RpcError(-32602, "MODEL_NAME_REQUIRED")
    max_items = int(params.get("max_items", 500))
    max_rows = int(params.get("max_table_rows", 1000))
    max_keyword_blocks = int(params.get("max_keyword_blocks", 1000))
    max_keyword_chars = int(params.get("max_keyword_chars", 1024 * 1024))
    if max_items < 1 or max_items > MAX_ITEMS_LIMIT:
        raise RpcError(-32602, "MAX_ITEMS_OUT_OF_RANGE")
    if max_rows < 1 or max_rows > MAX_TABLE_ROWS:
        raise RpcError(-32602, "MAX_TABLE_ROWS_OUT_OF_RANGE")
    if max_keyword_blocks < 1 or max_keyword_blocks > MAX_KEYWORD_BLOCKS:
        raise RpcError(-32602, "MAX_KEYWORD_BLOCKS_OUT_OF_RANGE")
    if max_keyword_chars < 1024 or max_keyword_chars > MAX_KEYWORD_CHARS:
        raise RpcError(-32602, "MAX_KEYWORD_CHARS_OUT_OF_RANGE")

    opened, source = _open_staged(params)
    model_key = _repo_key(opened.models, model_name)
    if model_key is None:
        raise RpcError(-32003, "MODEL_NOT_FOUND", {
            "available_models": _repo_names(opened.models, max_items),
        })
    model = opened.models[model_key]
    step_names = sorted([_text(key) for key in list(model.steps.keys())])

    audit = {
        "schema_version": "1.0.0",
        "worker_version": WORKER_VERSION,
        "source": source,
        "model_name": _text(model_key),
        "repositories": {},
        "parts": [],
        "materials": [],
        "steps": {},
        "amplitudes": {},
        "loads": {},
        "boundary_conditions": {},
        "interactions": {},
        "interaction_properties": {},
        "constraints": {},
        "predefined_fields": {},
        "field_output_requests": {},
        "history_output_requests": {},
        "assembly": {},
        "jobs_for_model": [],
        "keyword_audit": {},
    }

    repositories = (
        ("parts", model.parts), ("materials", model.materials),
        ("sections", model.sections), ("steps", model.steps),
        ("amplitudes", model.amplitudes), ("loads", model.loads),
        ("boundary_conditions", model.boundaryConditions),
        ("interactions", model.interactions),
        ("interaction_properties", model.interactionProperties),
        ("constraints", model.constraints),
        ("predefined_fields", model.predefinedFields),
        ("field_output_requests", model.fieldOutputRequests),
        ("history_output_requests", model.historyOutputRequests),
    )
    for name, repository in repositories:
        audit["repositories"][name] = _repo_names(repository, max_items)

    for key in sorted(list(model.parts.keys()), key=_text)[:max_items]:
        audit["parts"].append(_part_audit(key, model.parts[key]))
    for key in sorted(list(model.materials.keys()), key=_text)[:max_items]:
        audit["materials"].append(_material_audit(key, model.materials[key], max_rows))

    audit["steps"] = _named_objects(model.steps, max_items, (
        "previous", "description", "timePeriod", "nlgeom", "massScaling",
        "improvedDtMethod", "scaleFactor", "linearBulkViscosity",
        "quadBulkViscosity", "maxIncrement", "timeIncrementationMethod",
    ))
    audit["amplitudes"] = _named_objects(model.amplitudes, max_items, (
        "timeSpan", "smooth", "data", "properties", "numVariables",
    ))
    common_attrs = (
        "createStepName", "region", "amplitude", "distributionType",
        "field", "localCsys", "u1", "u2", "u3", "ur1", "ur2", "ur3",
        "cf1", "cf2", "cf3", "magnitude", "components", "suppressed",
    )
    audit["loads"] = _named_objects(
        model.loads, max_items, common_attrs, step_names=step_names
    )
    audit["boundary_conditions"] = _named_objects(
        model.boundaryConditions, max_items, common_attrs, step_names=step_names
    )
    audit["interactions"] = _named_objects(model.interactions, max_items, (
        "createStepName", "master", "slave", "main", "secondary",
        "interactionProperty", "includedPairs", "excludedPairs", "suppressed",
    ), step_names=step_names)
    audit["interaction_properties"] = _named_objects(
        model.interactionProperties, max_items, (
            "tangentialBehavior", "normalBehavior", "cohesiveBehavior",
        )
    )
    audit["constraints"] = _named_objects(model.constraints, max_items, (
        "controlPoint", "surface", "tieRotations", "adjust", "suppressed",
    ))
    audit["predefined_fields"] = _named_objects(
        model.predefinedFields, max_items, common_attrs, step_names=step_names
    )
    output_attrs = (
        "createStepName", "variables", "region", "frequency", "timeInterval",
        "numIntervals", "sensor", "filter", "sectionPoints", "rebar",
        "interactions", "contourIntegral", "suppressed",
    )
    audit["field_output_requests"] = _named_objects(
        model.fieldOutputRequests, max_items, output_attrs, step_names=step_names
    )
    audit["history_output_requests"] = _named_objects(
        model.historyOutputRequests, max_items, output_attrs, step_names=step_names
    )

    assembly = model.rootAssembly
    audit["assembly"] = {
        "instances": _named_objects(assembly.instances, max_items, (
            "partName", "dependent", "translation", "rotation",
        )),
        "sets": _repo_names(assembly.sets, max_items),
        "surfaces": _repo_names(assembly.surfaces, max_items),
        "reference_points": len(assembly.referencePoints),
    }

    for key in sorted(list(opened.jobs.keys()), key=_text):
        job = opened.jobs[key]
        try:
            if _text(job.model) == _text(model_key):
                audit["jobs_for_model"].append(_job_audit(key, job))
        except Exception:
            pass

    audit["keyword_audit"] = _keyword_audit(
        model, max_keyword_blocks, max_keyword_chars
    )

    fingerprint_input = dict(audit)
    fingerprint_input.pop("source", None)
    fingerprint_input.pop("worker_version", None)
    audit["model_fingerprint_sha256"] = _canonical_sha256(fingerprint_input)
    return audit


METHODS = {
    "system.capabilities": system_capabilities,
    "model.deep_audit": model_deep_audit,
}


def main():
    separator = None
    try:
        separator = sys.argv.index("--")
    except ValueError:
        pass
    arguments = sys.argv[separator + 1:] if separator is not None else sys.argv[-2:]
    if len(arguments) != 2:
        sys.__stderr__.write("CAE_AUDIT_WORKER_BAD_ARGUMENTS: %r\n" % (arguments,))
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
    sys.__stderr__.write("CAE_AUDIT_WORKER_COMPLETE exit=%s\n" % exit_code)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
