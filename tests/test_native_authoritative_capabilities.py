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
        self.assertIn("DefineCustomObject", helper)
        self.assertIn("kCustomObjectPrefNever", helper)
        self.assertIn("CreateCustomObject", helper)
        self.assertIn("spec.rotationDegrees", helper)
        self.assertRegex(helper, r"spec\.rotationDegrees,\s*false\)")
        self.assertIn("AddObjectToContainer", helper)
        self.assertNotIn("gSDK->SetObjectWallInsertMode", helper)
        self.assertIn("RequireInstanceControlled", helper)
        self.assertIn("SetParamReal", helper)
        self.assertIn("ResetObject", helper)
        self.assertGreaterEqual(helper.count("IsObjectHostedByWall"), 2)
        self.assertIn("GetParamReal", helper)
        self.assertNotRegex(helper, r"localized|Localized|GetNamedObject|RunMenu|Alert|Python")

        generic_start = source.index("MCObjectHandle CreateVerifiedParametricObject")
        generic_end = source.index("void UpdateVerifiedParametricObject", generic_start)
        generic = source[generic_start:generic_end]
        self.assertIn("DefineCustomObject", generic)
        self.assertIn("kCustomObjectPrefNever", generic)
        self.assertIn("const bool insertOnActiveLayer = !spec.requireWallHost", generic)
        self.assertRegex(
            generic,
            r"spec\.rotationDegrees,\s*insertOnActiveLayer\)",
        )
        self.assertIn("AddObjectToContainer(object, spec.expectedWall)", generic)

        bridge = self.source("VectorworksMCPBridge.cpp")
        generic_factory = bridge[
            bridge.index('} else if (spec.objectType == "parametric")') :
            bridge.index('} else if (spec.objectType == "symbol")')
        ]
        self.assertIn("ObjectFamily::Parametric", bridge)
        self.assertIn("ObjectFamily::Symbol", bridge)
        self.assertIn("CreateVerifiedParametricObject", generic_factory)

        symbol_factory = bridge[
            bridge.index('} else if (spec.objectType == "symbol")') :
            bridge.index('} else if (spec.objectType == "wall")')
        ]
        native_factory = self.source("NativeObjectFactory.cpp")
        self.assertIn("PlaceVerifiedSymbol", symbol_factory)
        self.assertIn("gSDK->PlaceSymbolN(", native_factory)
        self.assertIn("gSDK->AddObjectToContainer(instance, layer)", native_factory)
        self.assertIn("gSDK->ParentObject(instance) != layer", native_factory)
        self.assertNotIn("PlaceSymbolByNameN", native_factory)

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
        bridge = self.source("VectorworksMCPBridge.cpp")
        transport_header = self.source("NativeTransport.hpp")
        transport_source = self.source("NativeTransport.cpp")

        self.assertIn("enum class CommitState", header)
        self.assertIn("RequestedPath()", header)
        self.assertIn("ActivePath()", header)
        self.assertIn("CommitState commitState", header)
        self.assertIn("CommitState::Unknown", source)
        self.assertIn("CommitState::Accepted", source)
        self.assertIn("PreparedOpenDocument", header)
        self.assertIn("PrepareOpenDocument", source)
        self.assertIn("LaunchPreparedOpenDocument", source)
        self.assertIn("SDK_VERSION >= 3000", source)
        self.assertIn("replacementConfirmationRequired = true", source)
        self.assertIn("IsActiveFileChangedAfterLastSave", source)
        self.assertIn("gSDK->OpenDocumentPath(identifier, false)", source)
        self.assertIn("auto identifier = FileIdentifier(path)", source)
        self.assertIn("gSDK->GetOpenFilesList(openFiles)", source)
        self.assertIn("gSDK->SwitchToOpenFile(openFile.fFileRef)", source)
        self.assertGreaterEqual(source.count("gSDK->DrawScreen()"), 2)
        self.assertIn("fs::equivalent(path, openPath, error)", source)
        self.assertNotIn("ShellExecuteExW", source)
        self.assertNotIn("SEE_MASK_NOCLOSEPROCESS", source)
        self.assertNotIn("GetModuleFileNameW", source)
        self.assertNotIn('L"explorer.exe', source)
        self.assertNotIn("CreateProcessW", source)

        native_io = self.source("NativeIOHandlers.cpp")
        self.assertIn("EExportMode::eDWGDXF", native_io)
        self.assertIn("VectorWorks::Filing::eExportDWGDXF", native_io)
        self.assertIn("kDwgExportMode", native_io)
        lifecycle_start = bridge.index("std::string HandleDocumentLifecycle")
        lifecycle_end = bridge.index("int ParseIntegerString", lifecycle_start)
        lifecycle = bridge[lifecycle_start:lifecycle_end]
        self.assertIn("StageDeferredDocumentOpen", lifecycle)
        self.assertNotIn("ViewDocument::OpenDocument(", lifecycle)
        deferred_start = bridge.index("if (auto deferredOpen = TakeReadyDeferredDocumentOpen())")
        deferred_end = bridge.index("constexpr std::size_t kMaxRequestsPerPump", deferred_start)
        deferred = bridge[deferred_start:deferred_end]
        self.assertIn("LaunchPreparedOpenDocument", deferred)
        self.assertNotIn("gTransport.Stop()", deferred)
        self.assertIn("ResponseSentCallback", transport_header)
        self.assertRegex(
            bridge,
            r"#else\s+void MarkDeferredDocumentOpenResponseSent\(\s*"
            r"const Protocol::RequestEnvelope&,\s*"
            r"const Protocol::ResponseEnvelope&\) \{\}",
        )
        callback_start = transport_source.index("if (responseSentCallback_)")
        write_start = transport_source.rfind("WriteFrame", 0, callback_start)
        self.assertGreater(callback_start, write_start)
        self.assertIn("TryStartNativeTransport", bridge)
        self.assertIn("native-bridge-startup.log", bridge)
        self.assertIn("kTransportStartRetryInterval", bridge)
        self.assertIn("GET_MODULE_HANDLE_EX_FLAG_PIN", bridge)
        self.assertIn("PinBridgeModuleForProcessLifetime", bridge)
        pump_start = bridge.index("void OnVectorworksMainPluginEvent()")
        deferred_start = bridge.index(
            "if (auto deferredOpen = TakeReadyDeferredDocumentOpen())",
            pump_start,
        )
        pump_prefix = bridge[pump_start:deferred_start]
        self.assertIn("!gTransport.IsRunning()", pump_prefix)
        self.assertIn("TryStartNativeTransport()", pump_prefix)
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
