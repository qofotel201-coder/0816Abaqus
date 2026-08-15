"""Contract tests for the Abaqus-Python-2.7 ODB worker without a real ODB."""

import math
import os
import re
import sys
import tempfile
import unittest
from unittest import mock


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TEST_DIR)
AUTOMATION_DIR = os.path.join(PROJECT_DIR, "automation")
if AUTOMATION_DIR not in sys.path:
    sys.path.insert(0, AUTOMATION_DIR)

import odb_worker


class FakeHistoryOutput(object):
    def __init__(self, data):
        self.data = data


class FakeHistoryRegion(object):
    def __init__(self, outputs):
        self.historyOutputs = outputs


class FakeStep(object):
    def __init__(self, regions):
        self.historyRegions = regions
        self.frames = [object(), object()]
        self.timePeriod = 50.0


class FakeOdb(object):
    def __init__(self, steps):
        self.steps = steps
        self.closed = False

    def close(self):
        self.closed = True


class OdbWorkerContractTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".odb", delete=False)
        handle.write(b"fake-odb-for-contract-tests")
        handle.close()
        self.odb_path = os.path.abspath(handle.name)
        self.original_opener = odb_worker._OPEN_ODB
        self.open_calls = []
        self.opened_odbs = []
        self.history_rows = [(0.0, 10.0), (0.025, 20.0)]

        def fake_open(path, readOnly):
            self.open_calls.append({"path": path, "readOnly": readOnly})
            fake = FakeOdb(
                {
                    "Step-hammer-load": FakeStep(
                        {
                            "Surface PILE-1.SURF-PILE-OUT": FakeHistoryRegion(
                                {
                                    "CFT3": FakeHistoryOutput(
                                        list(self.history_rows)
                                    ),
                                    "CFN3": FakeHistoryOutput(
                                        [(0.0, 8.0), (0.025, 16.0)]
                                    ),
                                }
                            ),
                            "Node PILE-1.1": FakeHistoryRegion(
                                {
                                    "U3": FakeHistoryOutput(
                                        [(0.0, 0.0), (0.025, -0.05)]
                                    )
                                }
                            ),
                        }
                    )
                }
            )
            self.opened_odbs.append(fake)
            return fake

        self.fake_open = fake_open
        odb_worker._OPEN_ODB = fake_open

    def test_native_cpython3_windows_path_remains_text(self):
        with mock.patch.object(odb_worker.os, "name", "nt"):
            value = r"C:\\AbaqusRuns\\result.odb"
            self.assertEqual(value, odb_worker._native_path(value))

    def tearDown(self):
        odb_worker._OPEN_ODB = self.original_opener
        try:
            os.unlink(self.odb_path)
        except OSError:
            pass

    def extraction_params(self, **updates):
        params = {
            "odb_path": self.odb_path,
            "step": "Step-hammer-load",
            "region": "Surface PILE-1.SURF-PILE-OUT",
            "variable": "CFT3",
            "max_rows": 100,
        }
        params.update(updates)
        return params

    def assert_contract_error(self, code, function, *args, **kwargs):
        with self.assertRaises(odb_worker.OdbContractError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_inventory_opens_read_only_and_is_bounded(self):
        result = odb_worker.inventory(
            {
                "odb_path": self.odb_path,
                "max_steps": 1,
                "max_regions_per_step": 1,
                "max_variables_per_region": 1,
            }
        )
        self.assertEqual(self.open_calls, [{"path": self.odb_path, "readOnly": True}])
        self.assertTrue(self.opened_odbs[-1].closed)
        self.assertTrue(result["source"]["read_only"])
        self.assertEqual(result["steps"]["count"], 1)
        step = result["steps"]["items"][0]
        self.assertEqual(step["name"], "Step-hammer-load")
        self.assertEqual(step["history_regions"]["count"], 2)
        self.assertTrue(step["history_regions"]["truncated"])
        self.assertEqual(len(step["history_regions"]["items"]), 1)

    def test_extract_history_uses_exact_step_region_and_variable(self):
        result = odb_worker.extract_history(self.extraction_params())
        self.assertEqual(self.open_calls[-1]["readOnly"], True)
        self.assertTrue(self.opened_odbs[-1].closed)
        self.assertEqual(
            result["selection"],
            {
                "step": "Step-hammer-load",
                "region": "Surface PILE-1.SURF-PILE-OUT",
                "variable": "CFT3",
            },
        )
        self.assertEqual(result["columns"], ["frame_value", "value"])
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(result["rows"], [[0.0, 10.0], [0.025, 20.0]])
        self.assertFalse(result["truncated"])

    def test_wrong_case_and_wildcard_region_are_rejected_exactly(self):
        self.assert_contract_error(
            "REGION_NOT_FOUND",
            odb_worker.extract_history,
            self.extraction_params(region="surface pile-1.surf-pile-out"),
        )
        self.assertTrue(self.opened_odbs[-1].closed)
        self.assert_contract_error(
            "REGION_NOT_FOUND",
            odb_worker.extract_history,
            self.extraction_params(region="*"),
        )
        self.assertTrue(self.opened_odbs[-1].closed)

    def test_wrong_variable_is_rejected_exactly(self):
        error = self.assert_contract_error(
            "VARIABLE_NOT_FOUND",
            odb_worker.extract_history,
            self.extraction_params(variable="cft3"),
        )
        self.assertEqual(error.data["requested"], "cft3")
        self.assertIn("CFT3", error.data["available"])
        self.assertTrue(self.opened_odbs[-1].closed)

    def test_row_limit_fails_without_returning_a_partial_series(self):
        error = self.assert_contract_error(
            "ROW_LIMIT_EXCEEDED",
            odb_worker.extract_history,
            self.extraction_params(max_rows=1),
        )
        self.assertEqual(error.data, {"available_rows": 2, "max_rows": 1})
        self.assertTrue(self.opened_odbs[-1].closed)

    def test_non_finite_history_data_is_rejected(self):
        self.history_rows = [(0.0, 10.0), (0.025, math.nan)]
        error = self.assert_contract_error(
            "NON_FINITE_HISTORY_DATA",
            odb_worker.extract_history,
            self.extraction_params(),
        )
        self.assertEqual(error.data["row_index"], 1)
        self.assertTrue(self.opened_odbs[-1].closed)

    def test_missing_odb_and_invalid_limits_fail_before_open(self):
        missing = os.path.join(tempfile.gettempdir(), "missing-contract-file.odb")
        self.assert_contract_error(
            "ODB_NOT_FOUND", odb_worker.inventory, {"odb_path": missing}
        )
        self.assert_contract_error(
            "INVALID_PARAMETER",
            odb_worker.extract_history,
            self.extraction_params(max_rows=True),
        )
        self.assertEqual(self.open_calls, [])

    def test_odb_access_unavailable_fails_closed(self):
        odb_worker._OPEN_ODB = None
        self.assert_contract_error(
            "ODB_ACCESS_UNAVAILABLE",
            odb_worker.inventory,
            {"odb_path": self.odb_path},
        )

    def test_dispatch_has_only_two_fixed_methods(self):
        self.assertEqual(set(odb_worker.METHODS), {"inventory", "extract_history"})
        self.assert_contract_error(
            "METHOD_NOT_FOUND",
            odb_worker.dispatch_request,
            {
                "jsonrpc": "2.0",
                "id": "contract-test",
                "method": "open_python_console",
                "params": {},
            },
        )

    def test_json_dispatch_extracts_through_the_fixed_method_table(self):
        response = odb_worker.dispatch_request(
            {
                "jsonrpc": "2.0",
                "id": "extract-contract-test",
                "method": "extract_history",
                "params": self.extraction_params(),
            }
        )
        self.assertEqual(response["jsonrpc"], "2.0")
        self.assertEqual(response["id"], "extract-contract-test")
        self.assertEqual(response["result"]["row_count"], 2)
        self.assertTrue(self.opened_odbs[-1].closed)

    def test_worker_source_is_syntax_valid_and_has_no_eval_or_exec(self):
        with open(odb_worker.__file__, "r", encoding="utf-8") as stream:
            source = stream.read()
        compile(source, odb_worker.__file__, "exec")
        self.assertIsNone(re.search(r"\beval\s*\(", source))
        self.assertIsNone(re.search(r"\bexec\s*\(", source))
        self.assertIn("_OPEN_ODB(path=native,readOnly=True)", source.replace(" ", ""))


if __name__ == "__main__":
    unittest.main()
