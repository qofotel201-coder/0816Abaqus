# Security and publication policy

## Threat model

The system handles very large proprietary model files, native Fortran code,
Abaqus licenses and processes capable of consuming substantial resources.
Risks include accidental source mutation, path traversal, overwrites, arbitrary
code execution, credential disclosure and uncontrolled solver processes.

## Controls

- local absolute paths are generated into ignored config/local files;
- UNC paths are rejected;
- configured roots must exist and source/workspace roots are disjoint;
- CAE access is staged and source integrity is rechecked;
- worker method names are fixed;
- request and response sizes are bounded;
- persistent writes refuse existing targets;
- workspace writes use expiring plans, plan hashes and single-use tokens;
- ODB reads require exact step, region and variable names with row limits;
- NaN, infinity, truncation and wildcard extraction are rejected;
- solver submission and process termination are disabled.

## Public repository exclusions

The public-tree check rejects:

- CAE, ODB, INP, archives, PDFs, images and solver logs;
- Abaqus journal, replay, lock and cache files;
- the private project FOR;
- runtime directories and files larger than 20 MiB;
- common API key, private key and plaintext commit-token patterns;
- originating-computer user-profile and project-root paths.

The scanner is a policy guard, not a proof that arbitrary binary data contains
no secret. Only the explicit source allowlist should be staged.

## Incident response

If a private token or customer file reaches Git history:

1. stop further pushes;
2. revoke or rotate the credential where applicable;
3. notify the data owner;
4. remove the object from all branches and tags using an approved history
   rewrite process;
5. invalidate affected releases and clones;
6. document the incident without repeating the secret.

Deleting a file in a later commit is not sufficient.

## Reporting

Do not open a public issue containing private model data or a credential. Contact
the repository owner through a private channel and provide only the minimum
metadata needed to identify the affected commit.
