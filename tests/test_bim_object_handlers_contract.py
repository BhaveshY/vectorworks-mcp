from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "native_bridge" / "src" / "BimObjectHandlers.hpp"
SOURCE = ROOT / "native_bridge" / "src" / "BimObjectHandlers.cpp"
SPACE_SOURCE = ROOT / "native_bridge" / "src" / "SpaceObjectHandlers.cpp"


class BimObjectHandlersContractTests(unittest.TestCase):
    def test_handlers_use_only_true_vw_2024_bim_constructors(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("sdk.CreateSlab(profile)", source)
        self.assertIn("sdk.CreateRoof(", source)
        self.assertIn("sdk.AppendRoofEdge(", source)
        self.assertNotIn("CreateCustomObject", source)
        self.assertNotIn("CreateExtrude", source)

    def test_receipts_require_actual_semantic_node_types(self):
        source = SOURCE.read_text(encoding="utf-8")
        header = HEADER.read_text(encoding="utf-8")

        self.assertIn("short actualNodeType", header)
        self.assertIn("sdk.GetObjectTypeN(object)", source)
        self.assertIn("kSlabNode", source)
        self.assertIn("kRoofContainerNode", source)
        self.assertIn("actualNodeType != expectedNodeType", source)

    def test_partial_objects_transfer_to_transaction_before_guard_release(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("class CreatedObjectGuard", source)
        self.assertIn("sdk_.DeleteObject(handle_, false)", source)
        self.assertIn("CreatedObjectGuard profileGuard", source)
        self.assertIn("CreatedObjectGuard slabGuard", source)
        self.assertIn("CreatedObjectGuard roofGuard", source)
        slab_handler = source[source.index("CreationReceipt CreateTrueSlab") :]
        self.assertLess(
            slab_handler.index("transaction.AdoptTemporary(profile"),
            slab_handler.index("profileGuard.Release()"),
        )
        self.assertLess(
            source.index("transaction.AdoptFinal(\n        slab"),
            source.index("slabGuard.Release()"),
        )
        self.assertLess(
            source.index("transaction.AdoptFinal(\n        roof"),
            source.index("roofGuard.Release()"),
        )

    def test_slab_components_use_zero_based_sdk_indices(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("sdk.SetComponentWidth(slab, 0, request.thickness)", source)
        self.assertIn("for (short component = 0; component < componentCount; ++component)", source)
        self.assertNotIn("component = 1; component <= componentCount", source)

    def test_profile_and_roof_inputs_are_strictly_validated(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("kMaxProfileVertices = 1024", source)
        self.assertIn("must be a simple non-self-intersecting polygon", source)
        self.assertIn("boundary area must be non-zero", source)
        self.assertIn("roof slopeDegrees must be greater than 0", source)
        self.assertIn("roof miterType must be one of 1, 2, 3, or 4", source)
        self.assertIn("std::reverse(normalized.begin(), normalized.end())", source)

    def test_space_profile_cleanup_and_area_units_are_explicit(self):
        source = SPACE_SOURCE.read_text(encoding="utf-8")
        header = (ROOT / "native_bridge" / "src" / "SpaceObjectHandlers.hpp").read_text(encoding="utf-8")

        self.assertIn("CreatedObjectGuard profileGuard", source)
        self.assertIn("transaction.AdoptTemporary(profile", source)
        self.assertLess(
            source.index("transaction.AdoptTemporary(profile"),
            source.index("profileGuard.Release()"),
        )
        self.assertIn("support.NetPoly", source)
        self.assertIn("support.GrossPoly", source)
        self.assertIn("expected.EqualishTo(actual, true)", source)
        self.assertIn("CreatedObjectGuard netGuard", source)
        self.assertIn("CreatedObjectGuard grossGuard", source)
        self.assertIn("Transactions::NativeTransaction& transaction", header)
        self.assertNotIn("registerWithUndo", header)
        self.assertIn("transaction.AdoptFinal(", source)
        self.assertLess(source.index("transaction.AdoptFinal("), source.index("spaceGuard.Release()"))
        self.assertLess(source.index("transaction.AdoptFinal("), source.index("gSDK->ResetObject(space)"))


if __name__ == "__main__":
    unittest.main()
