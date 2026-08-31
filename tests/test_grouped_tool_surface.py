import asyncio
import json
import unittest
from unittest.mock import patch

import server
from tests.test_server_protocol import FakeListener, _configure_server, _native_phase_four_status


def _response(request, result):
    return {"id": request["id"], "success": True, "result": result}


class GroupedToolSurfaceTests(unittest.TestCase):
    def test_fast_native_surface_and_action_discriminators_are_compact(self):
        self.assertEqual(
            server.FAST_NATIVE_TOOL_NAMES,
            {
                "vw_status",
                "vw_read",
                "vw_catalog",
                "vw_apply",
                "vw_io",
                "vw_view",
                "vw_document",
                "vw_execute_operations",
                "vw_tool_safety",
            },
        )

        schemas = {
            name: asyncio.run(server.mcp.get_tool(name)).parameters
            for name in ("vw_status", "vw_read", "vw_catalog", "vw_apply", "vw_io", "vw_view", "vw_document")
        }
        self.assertEqual(schemas["vw_status"]["properties"]["action"]["enum"], ["health", "context"])
        self.assertEqual(
            schemas["vw_read"]["properties"]["action"]["enum"],
            ["document", "layers", "summary", "query", "selection", "plan_quality"],
        )
        self.assertEqual(schemas["vw_read"]["properties"]["limit"]["maximum"], 200)
        self.assertIn("cursor", schemas["vw_catalog"]["properties"])
        self.assertEqual(
            schemas["vw_catalog"]["properties"]["action"]["enum"],
            ["capabilities", "classes", "symbols", "parametric_schemas", "worksheets", "resources"],
        )
        self.assertEqual(schemas["vw_io"]["properties"]["action"]["enum"], ["import", "export", "capture"])
        self.assertNotIn("idempotency_key", schemas["vw_io"]["properties"])
        self.assertEqual(schemas["vw_view"]["properties"]["action"]["enum"], ["get", "set", "fit", "capture"])
        self.assertEqual(schemas["vw_apply"]["properties"]["coordinate_units"]["enum"], ["mm", "cm", "m", "in", "ft"])
        self.assertEqual(
            schemas["vw_document"]["properties"]["action"]["enum"],
            ["info", "save", "export", "open"],
        )
        self.assertNotIn("idempotency_key", schemas["vw_document"]["properties"])

    def test_native_manifest_is_explicit_and_create_types_are_never_phase_inferred(self):
        status = _native_phase_four_status()
        self.assertEqual(server._fast_native_readiness_errors(status), [])
        self.assertEqual(server._native_create_object_types(status), set(status["create_object_types"]))

        stale = dict(status)
        stale["capability_revision"] = server.MIN_FAST_NATIVE_CAPABILITY_REVISION - 1
        self.assertIn(
            f"capability_revision is not >= {server.MIN_FAST_NATIVE_CAPABILITY_REVISION}",
            server._fast_native_readiness_errors(stale),
        )
        with patch.object(server, "_cached_cad_safe_status", return_value=stale):
            _, cached_error = server._fast_execution_bridge_status(
                server._new_request_trace("vw_apply", "apply")
            )
        self.assertIn("required phase-4 capability manifest is not ready", cached_error)

        no_manifest = dict(status)
        no_manifest.pop("capability_fingerprint")
        no_manifest.pop("create_object_types")
        self.assertIn("capability_fingerprint is missing", server._fast_native_readiness_errors(no_manifest))
        self.assertEqual(server._native_create_object_types(no_manifest), set())

    def test_grouped_variant_safety_is_exact_and_compact(self):
        with patch.dict("os.environ", {"VW_MCP_TOOL_PROFILE": "fast-native"}):
            safety = json.loads(server.vw_tool_safety())

        self.assertEqual(safety["vw_io"]["actions"]["import"]["retryPolicy"], "never_after_send")
        self.assertTrue(safety["vw_io"]["actions"]["import"]["writesDocument"])
        self.assertTrue(safety["vw_io"]["actions"]["export"]["writesFiles"])
        self.assertEqual(safety["vw_view"]["actions"]["get"]["retryPolicy"], "safe")
        self.assertEqual(safety["vw_document"]["actions"]["open"]["unknownCommitState"], "possible")
        self.assertFalse(safety["vw_document"]["actions"]["open"]["idempotentHint"])
        self.assertEqual(safety["vw_apply"]["retryPolicy"], "same_idempotency_key")

    def test_grouped_read_preflights_once_and_missing_capability_never_dispatches(self):
        status = _native_phase_four_status()

        def read_handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            if request["action"] == "get_document_info":
                return _response(request, {"filename": "Model.vwx", "layer_count": 3})
            self.fail(f"unexpected native dispatch: {request['action']}")

        with FakeListener(read_handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_read("document", fields=["filename"]))

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"], {"filename": "Model.vwx"})
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_document_info"])

        with FakeListener(lambda request: _response(request, status), max_requests=1) as listener:
            _configure_server(listener.port)
            unavailable = json.loads(server.vw_io("export", r"C:\temp\model.pdf", "pdf"))

        self.assertFalse(unavailable["ok"])
        self.assertEqual(unavailable["error"]["code"], "capability_unavailable")
        self.assertEqual(unavailable["error"]["required_native_action"], "export_pdf")
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_grouped_query_and_parametric_schema_forward_exact_filters(self):
        status = _native_phase_four_status()
        status["implemented_actions"] = sorted(
            set(status["implemented_actions"]) | {"describe_parametric_schema"}
        )

        def query_handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "get_objects")
            self.assertEqual(
                request["params"],
                {
                    "layer": "Level 1",
                    "object_type": "space",
                    "limit": 11,
                },
            )
            return _response(request, [])

        with FakeListener(query_handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_read(
                    "query",
                    criteria="T=SPACE",
                    layer="Level 1",
                    object_type="space",
                    limit=10,
                )
            )
        self.assertTrue(result["ok"])

        def schema_handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "describe_parametric_schema")
            self.assertEqual(request["params"], {"plugin_name": "Space"})
            return _response(
                request,
                {"universal_plugin_name": "Space", "descriptor_fingerprint": "space-v1", "parameters": []},
            )

        with FakeListener(schema_handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_catalog("parametric_schemas", query="Space"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["descriptor_fingerprint"], "space-v1")

    def test_catalog_rejects_capability_manifest_identity_mismatch(self):
        status = _native_phase_four_status()

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "capabilities")
            return _response(
                request,
                {
                    "capability_revision": status["capability_revision"],
                    "capability_fingerprint": "sha256:different-manifest",
                },
            )

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_catalog("capabilities"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "capability_manifest_mismatch")
        self.assertEqual(result["error"]["writes_started"], False)
        self.assertEqual(result["error"]["retry_policy"], "after_preflight_repair")

    def test_document_unknown_commit_state_is_non_retryable(self):
        status = _native_phase_four_status()
        status["implemented_actions"] = sorted(set(status["implemented_actions"]) | {"open_document"})

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "open_document")
            return {
                "id": request["id"],
                "success": False,
                "error": "unknown_commit_state: response was lost after Vectorworks accepted the request",
            }

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_document("open", file_path=r"C:\models\sample.vwx"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "unknown_commit_state")
        self.assertEqual(result["error"]["commit_state"], "unknown")
        self.assertEqual(result["error"]["retry_policy"], "never_after_send")
        self.assertFalse(result["error"]["retryable"])

    def test_document_open_confirms_deferred_native_acceptance_with_readback(self):
        status = _native_phase_four_status()
        status["implemented_actions"] = sorted(set(status["implemented_actions"]) | {"open_document"})
        requested_path = r"C:\models\sample.vwx"
        actions = []

        def handler(request):
            actions.append(request["action"])
            if request["action"] == "ping":
                return _response(request, status)
            if request["action"] == "open_document":
                return _response(
                    request,
                    {
                        "operation": "open_document",
                        "path": requested_path,
                        "requested_path": requested_path,
                        "active_path": "",
                        "commit_state": "accepted",
                    },
                )
            self.assertEqual(request["action"], "get_document_info")
            return _response(request, {"filepath": requested_path, "filename": "sample.vwx"})

        with FakeListener(handler, max_requests=3) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_document("open", file_path=requested_path))

        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["commit_state"], "committed")
        self.assertEqual(result["data"]["readback"]["filepath"], requested_path)
        self.assertEqual(actions, ["ping", "open_document", "get_document_info"])

    def test_grouped_apply_is_only_an_atomic_execute_operations_adapter(self):
        native_result = json.dumps(
            {
                "ok": True,
                "tool": "vw_execute_operations",
                "atomic": True,
                "idempotency_key": "plan-1",
            }
        )
        operations = [
            {
                "type": "create",
                "operation_id": "room-101",
                "params": {
                    "object_type": "space",
                    "points": [[0, 0], [4000, 0], [4000, 3000], [0, 3000]],
                    "height": 2800,
                    "name": "Meeting Room",
                    "room_id": "101",
                },
            },
            {
                "type": "create",
                "operation_id": "room-102",
                "params": {
                    "object_type": "space",
                    "points": [[4000, 0], [8000, 0], [8000, 3000], [4000, 3000]],
                    "height": 2800,
                    "name": "Office",
                    "room_id": "102",
                },
            },
        ]
        with patch.object(server, "vw_execute_operations", return_value=native_result) as execute:
            result = json.loads(server.vw_apply(operations, "plan-1"))

        execute.assert_called_once_with(operations, "plan-1", "mm")
        self.assertTrue(result["atomic"])
        self.assertEqual(result["tool"], "vw_apply")
        self.assertEqual(result["delegated_tool"], "vw_execute_operations")
        self.assertEqual(execute.call_count, 1)
        self.assertTrue(all(item["params"]["object_type"] == "space" for item in operations))

    def test_atomic_normalization_accepts_native_slab_roof_and_space(self):
        boundary = [[0, 0], [5000, 0], [5000, 4000], [0, 4000]]
        normalized = server._normalise_execute_operations(
            [
                {
                    "type": "create",
                    "params": {
                        "object_type": "slab",
                        "points": boundary,
                        "thickness": 250,
                        "elevation": 100,
                        "style_name": "Floor Slab",
                    },
                },
                {
                    "type": "create",
                    "params": {
                        "object_type": "roof",
                        "points": boundary,
                        "thickness": 300,
                        "slope": 35,
                        "overhang": 600,
                        "bearing_height": 3200,
                        "bearing_inset": 100,
                        "vertical_miter": 50,
                        "miter_type": 2,
                        "generate_gable_walls": True,
                    },
                },
                {
                    "type": "create",
                    "operation_id": "space-1",
                    "params": {
                        "object_type": "space",
                        "points": boundary,
                        "height": 2800,
                        "name": "Conference Room",
                        "room_id": "R-101",
                        "class_name": "A-Space",
                    },
                },
                {"type": "transform", "params": {"target": "$space-1", "dx": 500, "rotation_deg": 15}},
                {
                    "type": "duplicate",
                    "operation_id": "space-copy",
                    "params": {"target": "$space-1", "dx": 6000, "dy": 0},
                },
                {"type": "delete", "params": {"target": "uuid:obsolete-object"}},
            ]
        )

        params = [operation["params"] for operation in normalized]
        self.assertEqual([item["object_type"] for item in params[:3]], ["slab", "roof", "space"])
        self.assertEqual(params[1]["elevation"], 3200.0)
        self.assertTrue(params[1]["generate_gable_walls"])
        self.assertEqual(params[2]["room_id"], "R-101")
        self.assertEqual(
            params[3],
            {
                "target": "$space-1",
                "dx": 500.0,
                "dy": 0.0,
                "rotation_deg": 15.0,
                "scale_x": 1.0,
                "scale_y": 1.0,
            },
        )
        self.assertEqual(params[4], {"target": "$space-1", "dx": 6000.0, "dy": 0.0})
        self.assertEqual(params[5], {"target": "uuid:obsolete-object"})

    def test_atomic_door_window_create_uses_exact_hosted_native_wire_shape(self):
        status = _native_phase_four_status()

        expected_wire = [
            {
                "op": "create",
                "object_type": "door",
                "plugin_name": "Door",
                "descriptor_fingerprint": "door-schema-v1",
                "x1": 1200.0,
                "y1": 0.0,
                "rotation": 0.0,
                "require_wall_host": True,
                "wall_uuid": "wall-uuid-1",
                "width": 900.0,
                "height": 2100.0,
                "parameter_count": 1,
                "parameter_1_name": "Operation",
                "parameter_1_type": "integer",
                "parameter_1_integer": 1,
                "local_ref": "entry-door",
            },
            {
                "op": "create",
                "object_type": "window",
                "plugin_name": "Window",
                "descriptor_fingerprint": "window-schema-v1",
                "x1": 3200.0,
                "y1": 0.0,
                "rotation": 0.0,
                "require_wall_host": True,
                "wall_uuid": "wall-uuid-1",
                "width": 1200.0,
                "height": 1500.0,
                "sill_height": 900.0,
                "parameter_count": 0,
                "local_ref": "living-window",
            },
        ]

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "apply_operations")
            wire = [
                json.loads(request["params"][f"operation_{index}_json"])
                for index in range(1, request["params"]["operation_count"] + 1)
            ]
            self.assertEqual(wire, expected_wire)
            return _response(
                request,
                {
                    "transaction": {
                        "committed": True,
                        "operation_count": 2,
                        "operations": [
                            {"index": 1, "type": "door", "verified": True},
                            {"index": 2, "type": "window", "verified": True},
                        ],
                    }
                },
            )

        operations = [
            {
                "type": "create",
                "operation_id": "entry-door",
                "params": {
                    "object_type": "door",
                    "plugin_name": "Door",
                    "descriptor_fingerprint": "door-schema-v1",
                    "wall_uuid": "wall-uuid-1",
                    "x": 1200,
                    "y": 0,
                    "width": 900,
                    "height": 2100,
                    "parameters": [{"id": "Operation", "type": "integer", "value": 1}],
                },
            },
            {
                "type": "create",
                "operation_id": "living-window",
                "params": {
                    "object_type": "window",
                    "plugin_name": "Window",
                    "descriptor_fingerprint": "window-schema-v1",
                    "wall_uuid": "wall-uuid-1",
                    "x": 3200,
                    "y": 0,
                    "width": 1200,
                    "height": 1500,
                    "sill_height": 900,
                },
            },
        ]
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_apply(operations, "hosted-openings-1"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["atomic"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "apply_operations"])

    def test_door_window_require_exact_host_and_manifest_advertisement(self):
        missing_wall = json.loads(
            server.vw_apply(
                [
                    {
                        "type": "create",
                        "params": {
                            "object_type": "door",
                            "plugin_name": "Door",
                            "descriptor_fingerprint": "door-schema-v1",
                            "x": 1000,
                            "y": 0,
                        },
                    }
                ],
                "missing-door-host-1",
            )
        )
        self.assertFalse(missing_wall["ok"])
        self.assertEqual(missing_wall["error"]["code"], "validation_error")
        self.assertFalse(missing_wall["error"]["writes_started"])
        self.assertEqual(missing_wall["timing"]["attempts"], 0)
        self.assertIn("wall_uuid is required", missing_wall["error"]["message"])

        unhosted = json.loads(
            server.vw_apply(
                [
                    {
                        "type": "create",
                        "params": {
                            "object_type": "window",
                            "plugin_name": "Window",
                            "descriptor_fingerprint": "window-schema-v1",
                            "wall_uuid": "wall-uuid-1",
                            "x": 2000,
                            "y": 0,
                            "require_wall_host": False,
                        },
                    }
                ],
                "unhosted-window-1",
            )
        )
        self.assertFalse(unhosted["ok"])
        self.assertEqual(unhosted["error"]["code"], "validation_error")
        self.assertFalse(unhosted["error"]["writes_started"])
        self.assertEqual(unhosted["timing"]["attempts"], 0)
        self.assertIn("require_wall_host cannot be false", unhosted["error"]["message"])

        status = _native_phase_four_status()
        status["create_object_types"] = [
            object_type for object_type in status["create_object_types"] if object_type != "window"
        ]
        with FakeListener(lambda request: _response(request, status), max_requests=1) as listener:
            _configure_server(listener.port)
            unsupported = json.loads(
                server.vw_apply(
                    [
                        {
                            "type": "create",
                            "params": {
                                "object_type": "window",
                                "plugin_name": "Window",
                                "descriptor_fingerprint": "window-schema-v1",
                                "wall_uuid": "wall-uuid-1",
                                "x": 2000,
                                "y": 0,
                                "width": 1200,
                                "height": 1500,
                                "sill_height": 900,
                            },
                        }
                    ],
                    "unadvertised-window-1",
                )
            )

        self.assertFalse(unsupported["ok"])
        self.assertEqual(unsupported["error"]["code"], "capability_unavailable")
        self.assertFalse(unsupported["error"]["writes_started"])
        self.assertIn("not implemented", unsupported["error"]["message"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_door_window_reject_ambiguous_dedicated_inputs_before_dispatch(self):
        invalid_params = [
            {
                "object_type": "door",
                "plugin_name": "Custom Door",
                "descriptor_fingerprint": "door-schema-v1",
                "wall_uuid": "wall-uuid-1",
                "x": 1000,
                "y": 0,
                "width": 900,
                "height": 2100,
            },
            {
                "object_type": "window",
                "plugin_name": "Window",
                "descriptor_fingerprint": "window-schema-v1",
                "wall_uuid": "wall-uuid-1",
                "width": 1200,
                "height": 1500,
                "sill_height": 900,
            },
            {
                "object_type": "door",
                "plugin_name": "Door",
                "descriptor_fingerprint": "door-schema-v1",
                "wall_uuid": "wall-uuid-1",
                "x": 1000,
                "y": 0,
                "width": 900,
                "height": 2100,
                "sill_height": 0,
            },
            {
                "object_type": "window",
                "plugin_name": "Window",
                "descriptor_fingerprint": "window-schema-v1",
                "wall_uuid": "wall-uuid-1",
                "x": 1000,
                "y": 0,
                "width": 1200,
                "height": 1500,
                "sill_height": 900,
                "parameters": [{"id": "Elevation", "type": "real", "value": 900}],
            },
            {
                "object_type": "door",
                "plugin_name": "Door",
                "descriptor_fingerprint": 7,
                "wall_uuid": "wall-uuid-1",
                "x": 1000,
                "y": 0,
                "width": 900,
                "height": 2100,
            },
            {
                "object_type": "window",
                "plugin_name": "Window",
                "descriptor_fingerprint": "window-schema-v1",
                "wall_uuid": 42,
                "x": 1000,
                "y": 0,
                "width": 1200,
                "height": 1500,
                "sill_height": 900,
            },
        ]

        for index, params in enumerate(invalid_params, start=1):
            with self.subTest(index=index):
                result = json.loads(
                    server.vw_apply(
                        [{"type": "create", "params": params}],
                        f"invalid-hosted-opening-{index}",
                    )
                )
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "validation_error")
                self.assertFalse(result["error"]["writes_started"])
                self.assertEqual(result["timing"]["attempts"], 0)

    def test_atomic_mutation_wire_contract_matches_native_parser(self):
        status = _native_phase_four_status()

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "apply_operations")
            wire = [
                json.loads(request["params"][f"operation_{index}_json"])
                for index in range(1, request["params"]["operation_count"] + 1)
            ]
            self.assertEqual(
                wire,
                [
                    {
                        "op": "object.transform",
                        "target": "uuid:a",
                        "delta_x": 10.0,
                        "delta_y": 20.0,
                        "rotation_degrees": 30.0,
                        "scale_x": 2.0,
                        "scale_y": 3.0,
                        "pivot_x": 4.0,
                        "pivot_y": 5.0,
                    },
                    {
                        "op": "object.duplicate",
                        "target": "uuid:a",
                        "delta_x": 100.0,
                        "delta_y": 200.0,
                        "local_ref": "copy-a",
                    },
                    {"op": "object.delete", "target": "uuid:b", "confirm": "DELETE_OBJECT"},
                ],
            )
            return _response(
                request,
                {"transaction": {"committed": True, "operations": wire}},
            )

        operations = [
            {
                "type": "transform",
                "params": {
                    "target": "uuid:a",
                    "dx": 10,
                    "dy": 20,
                    "rotation_deg": 30,
                    "scale_x": 2,
                    "scale_y": 3,
                    "pivot_x": 4,
                    "pivot_y": 5,
                },
            },
            {
                "type": "duplicate",
                "operation_id": "copy-a",
                "params": {"target": "uuid:a", "dx": 100, "dy": 200},
            },
            {"type": "delete", "params": {"target": "uuid:b"}},
        ]
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_execute_operations(operations, "mutations-wire-1"))

        self.assertTrue(result["ok"])

    def test_production_edits_normalize_units_and_keep_parametric_values_typed(self):
        status = _native_phase_four_status()

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "apply_operations")
            wire = [
                json.loads(request["params"][f"operation_{index}_json"])
                for index in range(1, request["params"]["operation_count"] + 1)
            ]
            self.assertEqual(
                wire,
                [
                    {
                        "op": "object.reshape",
                        "target": "uuid:wall-1",
                        "start_x": 0.0,
                        "start_y": 0.0,
                        "end_x": 5000.0,
                        "end_y": 0.0,
                    },
                    {
                        "op": "object.update_parametric",
                        "target": "uuid:door-1",
                        "object_type": "parametric",
                        "plugin_name": "Door",
                        "descriptor_fingerprint": "door-schema-v1",
                        "parameter_count": 1,
                        "parameter_1_name": "Operation",
                        "parameter_1_type": "integer",
                        "parameter_1_integer": 2,
                    },
                ],
            )
            return _response(request, {"transaction": {"committed": True, "operations": wire}})

        operations = [
            {
                "type": "reshape",
                "params": {
                    "target": "uuid:wall-1",
                    "start_x": 0,
                    "start_y": 0,
                    "end_x": 5,
                    "end_y": 0,
                },
            },
            {
                "type": "update_parametric",
                "params": {
                    "target": "uuid:door-1",
                    "plugin_name": "Door",
                    "descriptor_fingerprint": "door-schema-v1",
                    "parameters": [{"id": "Operation", "type": "integer", "value": 2}],
                },
            },
        ]
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_execute_operations(
                    operations,
                    "production-edits-1",
                    coordinate_units="m",
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["coordinate_units"], "m")
        self.assertEqual(result["native_coordinate_units"], "mm")

    def test_view_fit_is_one_native_set_view_call(self):
        status = _native_phase_four_status()
        status["implemented_actions"] = sorted(set(status["implemented_actions"]) | {"set_view"})

        def handler(request):
            if request["action"] == "ping":
                return _response(request, status)
            self.assertEqual(request["action"], "set_view")
            self.assertEqual(
                request["params"],
                {"file_path": "", "fit_to_objects": True, "clear_selection": True},
            )
            return _response(
                request,
                {"fit_to_objects_applied": True, "selection_cleared": True},
            )

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_view("fit"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["data"]["fit_to_objects_applied"])


if __name__ == "__main__":
    unittest.main()
