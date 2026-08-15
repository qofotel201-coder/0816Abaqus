"""Validate portable schemas, examples, and public-tree invariants."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
CONFIG = ROOT / "config"


class PortableContractTests(unittest.TestCase):
    def test_all_schemas_are_valid_draft_2020_12(self) -> None:
        for path in sorted(SCHEMAS.glob("*.schema.json")):
            with self.subTest(schema=path.name):
                value = json.loads(path.read_text(encoding="utf-8"))
                Draft202012Validator.check_schema(value)

    def test_versioned_examples_validate(self) -> None:
        pairs = {
            "automation.example.json": "automation-config.schema.json",
            "abaqus_bridge.example.json": "bridge-config.schema.json",
            "EXTERNAL_DATA_MANIFEST.json": "external-data-manifest.schema.json",
            "project-profile.json": "project-config.schema.json",
            "case-matrix.json": "case-matrix.schema.json",
            "output-contract.json": "output-contract.schema.json",
            "preparation-readiness.example.json": "preparation-readiness.schema.json",
        }
        for instance_name, schema_name in pairs.items():
            with self.subTest(instance=instance_name):
                instance = json.loads(
                    (CONFIG / instance_name).read_text(encoding="utf-8")
                )
                schema = json.loads(
                    (SCHEMAS / schema_name).read_text(encoding="utf-8")
                )
                errors = sorted(
                    Draft202012Validator(schema).iter_errors(instance),
                    key=lambda error: list(error.absolute_path),
                )
                self.assertEqual([], errors)

    def test_no_original_machine_paths_are_versioned(self) -> None:
        denied = (
            re.compile(r"(?i)D:\\\\1950"),
            re.compile(r"/mnt/[a-z]/Users/"),
            re.compile(r"(?i)[A-Z]:\\\\Users\\\\[A-Za-z0-9._-]+"),
        )
        suffixes = {".py", ".json", ".md", ".toml", ".yml", ".yaml", ".txt"}
        findings = []
        for path in ROOT.rglob("*"):
            relative = path.relative_to(ROOT)
            ignored_local = (
                relative.parts[:2] == ("config", "local")
                or any(part.startswith(".venv") for part in relative.parts)
            )
            if (
                not path.is_file()
                or ".git" in path.parts
                or ignored_local
                or path.suffix not in suffixes
            ):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in denied:
                if pattern.search(text):
                    findings.append("%s: %s" % (relative, pattern.pattern))
        self.assertEqual([], findings)

    def test_private_inputs_are_external_only(self) -> None:
        manifest = json.loads(
            (CONFIG / "EXTERNAL_DATA_MANIFEST.json").read_text(encoding="utf-8")
        )
        inputs = {row["logical_id"]: row for row in manifest["inputs"]}
        self.assertFalse(inputs["frozen_source_cae"]["included_in_repository"])
        self.assertFalse(inputs["phase0_user_subroutine"]["included_in_repository"])
        self.assertEqual([], manifest["repository_sources"])
        self.assertFalse(
            (ROOT / "subroutines" / "Combined_VUAMP_VUSDFLD.for").exists()
        )


if __name__ == "__main__":
    unittest.main()
