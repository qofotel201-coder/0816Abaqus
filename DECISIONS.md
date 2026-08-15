# Decision register

Version: 2.0
Updated: 2026-08-16

Only decisions that change public scope, engineering meaning, acceptance
criteria or resource commitments belong here. PENDING items may not be inferred
as approved.

## Repository decisions

| ID | Decision | Status | Rule |
|---|---|---|---|
| R-001 | Use a public repository with private external model inputs | APPROVED | Git contains source/config/docs only; binaries are supplied separately |
| R-002 | Publish the customer user subroutine source | NOT APPROVED | Keep it external and verify size/SHA-256 |
| R-003 | Parameterize machine paths | APPROVED | bootstrap.py generates ignored local absolute paths |
| R-004 | Enable solver submission in this release | REJECTED | Data Check and submit remain hard-gated |
| R-005 | Assign an open-source license | PENDING | No license is granted until ownership is confirmed |
| R-006 | Preserve exact private runtime reports in Git | REJECTED | Publish only sanitized status summaries |

## Engineering decisions

| ID | Decision | Status | Temporary rule |
|---|---|---|---|
| D-001 | Confirm whether customer acceptance is DR≤0.40 and DW≤0.40 | PENDING | Calculate test metrics only; do not issue acceptance conclusion |
| D-002 | Confirm whether pile-toe U1/U2 constraints are intentional | PENDING | Preserve the baseline until reviewed |
| D-003 | Authorize strength-only scaling with E/C1 held constant as an M2 comparison | PENDING | Keep the baseline dependency unchanged |
| D-004 | Confirm single versus double precision policy | PENDING | Preserve baseline single precision |
| D-005 | Approve ODB retention period and disk budget | PENDING | Do not start four long runs |
| D-006 | Approve a revised VUAMP/VUSDFLD SHA for Data Check | PENDING | Current FOR is static-review evidence only |

## New decision template

    ID:
    Subject:
    Status: APPROVED / REJECTED / PENDING / NOT_REQUIRED
    Decision:
    Decision owner:
    Date and timezone:
    Evidence:
    Affected files, gates and reruns:
