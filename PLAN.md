# Portable Abaqus automation execution plan

Version: 2.0
Updated: 2026-08-16
Target: Model-driven-pile-all-18 / Job-5

## Outcome

The repository must let an authorized user reproduce the safe Phase 0 chain on
another Windows/Abaqus 2022 computer without copying machine paths, credentials,
customer documents or generated results into Git.

Project completion remains separate from repository completion. The repository
can be complete while the engineering solve is still NOT_READY.

## Milestones

| Milestone | Scope | Status | Exit condition |
|---|---|---|---|
| R0 | Public repository packaging | COMPLETE | Portable config, docs, tests, exclusions and CI pass |
| R1 | New-machine bootstrap | PENDING PER MACHINE | Private inputs hash correctly; local config generated without overwrite |
| R2 | New-machine Phase 0 | PENDING PER MACHINE | Tests, Abaqus ping, staged summary and deep fingerprint pass |
| R3 | Resource and compiler gate | BLOCKED | Fresh CPU/RAM/disk/NTFS, Explicit Token and supported compiler verified |
| M1 | Revised VUSDFLD implementation | BLOCKED | Approved new FOR, unit calibration and registered SHA-256 |
| G2 | Four INP equivalence and Data Check | BLOCKED | Only allowed switches differ; 0 ERROR; compile/link succeeds |
| G3-G5 | Gravity, one-blow and ten-blow short runs | BLOCKED | Stability, energy and deformation-speed gates pass |
| G6 | Four full comparison runs | BLOCKED | All jobs finish at the sensor target and artifacts are complete |
| G7 | ODB extraction and acceptance | BLOCKED | Components close and approved DR/DW/RW criteria are evaluated |

## R0 repository deliverables

- Safe external CPython bridge and fixed Abaqus Python 2.7 workers.
- Machine-local bootstrap and config schemas.
- Private input manifest with size/SHA-256, without binary payloads.
- Phase 0 reproduction, resource preflight and readiness scripts.
- Versioned workspace creation.
- Four-case and output-contract examples.
- Standard-library analysis code and contract tests.
- Public-tree scanner and GitHub Actions.
- README, Agents.md, PLAN.md, DECISIONS.md and operational docs.

## Per-machine sequence

1. Clone a reviewed commit.
2. Obtain authorized CAE and FOR through a private channel.
3. Verify local NTFS and separate source/runtime directories.
4. Run scripts/bootstrap.ps1.
5. Run scripts/run-phase0.ps1.
6. Read the generated readiness report.
7. If Phase 0 passed, optionally create one versioned workspace.
8. Stop. Do not proceed to Data Check or solver work in this release.

## Engineering repair route after authorization

The next model revision should be a new FOR path and SHA, not an in-place edit.
The planned minimum repair is:

- initialize the depth factor once and carry it with the material state;
- calculate softening history from plastic strain increments;
- calculate rate effect from total strain increments;
- copy old state variables before updating the defined entries;
- audit initial CEL volume fraction, contact offsets and pile-toe mesh;
- keep all physical parameters frozen unless a decision in DECISIONS.md is
  explicitly approved.

The original failed job stopped in the gravity step before hammer loading. A
future repair must therefore pass the gravity gates before VUAMP is treated as
a possible runtime cause.

## Non-negotiable gates

- G0: source hashes, staged-copy isolation and deep audit.
- G1: approved FOR, saved-copy audit and exact model diff.
- G2: normalized four-case INP equivalence, Data Check and compile/link.
- G3: gravity through 0.10 seconds, beyond the prior failure point.
- G4: complete gravity step.
- G5: one and ten hammer blows.
- G6: four full cases to the target sensor position.
- G7: exact ODB inventory, bounded extraction, resistance closure and approved
  acceptance formulas.

No gate may be skipped by changing strength, gravity, mass scaling, precision or
deformation-speed limits.
