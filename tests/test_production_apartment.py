import unittest

from plan_quality import evaluate_plan_payload
from production_apartment import (
    build_manifest,
    compile_foundation,
    compile_openings,
    expected_counts,
)


class ProductionApartmentTests(unittest.TestCase):
    def test_manifest_passes_and_every_native_artifact_is_derived_from_it(self):
        raw = build_manifest()
        report = evaluate_plan_payload(raw)
        compiled = compile_foundation(raw, "APT_TEST")

        self.assertTrue(report["passed"], report["issues"])
        self.assertEqual(report["counts"], {"error": 0, "warning": 0, "info": 0})
        self.assertEqual(expected_counts(compiled), {"spaces": 7, "walls": 21, "doors": 7, "windows": 4})
        self.assertLessEqual(len(compiled.foundation), 250)

        operation_ids = [item.get("operation_id") for item in compiled.foundation if item.get("operation_id")]
        self.assertEqual(len(operation_ids), len(set(operation_ids)))
        wall_operations = [
            item
            for item in compiled.foundation
            if item["type"] == "create" and item["params"].get("object_type") == "wall"
        ]
        space_operations = [
            item
            for item in compiled.foundation
            if item["type"] == "create" and item["params"].get("object_type") == "space"
        ]
        self.assertEqual(len(wall_operations), len(compiled.manifest.walls))
        self.assertEqual(len(space_operations), len(compiled.manifest.rooms))
        self.assertEqual(
            {item["params"]["name"] for item in space_operations},
            {room.name for room in compiled.manifest.rooms},
        )

    def test_hosted_openings_bind_in_one_batch_after_wall_uuid_resolution(self):
        compiled = compile_foundation(build_manifest(), "APT_TEST")
        wall_uuids = {wall_id: f"uuid-{wall_id}" for wall_id in compiled.wall_names}
        openings = compile_openings(
            compiled,
            prefix="APT_TEST",
            wall_uuids=wall_uuids,
            door_fingerprint="door-v1",
            window_fingerprint="window-v1",
        )

        self.assertEqual(len(openings), 11)
        self.assertEqual(len({item["operation_id"] for item in openings}), 11)
        self.assertTrue(all(item["params"]["wall_uuid"].startswith("uuid-") for item in openings))
        entry = next(item for item in openings if item["operation_id"] == "door-entry-door")
        self.assertEqual((entry["params"]["x"], entry["params"]["y"]), (6750.0, 0.0))
        self.assertEqual(entry["params"]["rotation"], 180)


if __name__ == "__main__":
    unittest.main()
