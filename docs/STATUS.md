# Current status

Updated: 2026-08-16

## Repository status

The portable repository contains the complete Phase 0 source, schemas, tests,
configuration generator, fresh-machine resource/readiness scripts and CI
workflow. Private binary inputs and user subroutine source are excluded.

## Portable publication verification

Before publication, the curated repository passed:

- 43 of 43 tests under Linux CPython;
- 43 of 43 tests under native Windows CPython 3.11;
- public-tree path, credential, extension and 20 MiB checks with zero findings;
- real Abaqus Phase 0 with all six checks PASS;
- exact normalized model fingerprint comparison;
- source CAE pre/post integrity and staged-copy removal;
- a fresh two-phase 1.26 GB workspace create/inspect test.

The portable Phase 0 result was PASS_WITH_NO_SOLVER. The same fresh run's
readiness result was NOT_READY: 6 PASS, 1 WARN, 1 FAIL and 4 BLOCKED. No Data
Check or solver job was submitted.

## Evidence established on the originating computer

- External Python launched Abaqus/CAE 2022 successfully.
- Abaqus embedded Python reported 2.7.15.
- Model-driven-pile-all-18 and Job-5 were opened only through staged copies.
- Bounded summary and deep audit completed.
- Source CAE remained unchanged while the staged copy changed and was removed.
- A versioned 1.26 GB workspace was created with matching hashes.
- A workspace clone/saveAs/FOR binding/writeInput test succeeded without solver
  submission.
- 38 original unit/security tests passed.
- The final local consistency review passed all ten repository-governance
  checks while retaining a NO_GO solver gate.

The originating-computer preparation result was NOT_READY: 7 PASS, 3 WARN,
1 FAIL and 5 BLOCKED. The dynamic memory observation from that report is not
portable and must be measured again on every computer.

## Still unverified

- approved revised FOR compile and link;
- Data Check with zero errors;
- registered solver submit, wait and controlled termination lifecycle;
- positive extraction from a real completed ODB;
- B00/B10/B01/B11 materialization and normalized equivalence;
- complete gravity, hammer and full-penetration runs;
- component resistance closure and final 40 percent acceptance.

No valid project ODB exists in this repository, and no engineering result should
be inferred from the automation tests.

## Preparation interpretation

Repository setup complete does not mean engineering preparation complete.

- A new computer is Phase 0 capable only after its own run reports
  PASS_WITH_NO_SOLVER.
- A computer is READY only if every required readiness item is PASS.
- With this release, Data Check, Explicit Token validation, real ODB and the
  four-case matrix remain BLOCKED, so the expected overall result is NOT_READY.
