import copy
import json
import unittest
from unittest.mock import patch

import plan_quality
import server


def valid_plan() -> dict:
    return {
        "schema": "vectorworks.plan-quality/v1",
        "units": "mm",
        "plan_id": "quality-test-apartment",
        "assumptions": {
            "geometry_tolerance_mm": 2,
            "wall_room_tolerance_mm": 150,
            "minimum_wall_end_clearance_mm": 100,
            "annotation_clearance_mm": 50,
            "blocking_severities": ["error"],
        },
        "program": {
            "entry": {
                "door_id": "entry",
                "arrival_room_id": "hall",
                "must_reach_room_ids": ["living", "bed"],
            },
            "required_adjacencies": [
                {"id": "adj-hall-living", "room_ids": ["hall", "living"], "minimum_shared_boundary_mm": 900},
                {"id": "adj-living-bed", "room_ids": ["living", "bed"], "minimum_shared_boundary_mm": 900},
            ],
        },
        "rooms": [
            {
                "id": "hall",
                "number": "01",
                "name": "Entrance / Hall",
                "bounds": [0, 0, 2000, 3000],
                "target_area_m2": 6,
                "area_tolerance_m2": 0.01,
                "minimum_dimension_mm": 1000,
                "minimum_exterior_windows": 0,
            },
            {
                "id": "living",
                "number": "02",
                "name": "Living / Dining",
                "bounds": [2200, 0, 6000, 3000],
                "target_area_m2": 11.4,
                "area_tolerance_m2": 0.01,
                "minimum_dimension_mm": 2000,
                "minimum_exterior_windows": 1,
            },
            {
                "id": "bed",
                "number": "03",
                "name": "Bedroom",
                "bounds": [2200, 3200, 6000, 6000],
                "target_area_m2": 10.64,
                "area_tolerance_m2": 0.01,
                "minimum_dimension_mm": 2500,
                "minimum_exterior_windows": 1,
            },
        ],
        "walls": [
            {"id": "hall-south", "start": [0, 0], "end": [2000, 0], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "hall-living", "start": [2100, 0], "end": [2100, 3000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "living-bed", "start": [2200, 3100], "end": [6000, 3100], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "living-east", "start": [6000, 0], "end": [6000, 3000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "bed-north", "start": [2200, 6000], "end": [6000, 6000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
        ],
        "openings": [
            {"kind": "door", "id": "entry", "wall_id": "hall-south", "offset_mm": 1000, "width_mm": 900, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "hall"},
            {"kind": "door", "id": "hall-door", "wall_id": "hall-living", "offset_mm": 1500, "width_mm": 900, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "living"},
            {"kind": "door", "id": "bed-door", "wall_id": "living-bed", "offset_mm": 800, "width_mm": 900, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "living"},
            {"kind": "window", "id": "living-window", "wall_id": "living-east", "offset_mm": 1500, "width_mm": 1200, "height_mm": 1500, "sill_height_mm": 900},
            {"kind": "window", "id": "bed-window", "wall_id": "bed-north", "offset_mm": 1900, "width_mm": 1600, "height_mm": 1500, "sill_height_mm": 900},
        ],
        "furniture": [
            {
                "id": "sofa",
                "room_id": "living",
                "bounds": [3500, 400, 5500, 1200],
                "class_name": "A-Furn",
                "access_zones": [
                    {"id": "sofa-access", "bounds": [3500, 1300, 5500, 2200], "minimum_clear_width_mm": 900}
                ],
            },
            {
                "id": "bed-furniture",
                "room_id": "bed",
                "bounds": [3500, 3900, 5500, 5600],
                "class_name": "A-Furn",
                "access_zones": [
                    {"id": "bed-access", "bounds": [2800, 3900, 3400, 5600], "minimum_clear_width_mm": 600}
                ],
            },
        ],
        "circulation": [
            {"id": "hall-clear", "room_id": "hall", "bounds": [500, 1100, 1500, 2500], "minimum_clear_width_mm": 900},
            {"id": "living-clear", "room_id": "living", "bounds": [2300, 1500, 3000, 2500], "minimum_clear_width_mm": 700},
            {"id": "bed-clear", "room_id": "bed", "bounds": [2300, 3500, 2700, 5500], "minimum_clear_width_mm": 400},
        ],
        "annotations": [
            {"kind": "room_label", "id": "hall-label", "room_id": "hall", "bounds": [200, 2400, 1300, 2900]},
            {"kind": "room_label", "id": "living-label", "room_id": "living", "bounds": [4300, 2300, 5800, 2800]},
            {"kind": "room_label", "id": "bed-label", "room_id": "bed", "bounds": [4300, 3300, 5800, 3700]},
            {"kind": "room_schedule", "id": "schedule", "bounds": [6500, 0, 9500, 3000], "room_ids": ["hall", "living", "bed"], "header_height_mm": 300, "minimum_row_height_mm": 500},
        ],
    }


class PlanQualityTests(unittest.TestCase):
    def test_valid_plan_passes_every_advertised_check_deterministically(self):
        raw = valid_plan()
        first = plan_quality.evaluate_plan_payload(raw)
        reordered = copy.deepcopy(raw)
        for key in ("rooms", "walls", "openings", "furniture", "circulation"):
            reordered[key].reverse()
        reordered["program"]["required_adjacencies"].reverse()
        second = plan_quality.evaluate_plan_payload(reordered)

        self.assertTrue(first["passed"], json.dumps(first["issues"], indent=2))
        self.assertEqual(first["checks"], list(plan_quality.CHECKS))
        self.assertEqual(first, second)
        self.assertEqual(first["counts"], {"error": 0, "warning": 0, "info": 0})
        self.assertTrue(first["manifest_digest"].startswith("sha256:"))
        self.assertIn("not regulatory compliance", first["disclaimer"])

    def test_confirmed_apartment_defects_are_named_and_measured(self):
        raw = valid_plan()
        raw["program"]["entry"]["arrival_room_id"] = "living"
        raw["furniture"].append(
            {
                "id": "wardrobe",
                "room_id": "bed",
                "bounds": [5000, 5000, 5900, 5800],
                "class_name": "A-Furn-Storage",
                "access_zones": [],
            }
        )
        raw["annotations"][1]["bounds"] = [3600, 500, 5200, 1000]
        raw["circulation"][1]["minimum_clear_width_mm"] = 900

        report = plan_quality.evaluate_plan_payload(raw)
        codes = {issue["code"] for issue in report["issues"]}

        self.assertFalse(report["passed"])
        self.assertIn("entry.arrival_room_mismatch", codes)
        self.assertIn("furniture.collision", codes)
        self.assertIn("annotation.furniture_collision", codes)
        self.assertIn("circulation.too_narrow", codes)
        circulation = next(issue for issue in report["issues"] if issue["code"] == "circulation.too_narrow")
        self.assertEqual(circulation["measurements"][0]["actual"], "700")
        self.assertEqual(circulation["measurements"][0]["required"], "900")

    def test_unknown_fields_and_references_are_boundary_errors(self):
        raw = valid_plan()
        raw["rooms"][0]["typo_min_width"] = 900
        raw["openings"][0]["wall_id"] = "missing"

        with self.assertRaises(plan_quality.PlanValidationError) as raised:
            plan_quality.parse_plan(raw)

        codes = {problem.code for problem in raised.exception.problems}
        self.assertIn("field.unknown", codes)
        self.assertIn("reference.wall_missing", codes)

    def test_plan_quality_grouped_action_never_touches_native_transport(self):
        with (
            patch.object(server, "_grouped_native_call", side_effect=AssertionError("native dispatch")),
            patch.object(server, "_fast_execution_bridge_status", side_effect=AssertionError("ping")),
            patch.object(server, "_send", side_effect=AssertionError("transport")),
        ):
            result = json.loads(server.vw_read("plan_quality", plan=valid_plan()))

        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["passed"])
        self.assertEqual(result["execution"], {"mode": "host_only", "native_calls": 0})
        self.assertNotIn("bridge", result)

    def test_malformed_plan_is_tool_error_but_failed_design_is_data(self):
        malformed = json.loads(server.vw_read("plan_quality", plan={"schema": "wrong"}))
        self.assertFalse(malformed["ok"])
        self.assertEqual(malformed["error"]["code"], "validation_error")
        self.assertFalse(malformed["error"]["writes_started"])

        poor = valid_plan()
        poor["rooms"][0]["minimum_dimension_mm"] = 3500
        analyzed = json.loads(server.vw_read("plan_quality", plan=poor))
        self.assertTrue(analyzed["ok"])
        self.assertFalse(analyzed["data"]["passed"])

    def test_plan_payload_is_rejected_for_native_read_actions(self):
        result = json.loads(server.vw_read("document", plan=valid_plan()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "validation_error")

    def test_unexpected_analysis_failure_is_known_not_started_state(self):
        with patch.object(server, "evaluate_plan_payload", side_effect=RuntimeError("boom")):
            result = json.loads(server.vw_read("plan_quality", plan=valid_plan()))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "analysis_error")
        self.assertFalse(result["error"]["writes_started"])
        self.assertEqual(result["error"]["commit_state"], "not_started")
        self.assertNotIn("boom", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
