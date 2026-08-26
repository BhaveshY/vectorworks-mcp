from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
HEADER = ROOT / "native_bridge" / "src" / "ResourceWorksheetHandlers.hpp"
SOURCE = ROOT / "native_bridge" / "src" / "ResourceWorksheetHandlers.cpp"


class ResourceWorksheetHandlersContractTests(unittest.TestCase):
    def test_symbol_listing_and_insertion_use_direct_sdk_calls(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("sdk.ForEachObjectN(allSymbolDefs, collect)", source)
        self.assertIn("sdk.PlaceSymbolN(", source)
        self.assertIn("sdk.AddObjectToContainer(symbol, layer)", source)
        self.assertIn("sdk.ParentObject(symbol) != layer", source)
        self.assertIn("sdk.GetDefinition(symbol)", source)
        self.assertIn("actualNodeType != kSymbolNode", source)
        self.assertIn("kSymDefNode", source)
        self.assertNotIn("PlaceSymbolByName", source)

    def test_exact_resource_resolution_rejects_missing_and_ambiguous_names(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("resource not found by exact name", source)
        self.assertIn("resource name is ambiguous", source)
        self.assertIn("resource.name == name", source)

    def test_worksheet_reads_and_writes_are_semantically_verified(self):
        source = SOURCE.read_text(encoding="utf-8")
        header = HEADER.read_text(encoding="utf-8")

        self.assertIn('TXString("T=WORKSHEET")', source)
        self.assertIn("actualNodeType != expectedNodeType", source)
        self.assertIn("sdk.GetWorksheetCellFormula", source)
        self.assertIn("sdk.GetWorksheetCellString", source)
        self.assertIn("sdk.SetWorksheetCellFormula", source)
        self.assertIn("sdk.RecalculateWorksheet", source)
        self.assertIn("after.formula != request.formula", source)
        self.assertIn("bool verified = false", header)

    def test_worksheet_write_has_rollback_and_no_ui_fallback(self):
        source = SOURCE.read_text(encoding="utf-8")

        self.assertIn("class WorksheetCellRollback", source)
        self.assertIn("the previous cell formula was restored", source)
        self.assertNotIn("DoMenuTextByName", source)
        self.assertNotIn("ShowWorksheet", source)
        self.assertNotIn("Dialog", source)


if __name__ == "__main__":
    unittest.main()
