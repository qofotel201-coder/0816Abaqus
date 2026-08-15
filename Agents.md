# Repository agent and operator rules

Version: 2.0
Scope: this public, portable repository

This file is mandatory guidance for future Codex agents, automation authors,
reviewers, and Abaqus operators. It describes execution constraints; it is not
evidence that a planned capability works.

## Start-of-work checklist

Read, in order:

1. README.md
2. Agents.md
3. PLAN.md
4. DECISIONS.md
5. docs/STATUS.md
6. the current machine-local manifest and latest gate report

User instructions take precedence, but no instruction silently authorizes
publication of private inputs, overwriting frozen evidence, or solver use.

## Public/private boundary

Never commit or attach:

- CAE, ODB, INP, RAR, solver logs, Abaqus journals or replay files;
- config/local, runtime workspaces, bridge run directories or reports;
- customer screenshots, third-party papers or customer HTML exports;
- user subroutine source unless its owner explicitly approves public release;
- credentials, one-time commit tokens, hostnames, user-profile paths or license
  inventory.

Public configuration may contain logical IDs, required sizes and SHA-256 values.
Machine paths belong only in ignored local configuration.

Stage files explicitly. Do not publish by copying an entire source directory or
by blindly staging every file.

## Runtime separation

- External orchestration requires CPython 3.10 or newer.
- Abaqus 2022 workers must remain compatible with embedded Python 2.7.
- Abaqus modules are imported only inside Abaqus workers.
- Fixed method tables are mandatory. Do not add arbitrary code, shell strings,
  eval, exec or dynamic import endpoints.
- Audit, workspace mutation, job control and ODB extraction stay in separate
  execution surfaces.

## Frozen-source rules

- Validate input size and SHA-256 before any operation.
- Never call openMdb on the frozen CAE.
- Use a UUID/request-owned staged copy for every CAE audit.
- Record source size, mtime and SHA-256 before and after the call.
- Any unexpected source change makes the global gate NO_GO.
- Do not use a previously opened or modified CAE as a new baseline.

## Writes and process control

- Refuse existing destinations; never silently overwrite.
- Persistent model changes require plan/commit, matching plan hash, expected
  workspace version and a single-use confirmation token.
- Preserve failed operations and manifests for local audit.
- Do not delete runtime evidence automatically.
- Do not terminate Abaqus processes by executable name.
- Solver submission and job termination remain disabled until separate,
  reviewed implementations and positive lifecycle tests exist.

## Engineering status rules

Use only these meanings:

- VERIFIED: positive, saved evidence proves the complete stated capability.
- PARTIAL: code or contract tests exist, but a required positive integration
  result is missing.
- BLOCKED: execution is intentionally forbidden or a prerequisite is absent.
- NOT_READY: at least one required readiness item is WARN, FAIL or BLOCKED.

Never describe the repository, an interface, or a unit test as proof that a
solver run succeeded. The current release has no valid project ODB.

## Validation before a change is published

Run:

    python -B -m unittest discover -s tests -v
    python -B scripts/verify_public_tree.py

On an authorized Abaqus host also run scripts/run-phase0.ps1 and retain the
fresh report outside Git. Do not reuse another computer's readiness result.

Before commit:

- inspect git status and diff;
- use an explicit staging allowlist;
- confirm no file exceeds 20 MiB;
- scan the exact staged tree for credentials and private inputs;
- confirm the branch contains no solver results or local configuration.

## Documentation and handoff

Update PLAN.md, DECISIONS.md and docs/STATUS.md whenever a gate or capability
changes. A handoff must state:

- branch and commit;
- validation commands and results;
- current readiness;
- whether any solver was started;
- remaining blockers;
- artifact paths or hashes without exposing private paths.
