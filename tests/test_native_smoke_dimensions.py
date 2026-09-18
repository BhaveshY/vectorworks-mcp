import json
import unittest
from unittest.mock import patch

from native_bridge import smoke


class DimensionSmokeTests(unittest.TestCase):
    def run_fixture(self, object_record):
        calls = []
        deleted = False

        def call(sock, report, action, label, params=None):
            nonlocal deleted
            calls.append((action, label, params))
            if action == "create_linear_dimension":
                self.assertNotIn("name", params)
                return {"result": {"type": "linear_dimension", "handle": "0x123"}}
            if action == "get_objects":
                return {"result": [] if deleted else [object_record]}
            if action == "apply_operations":
                deleted = True
                return {"result": {"committed": True, "verified": True}}
            return None

        report = {"failures": []}
        with patch.object(smoke, "_record_call", side_effect=call):
            smoke._run_phase_two_write_fixture(None, report)
        return calls, report

    def test_unnamed_dimension_cleanup_uses_verified_uuid_and_confirmation(self):
        calls, report = self.run_fixture({"type": "dimension", "handle": "0x123", "uuid": "dimension-uuid"})
        deletes = [params for action, _, params in calls if action == "apply_operations"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(json.loads(deletes[0]["operation_1_json"]), {
            "op": "object.delete", "target": "uuid:dimension-uuid", "confirm": "DELETE_OBJECT",
        })
        self.assertFalse(any("dimension" in failure for failure in report["failures"]))

    def test_missing_or_unrelated_uuid_never_deletes(self):
        for record in ({"type": "dimension", "handle": "0x123"},
                       {"type": "dimension", "handle": "0x456", "uuid": "unrelated"}):
            with self.subTest(record=record):
                calls, report = self.run_fixture(record)
                self.assertFalse(any(action == "apply_operations" for action, _, _ in calls))
                self.assertIn("dimension UUID was not proven; refusing cleanup", report["failures"])


if __name__ == "__main__":
    unittest.main()
