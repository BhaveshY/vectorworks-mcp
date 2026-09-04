import json
import runpy
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import server
from tests.test_server_protocol import FakeListener, _configure_server, _native_phase_four_status


ROOT = Path(__file__).resolve().parents[1]
BINDING = {
    "file_path": r"C:\models\Documentation Fixture.vwx",
    "file_name": "Documentation Fixture.vwx",
    "document_fingerprint": "fnv1a64:fixture-document",
    "document_generation": 7,
    "bridge_session_id": "native-session-123",
    "dirty": False,
    "active_layer_uuid": "layer-active-123",
    "active_layer_name": "Design Layer-1",
}


def _response(request, result):
    return {"id": request["id"], "success": True, "result": result}


def _documentation_status():
    status = _native_phase_four_status()
    status["capability_revision"] = 5
    status["bridge_session_id"] = BINDING["bridge_session_id"]
    status["implemented_actions"] = sorted(set(status["implemented_actions"]) | {
        "get_sheet_layers",
        "get_viewports",
        "get_viewport_annotations",
        "apply_documentation_operations",
        "export_pdf",
        "capture_view",
    })
    return status


class DocumentationWorkflowContractTests(unittest.TestCase):
    def tearDown(self):
        server._close()

    def test_sheet_reads_autobind_and_preserve_native_pagination(self):
        status = _documentation_status()

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            if request["action"] == "get_document_info":
                return _response(request, {"binding": BINDING})
            self.assertEqual(request["action"], "get_sheet_layers")
            self.assertEqual(request["params"]["expected_file_path"], BINDING["file_path"])
            self.assertEqual(request["params"]["expected_document_generation"], 7)
            self.assertEqual(request["params"]["expected_bridge_session_id"], BINDING["bridge_session_id"])
            self.assertEqual(request["params"]["expected_active_layer_uuid"], BINDING["active_layer_uuid"])
            self.assertEqual(request["params"]["expected_active_layer_name"], BINDING["active_layer_name"])
            self.assertEqual(request["params"]["offset"], 0)
            return _response(request, {
                "binding": BINDING,
                "items": [{"uuid": "sheet-1", "name": "A-101", "title": "Plan"}],
                "page": {"offset": 0, "limit": 50, "returned": 1, "total": 1, "next_cursor": None},
            })

        with FakeListener(handler, max_requests=3) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_read("sheet_layers", limit=50))

        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["items"][0]["uuid"], "sheet-1")
        self.assertEqual(
            [request["action"] for request in listener.requests],
            ["ping", "get_document_info", "get_sheet_layers"],
        )
        self.assertEqual(
            server._normalise_target_binding(BINDING, require_dirty=True),
            BINDING,
        )

    def test_viewport_annotation_reads_require_exact_parent_uuids(self):
        invalid = json.loads(server.vw_read(
            "viewport_annotations",
            sheet_layer_uuid="name:A-101",
            viewport_uuid="uuid:viewport-1",
            target_binding=BINDING,
        ))
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["error"]["code"], "validation_error")

    def test_documentation_plan_uses_one_bound_native_transaction(self):
        status = _documentation_status()

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "apply_documentation_operations")
            params = request["params"]
            self.assertEqual(params["expected_document_fingerprint"], BINDING["document_fingerprint"])
            self.assertEqual(params["expected_dirty"], False)
            self.assertEqual(params["operation_count"], 3)
            wire = [json.loads(params[f"operation_{index}_json"]) for index in range(1, 4)]
            self.assertEqual(wire[0]["op"], "sheet_layer.create")
            self.assertEqual(wire[0]["sheet_width_mm"], 420.0)
            self.assertEqual(wire[1]["op"], "viewport.create")
            self.assertEqual(wire[1]["sheet_layer_ref"], "$sheet")
            self.assertEqual(wire[1]["source_layer_1_ref"], "uuid:design-layer-1")
            self.assertEqual(wire[2]["op"], "viewport_annotation.create")
            self.assertEqual(wire[2]["viewport_ref"], "$viewport")
            receipts = [
                {"index": index, "op": item["op"], "local_ref": item.get("local_ref"),
                 "uuid": f"created-{index}", "verified": True}
                for index, item in enumerate(wire, start=1)
            ]
            return _response(request, {
                "transaction": {"committed": True, "operation_count": 3, "operations": receipts}
            })

        operations = [
            {
                "type": "create_sheet_layer",
                "operation_id": "sheet",
                "params": {"name": "A-101", "title": "Plan", "sheet_width": 0.42, "sheet_height": 0.297},
            },
            {
                "type": "create_viewport",
                "operation_id": "viewport",
                "params": {
                    "sheet_layer_ref": "$sheet", "name": "Floor Plan", "scale": 50,
                    "x": 0.2, "y": 0.15, "projection_type": 0, "view_type": 0,
                    "render_type": 0, "foreground_render_type": 0,
                    "source_layers": [{"ref": "uuid:design-layer-1", "visibility": "normal"}],
                    "source_classes": [{"name": "None", "visibility": "normal"}],
                },
            },
            {
                "type": "create_viewport_annotation",
                "operation_id": "note",
                "params": {
                    "sheet_layer_ref": "$sheet", "viewport_ref": "$viewport", "annotation_kind": "text",
                    "class_name": "None", "text": "Verify fire rating", "x1": 0.01, "y1": 0.02,
                },
            },
        ]
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_execute_operations(
                operations, "documentation-fixture-1", coordinate_units="m", target_binding=BINDING
            ))

        self.assertTrue(result["ok"])
        self.assertTrue(result["atomic"])
        self.assertEqual(result["required_native_action"], "apply_documentation_operations")
        self.assertEqual(result["target_binding"], BINDING)
        self.assertEqual(result["commit_state"], "committed")
        self.assertTrue(result["writes_started"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "apply_documentation_operations"])

    def test_documentation_writes_fail_closed_before_dispatch(self):
        empty = json.loads(server.vw_execute_operations([], "documentation-empty"))
        self.assertFalse(empty["ok"])
        self.assertFalse(empty["writes_started"])

        create = [{
            "type": "create_sheet_layer",
            "operation_id": "sheet",
            "params": {"name": "A-101", "title": "Plan"},
        }]
        missing_binding = json.loads(server.vw_execute_operations(create, "documentation-no-binding"))
        self.assertFalse(missing_binding["ok"])
        self.assertIn("target_binding", missing_binding["error"])
        self.assertFalse(missing_binding["writes_started"])
        self.assertEqual(missing_binding["commit_state"], "not_started")

        missing_layer_binding = dict(BINDING)
        missing_layer_binding.pop("active_layer_uuid")
        missing_layer = json.loads(server.vw_execute_operations(
            create, "documentation-no-active-layer", target_binding=missing_layer_binding
        ))
        self.assertFalse(missing_layer["ok"])
        self.assertIn("active_layer_uuid", missing_layer["error"])
        self.assertFalse(missing_layer["writes_started"])

        mixed = create + [{"type": "delete", "params": {"target": "uuid:object-1"}}]
        mixed_result = json.loads(server.vw_execute_operations(
            mixed, "documentation-mixed", target_binding=BINDING
        ))
        self.assertFalse(mixed_result["ok"])
        self.assertIn("may not be mixed", mixed_result["error"])

        delete = [{
            "type": "delete_viewport",
            "params": {"target": "uuid:viewport-1", "sheet_layer_ref": "uuid:sheet-1", "confirm": "yes"},
        }]
        bad_confirmation = json.loads(server.vw_execute_operations(
            delete, "documentation-bad-confirm", target_binding=BINDING
        ))
        self.assertFalse(bad_confirmation["ok"])
        self.assertIn("DELETE_VIEWPORT_AND_ANNOTATIONS", bad_confirmation["error"])

        forward_parent = [{
            "type": "create_viewport",
            "operation_id": "viewport",
            "params": {
                "sheet_layer_ref": "$sheet", "name": "Plan", "scale": 50, "x": 1, "y": 1,
                "projection_type": 0, "view_type": 0, "render_type": 0, "foreground_render_type": 0,
                "source_layers": [{"ref": "uuid:design-1", "visibility": "normal"}],
                "source_classes": [{"name": "None", "visibility": "normal"}],
            },
        }, {
            "type": "create_sheet_layer", "operation_id": "sheet",
            "params": {"name": "A-102", "title": "Plan"},
        }]
        forward_result = json.loads(server.vw_execute_operations(
            forward_parent, "documentation-forward-parent", target_binding=BINDING
        ))
        self.assertFalse(forward_result["ok"])
        self.assertIn("prior create_sheet_layer", forward_result["error"])

    def test_documentation_transport_failure_is_unknown_and_never_retried(self):
        status = _documentation_status()
        operation = [{
            "type": "create_sheet_layer",
            "operation_id": "sheet",
            "params": {"name": "A-101", "title": "Plan"},
        }]
        with mock.patch.object(server, "_fast_execution_bridge_status", return_value=(status, None)), mock.patch.object(
            server,
            "_send",
            return_value="Unknown commit state after sending non-idempotent Vectorworks action",
        ) as send:
            result = json.loads(server.vw_execute_operations(
                operation, "documentation-unknown-commit", target_binding=BINDING
            ))
            applied = json.loads(server.vw_apply(
                operation, "documentation-unknown-commit-adapter", target_binding=BINDING
            ))

        self.assertFalse(result["ok"])
        self.assertEqual(result["commit_state"], "unknown")
        self.assertIsNone(result["writes_started"])
        self.assertFalse(result["retryable"])
        self.assertEqual(result["retry_policy"], "never_after_send")
        self.assertTrue(result["manual_reconciliation_required"])
        self.assertEqual(send.call_count, 2)
        self.assertEqual(applied["error"]["code"], "unknown_commit_state")
        self.assertEqual(applied["error"]["commit_state"], "unknown")
        self.assertFalse(applied["error"]["retryable"])

    def test_documentation_evidence_export_is_bound_to_the_exact_document(self):
        status = _documentation_status()

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "export_pdf")
            self.assertEqual(request["params"]["expected_file_path"], BINDING["file_path"])
            self.assertEqual(request["params"]["expected_document_fingerprint"], BINDING["document_fingerprint"])
            self.assertEqual(request["params"]["expected_document_generation"], BINDING["document_generation"])
            self.assertEqual(request["params"]["expected_bridge_session_id"], BINDING["bridge_session_id"])
            self.assertEqual(request["params"]["expected_active_layer_uuid"], BINDING["active_layer_uuid"])
            self.assertEqual(request["params"]["expected_active_layer_name"], BINDING["active_layer_name"])
            self.assertEqual(request["params"]["expected_dirty"], BINDING["dirty"])
            return _response(request, {"status": "completed", "path": r"C:\evidence\fixture.pdf"})

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_io(
                "export", r"C:\evidence\fixture.pdf", "pdf", target_binding=BINDING
            ))

        self.assertTrue(result["ok"], result)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "export_pdf"])

    def test_native_source_uses_real_sdk_objects_and_parent_checks(self):
        source = (ROOT / "native_bridge" / "src" / "DocumentationHandlers.cpp").read_text(encoding="utf-8")
        bridge = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        build = (ROOT / "scripts" / "build-native-bridge.ps1").read_text(encoding="utf-8")
        for token in (
            "CreateLayer(TXString(operation.name.c_str()), kLayerSheet)",
            "sdk.CreateViewport(sheet)",
            "GetViewportGroup(viewport, kViewportGroupAnnotation)",
            "AddViewportAnnotationObject(viewport, annotation)",
            "sdk.ParentObject(viewport) != sheet",
            "ValidateTargetBinding",
            "Transactions::NativeTransaction",
        ):
            self.assertIn(token, source)
        self.assertIn('action == "apply_documentation_operations"', bridge)
        self.assertIn("hasTargetBinding", bridge)
        self.assertIn('"DocumentationHandlers.hpp"', build)
        self.assertIn('"DocumentationHandlers.cpp"', build)
        self.assertIn('GetStringParam(params, "expected_active_layer_uuid")', bridge)
        self.assertIn('GetStringParam(params, "expected_active_layer_name")', bridge)
        self.assertIn("actual.activeLayerUuid != expected.activeLayerUuid", source)
        apply_start = source.index("std::string ApplyOperations(")
        dirty_guard = source.index("if (!expected.hasDirty)", apply_start)
        binding_read = source.index("const DocumentBinding initialBinding", apply_start)
        transaction_start = source.index("Transactions::NativeTransaction transaction", apply_start)
        self.assertLess(dirty_guard, binding_read)
        self.assertLess(dirty_guard, transaction_start)
        restore = source.index("activeLayerRestorer.RestoreAndVerify()", apply_start)
        final_validation = source.index("ValidateTargetBinding(sdk, finalExpected)", restore)
        commit = source.index("transaction.Commit()", apply_start)
        self.assertLess(restore, final_validation)
        self.assertLess(final_validation, commit)

    def test_review_runner_is_read_only_checkpointed_and_source_bound(self):
        script = (ROOT / "scripts" / "review-all-sheets.py").read_text(encoding="utf-8")
        self.assertNotIn('call("vw_apply"', script)
        self.assertNotIn('call("vw_execute_operations"', script)
        self.assertIn("write_json_atomic(checkpoint_path", script)
        self.assertIn("state_unchanged", script)
        self.assertIn("authoritative_url", script)
        self.assertIn("source_resolved", script)

    def test_external_evidence_schema_binds_typed_native_identity(self):
        namespace = runpy.run_path(str(ROOT / "scripts" / "review-all-sheets.py"))
        validate = namespace["validate_external_evidence"]
        evidence = [{
            "check_id": "rating-1",
            "extracted_text": "90 min",
            "source": {
                "kind": "viewport_annotation",
                "object_uuid": "annotation-1",
                "sheet_layer_uuid": "sheet-1",
                "viewport_uuid": "viewport-1",
            },
            "authoritative_url": "https://authority.example/rating",
            "observed_value": "90 min",
            "expected_value": "90 min",
            "observed_at": "2026-09-04T10:00:00Z",
            "confidence": 0.98,
        }]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "evidence.json"
            path.write_text(json.dumps(evidence), encoding="utf-8")
            checked = validate(path)

        self.assertTrue(checked[0]["matches"])
        self.assertEqual(checked[0]["source"]["kind"], "viewport_annotation")

    def test_live_acceptance_requires_disposable_consent_and_records_unautomated_gates(self):
        script = (ROOT / "scripts" / "run-native-documentation-acceptance.py").read_text(encoding="utf-8")
        self.assertIn("--allow-write-fixture", script)
        self.assertIn("DISPOSABLE_DOCUMENT", script)
        self.assertIn("create_sheet_layer", script)
        self.assertIn("create_viewport_annotation", script)
        self.assertIn("delete_viewport_annotation", script)
        self.assertIn("manual_confirmation_required", script)
        self.assertNotIn("run_script", script)


if __name__ == "__main__":
    unittest.main()
