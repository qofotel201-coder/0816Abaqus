# Reproducibility guide

## Reproduction levels

Level A, portable contracts:

- works on Linux, macOS, WSL and Windows;
- validates schemas, security boundaries and numerical comparison helpers;
- does not require Abaqus or private inputs.

Level B, Phase 0 integration:

- requires native Windows CPython, Abaqus/CAE 2022 and a CAE license;
- requires the private CAE and user subroutine matching the public hashes;
- validates connection, staged-copy integrity and model fingerprint;
- never starts a solver.

Level C, engineering solution:

- is not implemented in this repository release;
- requires compiler, Explicit license, approved revised FOR, Data Check, staged
  short runs, four full cases and real ODB extraction.

## Level A

    python -m pip install -r requirements-dev.txt
    python -B -m unittest discover -s tests -v
    python -B scripts/verify_public_tree.py

Expected result: all tests pass and the safety scanner reports zero findings.

## Level B prerequisites

- Clone is on a local NTFS volume.
- Source and runtime roots are disjoint.
- Source CAE was obtained privately and has never been opened in place.
- User subroutine is authorized and has the expected hash.
- Abaqus/CAE 2022 command exists.
- CAE license is available.
- At least 8 GiB memory is available at the time of testing.
- At least 140 GiB disk is available for later staged work.

Run scripts/bootstrap.ps1, followed by scripts/run-phase0.ps1 as shown in the
root README.

## Phase 0 acceptance

Required PASS checks:

- pure Python contract tests;
- Abaqus worker connection and embedded Python 2.7;
- exact model/job discovery;
- source CAE pre/post hash and mtime preservation;
- staged CAE removal;
- normalized deep model fingerprint;
- workspace worker reports no solver submission;
- odbAccess loads and rejects a missing ODB with the expected structured error.

The Phase 0 result is PASS_WITH_NO_SOLVER. It does not imply overall readiness.

## Workspace copy

scripts/create_workspace.py creates about 1.26 GB of persistent data and must
only be run after disk review. It writes a new UUID directory and never replaces
an existing target.

The output report must show:

- source hash unchanged before and after;
- workspace status CREATED;
- inspection status READY;
- gate GO;
- solver_submitted false.

## Evidence rules

Every target computer generates its own:

- config/local source manifest;
- bridge request/response/audit directories;
- Phase 0 report;
- resource report;
- readiness report;
- optional workspace manifest.

Do not copy an older computer's readiness report and treat it as evidence.
Runtime evidence contains machine details and must remain outside public Git.

## Expected differences

Permitted machine differences include:

- installation drive and directories;
- CPython patch version and executable path;
- host name, CPU, memory and disk;
- bridge request UUIDs and timestamps.

The following must not differ:

- source CAE size and SHA-256;
- logical model and Job names;
- Abaqus major release 2022;
- embedded Python major/minor 2.7;
- normalized model fingerprint;
- safety flags disabling solver execution and job termination.
