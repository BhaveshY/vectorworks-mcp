import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
NATIVE = ROOT / "native_bridge" / "src"


class NativeAuthoritativeCapabilitiesContractTests(unittest.TestCase):
    def source(self, name: str) -> str:
        return (NATIVE / name).read_text(encoding="utf-8")

    def test_revision_four_has_deterministic_fingerprint_and_truthful_aliases(self):
        header = self.source("CapabilityRegistry.hpp")
        source = self.source("CapabilityRegistry.cpp")
        domain = self.source("NativeDomain.hpp")

        self.assertIn("kCapabilityRevision = 4u", header)
        self.assertIn("CapabilityFingerprint(bool cadHandlersImplemented)", header)
        self.assertIn("capability_fingerprint", source)
        self.assertIn("14695981039346656037ull", source)
        self.assertIn("canonicalObjectKind", domain)
        self.assertRegex(source, r'\{"box",\s*"rect",\s*"kRectangleNode"')
        self.assertRegex(source, r'\{"rectangle",\s*"rect",\s*"kRectangleNode"')
        self.assertRegex(source, r'\{"dimension",\s*"linear_dimension",\s*"kDimensionNode"')
        self.assertRegex(source, r'\{"polyline",\s*"polygon",\s*"kPolygonNode"')
        self.assertRegex(source, r'\{"door",\s*"door",\s*"kParametricNode",\s*"requires_runtime_schema"')
        self.assertRegex(source, r'\{"window",\s*"window",\s*"kParametricNode",\s*"requires_runtime_schema"')
        self.assertIn('"required":["wall_uuid","x1","y1","width","height","descriptor_fingerprint"]', source)
        self.assertIn('"required":["wall_uuid","x1","y1","width","height","sill_height","descriptor_fingerprint"]', source)
        self.assertIn("exact universal Door plugin", source)
        self.assertIn("exact universal Window plugin", source)
        self.assertIn('"requires_runtime_schema"', source)
        self.assertIn('"requires_resource"', source)
        self.assertIn("input_schema", source)
        self.assertIn("verifier", source)

    def test_parametric_schema_uses_runtime_providers_without_probe_objects(self):
        source = self.source("ParametricObjectAdapter.cpp")
        definition_start = source.index("ParametricDescriptor DescribeParametricDefinition")
        built_in_start = source.index("const char* BuiltInParametricUniversalName")
        definition = source[definition_start:built_in_start]

        self.assertIn("GetPluginType", definition)
        self.assertIn("DescribeWithParams2Provider", definition)
        self.assertIn("DescribeWithParamsProvider", definition)
        self.assertNotIn("GetNamedObject", definition)
        self.assertNotIn("CreateCustomObject", definition)
        self.assertIn('case BuiltInParametricKind::Door: return "Door"', source)
        self.assertIn('case BuiltInParametricKind::Window: return "Window"', source)
        self.assertIn("descriptor fingerprint is required", source)

    def test_built_in_openings_use_universal_semantics_and_exact_wall_verification(self):
        header = self.source("ParametricObjectAdapter.hpp")
        source = self.source("ParametricObjectAdapter.cpp")
        helper_start = source.index("BuiltInOpeningReceipt CreateVerifiedBuiltInOpening")
        host_helper_start = source.index("bool IsObjectHostedByWall", helper_start)
        helper = source[helper_start:host_helper_start]

        self.assertIn("BuiltInOpeningSemanticIds", header)
        self.assertIn("BuiltInOpeningReceipt", header)
        self.assertIn("descriptorFingerprint", header)
        self.assertIn("wallHostVerified", header)
        self.assertIn("semanticReadbackVerified", header)
        self.assertIn('{"Width"}', source)
        self.assertIn('{"Height"}', source)
        self.assertIn('{"Elevation"}', source)
        self.assertIn("CreateCustomObject", helper)
        self.assertRegex(helper, r"spec\.rotationDegrees,\s*true\)")
        self.assertIn("RequireInstanceControlled", helper)
        self.assertIn("SetParamReal", helper)
        self.assertIn("ResetObject", helper)
        self.assertGreaterEqual(helper.count("IsObjectHostedByWall"), 2)
        self.assertIn("GetParamReal", helper)
        self.assertNotRegex(helper, r"localized|Localized|GetNamedObject|RunMenu|Alert|Python")

        verifier_start = source.index("void VerifyBuiltInOpeningReceipt")
        verifier_end = source.index("bool IsObjectHostedByWall", verifier_start)
        verifier = source[verifier_start:verifier_end]
        self.assertIn("DescribeParametricObject", verifier)
        self.assertIn("ResolveBuiltInOpeningSemanticIds", verifier)
        self.assertIn("IsObjectHostedByWall", verifier)
        self.assertIn("GetParamReal", verifier)
        self.assertNotIn("SetParam", verifier)
        self.assertNotIn("ResetObject", verifier)

    def test_view_and_document_readback_model_semantic_truth(self):
        header = self.source("ViewDocumentHandlers.hpp")
        source = self.source("ViewDocumentHandlers.cpp")

        self.assertIn("enum class CommitState", header)
        self.assertIn("RequestedPath()", header)
        self.assertIn("ActivePath()", header)
        self.assertIn("CommitState commitState", header)
        self.assertIn("CommitState::Unknown", source)
        self.assertIn("SameExistingPath", source)
        self.assertIn("sdkReportedSuccess", source)
        self.assertNotRegex(
            source,
            r"setRenderMode\s*&&\s*!gSDK->SetRenderMode",
        )
        self.assertRegex(
            source,
            r"gSDK->SetRenderMode\([^;]+;\s*}\s*const ViewState actual = ReadView\(\)",
        )

    def test_dwg_import_returns_before_after_mutation_receipt(self):
        header = self.source("NativeIOHandlers.hpp")
        source = self.source("NativeIOHandlers.cpp")

        for field in (
            "createdObjectUuids",
            "deletedObjectUuids",
            "createdLayers",
            "deletedLayers",
            "changedLayers",
            "activeLayerChanged",
            "verified",
        ):
            self.assertIn(field, header)
        self.assertIn("CaptureDocumentMutationSnapshot", header)
        self.assertIn("BuildDocumentMutationReceipt", header)
        import_start = source.index("Result ImportDWG")
        export_start = source.index("Result ExportDWG", import_start)
        import_body = source[import_start:export_start]
        self.assertLess(
            import_body.index("CaptureDocumentMutationSnapshot"),
            import_body.index("importer->Import"),
        )
        self.assertIn("BuildDocumentMutationReceipt", import_body)
        self.assertIn("hasDocumentMutationReceipt = true", import_body)
        self.assertNotRegex(import_body, r"RunMenu|Alert|CreateCustomObject|Python")


if __name__ == "__main__":
    unittest.main()
