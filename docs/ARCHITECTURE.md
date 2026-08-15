# Architecture

## Two Python runtimes

The repository deliberately separates:

- native Windows CPython 3.10+, which validates paths, hashes, manifests and
  launches bounded requests;
- Abaqus 2022 embedded Python 2.7, which owns CAE repositories and odbAccess.

Do not import abaqus, caeModules or odbAccess from ordinary CPython.

## Components

| Component | Runtime | Responsibility | Write scope |
|---|---|---|---|
| abaqus_bridge.py | Windows CPython 3 | Stage a CAE, launch a fixed worker, verify source integrity | Request-owned bridge directory |
| abaqus_worker.py | Abaqus Python 2.7 | Ping, list models/jobs, bounded model summary | None except response |
| automation/cae_audit_worker.py | Abaqus Python 2.7 | Deep read audit and normalized fingerprint | None except response |
| automation/workspace_worker.py | Abaqus Python 2.7 | Allowlisted clone, saveAs, FOR binding and writeInput | Registered workspace operation |
| automation/odb_worker.py | Abaqus Python 2.7 | Read-only ODB inventory and exact history extraction | Response only |
| automation/orchestrator.py | CPython 3 | Source manifests and two-phase workspace management | Configured workspace root |
| automation/job_manager.py | CPython 3 | Persistent job metadata and hard-gated solver surface | Registered job directories only |
| automation/compare_cases.py | CPython 3 | Same-depth interpolation, DR/DW/RW, hammer peaks and closure | Caller-selected report |

## CAE isolation

The bridge records source size, mtime and optionally SHA-256, copies the CAE
into a new UUID request directory, opens only that copy, closes the Abaqus
process, rechecks the source, then removes exactly the request-owned temporary
CAE. Runtime logs remain in the local bridge directory.

The copy is mandatory. The stage-copy command flag is retained only for command
compatibility and cannot select direct-source opening.

## Persistent workspaces

Workspace creation is a two-step operation:

1. plan generates a UUID destination, full source manifest, expiring plan hash
   and one-time confirmation token;
2. commit revalidates the plan, token and source, claims the operation, copies
   the CAE to a new directory, and writes append-only manifests.

Existing destinations are rejected. Partial failures are recorded and not
silently cleaned.

## Solver boundary

job.data_check and job.submit are exposed only so callers receive a structured
BLOCKED response. They are not launch implementations. The current release also
keeps job termination disabled.

This boundary must not be weakened in the audit or workspace workers. Any future
solver launcher requires a separate threat model, resource gate, registered
PID-tree lifecycle and positive integration tests.
