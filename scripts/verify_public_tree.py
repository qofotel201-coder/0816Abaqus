#!/usr/bin/env python3
"""Fail if the public source tree contains private, generated, or huge files."""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_BYTES = 20 * 1024 * 1024
FORBIDDEN_SUFFIXES = {
    ".cae",
    ".odb",
    ".inp",
    ".rar",
    ".7z",
    ".zip",
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".tif",
    ".tiff",
    ".html",
    ".dat",
    ".msg",
    ".sta",
    ".lck",
    ".rpy",
    ".jnl",
    ".pyc",
    ".for",
}
FORBIDDEN_PARTS = {
    ".git",
    "__pycache__",
    "_abaqus_bridge_runs",
    "workspaces",
    "cases",
    "results",
    "private",
}
TEXT_SUFFIXES = {
    ".py",
    ".json",
    ".md",
    ".txt",
    ".toml",
    ".yml",
    ".yaml",
    ".gitignore",
    ".gitattributes",
}
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}\b"),
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    "commit_token_value": re.compile(
        r'(?i)"commit_token"\s*:\s*"[A-Za-z0-9_-]{24,}"'
    ),
    "original_project_root": re.compile(r"(?i)D:\\" + "1950"),
    "windows_user_profile": re.compile(r"(?i)[A-Z]:\\Users\\[A-Za-z0-9._-]+"),
    "wsl_user_profile": re.compile(r"/mnt/[a-z]/Users/[A-Za-z0-9._-]+"),
}


def main() -> int:
    findings: list[dict[str, str]] = []
    checked = 0
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        ignored_local = (
            relative.parts[:2] == ("config", "local")
            or any(part.startswith(".venv") for part in relative.parts)
        )
        if (
            not path.is_file()
            or ignored_local
            or any(part in FORBIDDEN_PARTS for part in relative.parts)
        ):
            continue
        checked += 1
        if path.name in {"NUL", "credentials.json", "token.json"}:
            findings.append({"path": str(relative), "issue": "forbidden filename"})
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            findings.append({"path": str(relative), "issue": "forbidden suffix"})
        if path.stat().st_size > MAX_FILE_BYTES:
            findings.append({"path": str(relative), "issue": "file exceeds 20 MiB"})
        suffix_key = path.suffix.lower() if path.suffix else path.name
        if suffix_key not in TEXT_SUFFIXES:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append({"path": str(relative), "issue": name})

    result = {
        "status": "PASS" if not findings else "FAIL",
        "checked_files": checked,
        "max_file_bytes": MAX_FILE_BYTES,
        "findings": findings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if not findings else 20


if __name__ == "__main__":
    sys.exit(main())
