import ast
import json
import os
from pathlib import Path
import re
import socket
import time
import unittest
from unittest.mock import patch

import server
from native_bridge import smoke as smoke_module
from native_bridge.mock.mock_bridge import MockNativeBridge
from native_bridge.smoke import run_smoke


ROOT = Path(__file__).resolve().parents[1]


def _configure_server(port):
    server._close()
    server.HOST = "127.0.0.1"
    server.PORT = port
    server.TIMEOUT = 1
    server.MAX_FRAME_BYTES = 1024 * 1024
    server.PREFLIGHT_CACHE_SECONDS = 0.75
    server._CONFIG_ERROR = None


def _listener_handlers():
    tree = ast.parse((ROOT / "vw_listener.py").read_text(encoding="utf-8"))
    handlers = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "HANDLERS":
                    for key, value in zip(node.value.keys, node.value.values):
                        if isinstance(key, ast.Constant):
                            handlers[key.value] = value.id if isinstance(value, ast.Name) else ast.dump(value)
                elif (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "HANDLERS"
                    and isinstance(target.slice, ast.Constant)
                ):
                    handlers[target.slice.value] = "lambda" if isinstance(node.value, ast.Lambda) else ast.dump(node.value)
    return handlers


def _server_actions():
    return {value["wire_action"] for value in server.TOOL_SAFETY.values() if value.get("wire_action")}


def _matrix_rows():
    rows = {}
    matrix = (ROOT / "native_bridge" / "HANDLER_MATRIX.md").read_text(encoding="utf-8")
    for line in matrix.splitlines():
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2:
            continue
        action = cells[0].strip("`")
        if "." in action:
            continue
        rows[action] = cells[1]
    return rows


def _matrix_text():
    return (ROOT / "native_bridge" / "HANDLER_MATRIX.md").read_text(encoding="utf-8")


def _wait_for_port_released(port, timeout=2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            time.sleep(0.05)
        finally:
            probe.close()
    return False


class NativeBridgeContractTests(unittest.TestCase):
    def setUp(self):
        # The mock intentionally implements only the historical phase-one
        # contract. Production fast-native tests use the phase-four harness.
        self._profile_env = patch.dict(os.environ, {"VW_MCP_TOOL_PROFILE": "compat"})
        self._profile_env.start()

    def tearDown(self):
        server._close()
        self._profile_env.stop()

    def test_mock_native_bridge_supports_structured_preflight_and_cad_read(self):
        with MockNativeBridge() as bridge:
            _configure_server(bridge.port)

            preflight = json.loads(server.vw_preflight_for_cad())
            layers = json.loads(server.vw_get_layers())

        self.assertTrue(preflight["ok"])
        self.assertTrue(preflight["cad_api_safe"])
        self.assertTrue(preflight["native_bridge"])
        self.assertEqual(preflight["bridge_kind"], "native_sdk_bridge_mock")
        self.assertEqual(layers, [{"name": "Design Layer-1", "visible": True}])
        self.assertEqual([request["action"] for request in bridge.requests], ["ping", "get_layers"])

    def test_mock_native_bridge_covers_phase_one_actions(self):
        with MockNativeBridge() as bridge:
            _configure_server(bridge.port)

            info = json.loads(server.vw_get_document_info())
            layers = json.loads(server.vw_get_layers())
            objects = json.loads(server.vw_get_objects())
            selection = json.loads(server.vw_selection("get"))
            created = json.loads(server.vw_create_object("rect"))

        self.assertEqual(info["filename"], "Mock.vwx")
        self.assertEqual(layers[0]["name"], "Design Layer-1")
        self.assertEqual(objects[0]["handle"], "mock-rect-1")
        self.assertEqual(selection, [])
        self.assertEqual(created["handle"], "mock-created-1")
        self.assertEqual(created["uuid"], "mock-uuid-1")
        self.assertEqual(
            [request["action"] for request in bridge.requests],
            ["ping", "get_document_info", "get_layers", "get_objects", "selection", "create_object"],
        )

    def test_mock_native_bridge_stop_releases_listener_port(self):
        with MockNativeBridge() as bridge:
            port = bridge.port
            _configure_server(port)

            result = server.vw_stop_listener()
            server._close()

        self.assertEqual(result, "Mock bridge stop requested")
        self.assertTrue(_wait_for_port_released(port))

    def test_native_smoke_harness_accepts_mock_bridge(self):
        with MockNativeBridge() as bridge:
            report = run_smoke(
                port=bridge.port,
                ping_count=3,
                read_count=2,
                timeout=1,
                max_ping_ms=1000,
                max_read_ms=1000,
            )

        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["phase"], 1)
        self.assertEqual(report["max_ping_ms"], 1000)
        self.assertEqual(report["max_read_ms"], 1000)
        self.assertEqual(len(report["checks"]), 11)
        self.assertEqual(
            [request["action"] for request in bridge.requests],
            [
                "ping",
                "ping",
                "ping",
                "get_document_info",
                "get_document_info",
                "get_layers",
                "get_layers",
                "get_objects",
                "get_objects",
                "selection",
                "selection",
            ],
        )

    def test_native_smoke_harness_rejects_ping_latency_over_budget(self):
        status = {
            "pong": True,
            "handlers": 2,
            "version": "mock-native-bridge",
            "bridge_kind": "native_sdk_bridge_mock",
            "dispatch_mode": "native_sdk",
            "cad_api_safe": False,
            "transport_only": True,
            "native_bridge": True,
            "native_phase": 0,
            "implemented_actions": ["ping", "stop"],
        }

        def delayed_ping(_request):
            time.sleep(0.03)
            return {"success": True, "result": status}

        with MockNativeBridge(status=status, response_overrides={"ping": delayed_ping}) as bridge:
            report = run_smoke(
                port=bridge.port,
                ping_count=1,
                read_count=0,
                timeout=1,
                phase=0,
                max_ping_ms=1,
            )

        self.assertFalse(report["ok"])
        self.assertTrue(any("ping iteration 1 latency" in failure for failure in report["failures"]))
        self.assertTrue(any("ping latency budget 1ms" in failure for failure in report["failures"]))

    def test_native_smoke_harness_rejects_read_latency_over_budget(self):
        layers = [{"name": "Design Layer-1", "visible": True}]

        def delayed_layers(_request):
            time.sleep(0.03)
            return {"success": True, "result": layers}

        with MockNativeBridge(layers=layers, response_overrides={"get_layers": delayed_layers}) as bridge:
            report = run_smoke(
                port=bridge.port,
                ping_count=1,
                read_count=1,
                timeout=1,
                max_read_ms=1,
            )

        self.assertFalse(report["ok"])
        self.assertTrue(any("get_layers iteration 1 latency" in failure for failure in report["failures"]))
        self.assertTrue(any("read latency budget 1ms" in failure for failure in report["failures"]))

    def test_native_smoke_harness_accepts_phase_one_write_fixture(self):
        with MockNativeBridge() as bridge:
            report = run_smoke(
                port=bridge.port,
                ping_count=1,
                read_count=1,
                timeout=1,
                allow_write_fixture=True,
            )

        self.assertTrue(report["ok"], report["failures"])
        self.assertTrue(report["allow_write_fixture"])
        self.assertEqual(
            [request["action"] for request in bridge.requests],
            [
                "ping",
                "get_document_info",
                "get_layers",
                "get_objects",
                "selection",
                "create_object",
                "get_objects",
                "selection",
                "selection",
                "selection",
                "selection",
                "get_objects",
                "batch_create_objects",
                "get_objects",
                "selection",
                "selection",
                "selection",
                "selection",
                "get_objects",
            ],
        )

    def test_native_smoke_harness_accepts_phase_two_write_fixture(self):
        with MockNativeBridge() as bridge:
            report = run_smoke(
                port=bridge.port,
                ping_count=1,
                read_count=1,
                timeout=1,
                phase=2,
                allow_write_fixture=True,
            )

        self.assertTrue(report["ok"], report["failures"])
        actions = [request["action"] for request in bridge.requests]
        self.assertIn("create_wall", actions)
        self.assertIn("create_text", actions)
        self.assertIn("create_linear_dimension", actions)

    def test_native_smoke_harness_phase_zero_can_stop_bridge(self):
        with MockNativeBridge() as bridge:
            port = bridge.port
            report = run_smoke(port=port, ping_count=1, read_count=1, timeout=1, phase=0, stop=True)

        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["phase"], 0)
        self.assertTrue(report["stop_port_released"])
        self.assertEqual(report["checks"][-1]["iteration"], "port-release")
        self.assertEqual([request["action"] for request in bridge.requests], ["ping", "stop"])
        self.assertTrue(_wait_for_port_released(port))

    def test_native_smoke_harness_accepts_phase_zero_transport_scaffold(self):
        status = {
            "pong": True,
            "handlers": 2,
            "version": "native-scaffold-phase0",
            "bridge_kind": "native_sdk_bridge_scaffold",
            "dispatch_mode": "native_sdk",
            "cad_api_safe": False,
            "transport_only": True,
            "native_bridge": True,
            "cad_handlers_implemented": False,
        }
        with MockNativeBridge(status=status) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=0, timeout=1, phase=0)

        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["last_ping"]["cad_api_safe"], False)
        self.assertEqual(report["last_ping"]["transport_only"], True)
        self.assertEqual([request["action"] for request in bridge.requests], ["ping"])

    def test_native_smoke_harness_rejects_ping_count_below_one(self):
        report = run_smoke(port=1, ping_count=0, read_count=1, timeout=0.01)

        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"], [])
        self.assertIn("ping_count must be at least 1", report["failures"])

    def test_native_smoke_harness_rejects_phase_one_read_count_below_one(self):
        report = run_smoke(port=1, ping_count=1, read_count=0, timeout=0.01, phase=1)

        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"], [])
        self.assertIn("read_count must be at least 1 for phase >= 1", report["failures"])

    def test_native_smoke_harness_rejects_phase_zero_write_fixture(self):
        report = run_smoke(
            port=1,
            ping_count=1,
            read_count=0,
            timeout=0.01,
            phase=0,
            allow_write_fixture=True,
        )

        self.assertFalse(report["ok"])
        self.assertEqual(report["checks"], [])
        self.assertIn("allow_write_fixture requires phase >= 1", report["failures"])

    def test_native_smoke_harness_rejects_transport_scaffold_in_phase_one(self):
        status = {
            "pong": True,
            "handlers": 2,
            "version": "native-scaffold-phase0",
            "bridge_kind": "native_sdk_bridge_scaffold",
            "dispatch_mode": "native_sdk",
            "cad_api_safe": False,
            "transport_only": True,
            "native_bridge": True,
            "cad_handlers_implemented": False,
        }
        with MockNativeBridge(status=status) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1, phase=1)

        self.assertFalse(report["ok"])
        self.assertIn("ping handlers was not an integer >= 8", report["failures"])
        self.assertIn("bridge did not report cad_api_safe=true", report["failures"])
        self.assertIn("bridge did not report transport_only=false", report["failures"])
        self.assertIn("ping native_phase was not an integer >= 1", report["failures"])

    def test_native_smoke_harness_rejects_missing_phase_one_actions(self):
        status = {
            "pong": True,
            "handlers": 7,
            "version": "mock-native-bridge",
            "bridge_kind": "native_sdk_bridge_mock",
            "dispatch_mode": "native_sdk",
            "cad_api_safe": True,
            "transport_only": False,
            "native_bridge": True,
            "native_phase": 1,
            "implemented_actions": ["ping", "stop", "get_document_info", "get_layers", "get_objects", "create_object"],
            "main_context_pump": "win32_ui_timer",
            "main_context_pump_ready": True,
        }
        with MockNativeBridge(status=status) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1, phase=1)

        self.assertFalse(report["ok"])
        self.assertIn(
            "ping implemented_actions missing phase-1 action(s): batch_create_objects, selection",
            report["failures"],
        )

    def test_native_smoke_harness_rejects_phase_one_without_ready_pump(self):
        status = {
            "pong": True,
            "handlers": 7,
            "version": "mock-native-bridge",
            "bridge_kind": "native_sdk_bridge_mock",
            "dispatch_mode": "native_sdk",
            "cad_api_safe": True,
            "transport_only": False,
            "native_bridge": True,
            "native_phase": 1,
            "implemented_actions": [
                "ping",
                "stop",
                "get_document_info",
                "get_layers",
                "get_objects",
                "selection",
                "create_object",
            ],
            "main_context_pump": "win32_ui_timer",
            "main_context_pump_ready": False,
        }
        with MockNativeBridge(status=status) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1, phase=1)

        self.assertFalse(report["ok"])
        self.assertIn("ping main_context_pump_ready was not true", report["failures"])

    def test_native_smoke_harness_rejects_phase_one_selection_failure(self):
        with MockNativeBridge(
            response_overrides={
                "selection": {"success": False, "error": "selection get not implemented"},
            }
        ) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1, phase=1)

        self.assertFalse(report["ok"])
        self.assertIn("selection get not implemented", report["failures"])

    def test_native_smoke_harness_fails_if_stop_does_not_release_port(self):
        with MockNativeBridge(release_on_stop=False) as bridge:
            port = bridge.port
            report = run_smoke(port=port, ping_count=1, read_count=1, timeout=0.2, phase=0, stop=True)

        self.assertFalse(report["ok"])
        self.assertFalse(report["stop_port_released"])
        self.assertIn("bridge port did not close after stop", report["failures"])

    def test_native_smoke_write_fixture_deletes_by_exact_name_after_extra_selection(self):
        calls = []
        state = {"fixture_name": ""}
        original_record_call = smoke_module._record_call

        def fake_record_call(_sock, _report, action, iteration, params=None):
            calls.append((action, iteration, params or {}))
            if action == "create_object":
                state["fixture_name"] = str((params or {}).get("name", ""))
                return {"success": True, "result": "Created rect, handle: fixture-1"}
            if action == "batch_create_objects":
                state["batch_fixture_name"] = json.loads(str((params or {}).get("object_1_json", "{}"))).get("name", "")
                return {
                    "success": True,
                    "result": {
                        "atomic": True,
                        "created_count": 1,
                        "created": [{"handle": "batch-fixture-1", "type": "rect", "name": state["batch_fixture_name"]}],
                    },
                }
            if action == "get_objects" and iteration == "fixture-present":
                return {
                    "success": True,
                    "result": [{"handle": "fixture-1", "type": "rect", "name": state["fixture_name"]}],
                }
            if action == "get_objects" and iteration == "batch-fixture-present":
                return {
                    "success": True,
                    "result": [{"handle": "batch-fixture-1", "type": "rect", "name": state["batch_fixture_name"]}],
                }
            if action == "selection" and iteration == "fixture-get":
                return {
                    "success": True,
                    "result": [
                        {"handle": "fixture-1", "type": "rect", "name": state["fixture_name"]},
                        {"handle": "other-1", "type": "rect", "name": "Do Not Delete"},
                    ],
                }
            if action == "selection" and iteration == "batch-fixture-get":
                return {
                    "success": True,
                    "result": [
                        {"handle": "batch-fixture-1", "type": "rect", "name": state["batch_fixture_name"]},
                        {"handle": "other-1", "type": "rect", "name": "Do Not Delete"},
                    ],
                }
            if action == "selection" and iteration in {"fixture-delete", "batch-fixture-delete"}:
                return {"success": True, "result": {"deleted_count": 1}}
            if action == "get_objects" and iteration in {"fixture-cleanup", "batch-fixture-cleanup"}:
                return {"success": True, "result": []}
            return {"success": True, "result": "OK"}

        try:
            smoke_module._record_call = fake_record_call
            report = {"checks": [], "failures": []}
            smoke_module._run_phase_one_write_fixture(object(), report)
        finally:
            smoke_module._record_call = original_record_call

        self.assertFalse(report["failures"], report["failures"])
        self.assertIn(
            (
                "selection",
                "fixture-delete",
                {
                    "action": "delete",
                    "criteria": "((N='{0}'))".format(state["fixture_name"]),
                    "confirm": "DELETE_EXACT_NAME",
                },
            ),
            calls,
        )
        self.assertNotIn(("selection", "fixture-delete", {"action": "delete", "confirm": "DELETE_SELECTED"}), calls)

    def test_native_phase_two_smoke_deletes_by_exact_name_after_compound_selection(self):
        calls = []
        state = {"fixture_name": "", "handle": "phase2-fixture-1"}
        result_types = {
            "create_wall": "wall",
            "create_text": "text",
            "create_linear_dimension": "linear_dimension",
        }
        original_record_call = smoke_module._record_call

        def fake_record_call(_sock, _report, action, iteration, params=None):
            calls.append((action, iteration, params or {}))
            if action == "manage_classes" and iteration == "class-fixture-create":
                state["class_name"] = str((params or {}).get("class_name", ""))
                return {"success": True, "result": {"class_name": (params or {}).get("class_name"), "created": True}}
            if action == "manage_classes" and iteration == "class-fixture-list":
                return {"success": True, "result": [state.get("class_name", "")]}
            if action == "manage_classes" and iteration == "class-fixture-delete":
                return {"success": True, "result": {"deleted": True}}
            if action == "create_object" and iteration == "property-fixture-create":
                state["fixture_name"] = str((params or {}).get("name", ""))
                state["handle"] = "property-fixture-handle"
                return {"success": True, "result": {"type": "rect", "handle": state["handle"], "name": state["fixture_name"]}}
            if action == "set_property" and iteration == "property-fixture-set-name":
                state["fixture_name"] = str((params or {}).get("value", ""))
                state["property_renamed"] = state["fixture_name"]
                return {"success": True, "result": {"changed": True}}
            if action == "get_objects" and iteration == "property-fixture-readback":
                return {
                    "success": True,
                    "result": [{"handle": state["handle"], "type": "rect", "name": state["fixture_name"]}],
                }
            if action in result_types:
                state["fixture_name"] = str((params or {}).get("name", ""))
                state["handle"] = "{0}-handle".format(action)
                return {"success": True, "result": {"type": result_types[action], "handle": state["handle"]}}
            if action == "get_objects" and str(iteration).endswith("-present"):
                return {
                    "success": True,
                    "result": [{"handle": state["handle"], "type": "wall", "name": state["fixture_name"]}],
                }
            if action == "selection" and str(iteration).endswith("-get"):
                return {
                    "success": True,
                    "result": [
                        {"handle": state["handle"], "type": "wall", "name": state["fixture_name"]},
                        {"handle": "other-1", "type": "rect", "name": "Do Not Delete"},
                    ],
                }
            if action == "selection" and str(iteration).endswith("-delete"):
                return {"success": True, "result": {"deleted_count": 1}}
            if action == "get_objects" and str(iteration).endswith("-cleanup"):
                return {"success": True, "result": []}
            return {"success": True, "result": "OK"}

        try:
            smoke_module._record_call = fake_record_call
            report = {"checks": [], "failures": []}
            smoke_module._run_phase_two_write_fixture(object(), report)
        finally:
            smoke_module._record_call = original_record_call

        self.assertFalse(report["failures"], report["failures"])
        phase_two_deletes = [
            params
            for action, iteration, params in calls
            if action == "selection" and str(iteration).endswith("-delete")
        ]
        self.assertEqual(len(phase_two_deletes), 4)
        self.assertTrue(all(params.get("confirm") == "DELETE_EXACT_NAME" for params in phase_two_deletes))
        self.assertTrue(all(str(params.get("criteria", "")).startswith("((N='VW_MCP_NATIVE_PHASE2_SMOKE_") for params in phase_two_deletes))
        self.assertIn(
            (
                "set_property",
                "property-fixture-set-name",
                {
                    "handle": "property-fixture-handle",
                    "property_name": "name",
                    "value": state["property_renamed"],
                },
            ),
            calls,
        )

    def test_native_selection_delete_requires_raw_confirmation(self):
        source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        delete_branch = source[source.index('if (action == "delete")'):]

        self.assertIn('confirm") != "DELETE_EXACT_NAME"', delete_branch)
        self.assertIn('confirm") != "DELETE_SELECTED"', delete_branch)
        self.assertLess(
            delete_branch.index('confirm") != "DELETE_EXACT_NAME"'),
            delete_branch.index("SupportUndoAndRemove"),
        )
        self.assertLess(
            delete_branch.index('confirm") != "DELETE_SELECTED"'),
            delete_branch.index("SupportUndoAndRemove"),
        )

    def test_native_linear_dimension_filter_is_canonicalized(self):
        source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        matches_block = source[source.index("bool MatchesObjectType"):]

        self.assertIn('requestedType == "linear_dimension"', matches_block)
        self.assertIn('requestedType = "dimension"', matches_block)

    def test_native_space_filter_and_readback_are_semantic(self):
        source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        filter_block = source[source.index("bool IsSpaceObject"):source.index("bool IsUserVisibleObjectType")]
        object_json = source[source.index("std::string ObjectJson"):source.index("std::string ObjectListJson")]

        self.assertIn('parametric.GetParametricName()) == "Space"', filter_block)
        self.assertIn('requestedType == "space"', filter_block)
        self.assertIn("return IsSpaceObject(object)", filter_block)
        self.assertIn(r'\"native_type\":\"parametric\",\"plugin_name\":\"Space\"', object_json)
        self.assertIn('GetParamString(TXString("11_Room ID"))', object_json)
        self.assertIn('GetParamReal(TXString("11_Net Height"))', object_json)
        self.assertIn("support->NetArea(object, netArea)", object_json)
        self.assertIn("support->GrossArea(object, grossArea)", object_json)
        self.assertIn(r'\"net_area\"', object_json)
        self.assertIn(r'\"gross_area\"', object_json)

    def test_native_set_property_is_advertised_allowlisted_and_dispatched(self):
        bridge_source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        registry_source = (ROOT / "native_bridge" / "src" / "CapabilityRegistry.cpp").read_text(encoding="utf-8")

        self.assertIn('"set_property"', bridge_source)
        self.assertIn("HandleSetProperty", bridge_source)
        self.assertIn('request.action == "set_property"', bridge_source)
        self.assertIn('{"set_property", ExecutionContext::VectorworksMainPluginContext, 2u, true, false}', registry_source)

    def test_native_set_property_has_bounded_property_allowlist(self):
        source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        apply_block = source[source.index("void ApplyObjectProperty"):]
        set_property_block = source[source.index("std::string HandleSetProperty"):]

        for property_name in ("name", "class", "lineWeight", "opacity", "fillColor", "penColor"):
            self.assertIn(f'propertyName == "{property_name}"', apply_block)
        self.assertIn('throw std::invalid_argument("unsupported property: " + propertyName)', apply_block)
        self.assertIn("kMaxPropertyValueChars", set_property_block)
        self.assertIn("property value is too long", set_property_block)

    def test_native_set_property_requires_returned_session_handle(self):
        source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        handle_block = source[source.index("std::string HandleId"):]
        parse_block = source[source.index("MCObjectHandle ObjectHandleFromSessionId"):]

        self.assertIn("gKnownObjectHandleIds.insert(id)", handle_block)
        self.assertIn("gKnownObjectHandleIds.find(canonicalHandleId)", parse_block)
        self.assertIn("resolve the object with get_objects first", parse_block)

    def test_native_manage_classes_is_advertised_allowlisted_and_confirmed(self):
        bridge_source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        registry_source = (ROOT / "native_bridge" / "src" / "CapabilityRegistry.cpp").read_text(encoding="utf-8")
        manage_block = bridge_source[bridge_source.index("std::string HandleManageClasses"):]

        self.assertIn('"manage_classes"', bridge_source)
        self.assertIn("HandleManageClasses", bridge_source)
        self.assertIn('request.action == "manage_classes"', bridge_source)
        self.assertIn('{"manage_classes", ExecutionContext::VectorworksMainPluginContext, 2u, true, false}', registry_source)
        self.assertIn('confirm") != "DELETE_CLASS"', manage_block)
        self.assertIn("refusing to delete the None class", manage_block)

    def test_native_phase_four_fast_path_and_read_handlers_are_advertised_and_allowlisted(self):
        bridge_source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        registry_source = (ROOT / "native_bridge" / "src" / "CapabilityRegistry.cpp").read_text(encoding="utf-8")

        self.assertIn('"native_phase":4', bridge_source)
        self.assertIn('"find_objects"', bridge_source)
        self.assertIn('"drawing_summary"', bridge_source)
        self.assertIn('"apply_operations"', bridge_source)
        self.assertIn("HandleFindObjects", bridge_source)
        self.assertIn("HandleDrawingSummary", bridge_source)
        self.assertIn("HandleApplyOperations", bridge_source)
        self.assertIn('request.action == "find_objects"', bridge_source)
        self.assertIn('request.action == "drawing_summary"', bridge_source)
        self.assertIn('request.action == "apply_operations"', bridge_source)
        self.assertIn('{"find_objects", ExecutionContext::VectorworksMainPluginContext, 3u, false, false}', registry_source)
        self.assertIn('{"drawing_summary", ExecutionContext::VectorworksMainPluginContext, 3u, false, false}', registry_source)
        self.assertIn('{"apply_operations", ExecutionContext::VectorworksMainPluginContext, 4u, true, false}', registry_source)

    def test_native_capability_registry_is_complete_truthful_and_wired(self):
        registry_source = (ROOT / "native_bridge" / "src" / "CapabilityRegistry.cpp").read_text(encoding="utf-8")
        registry_header = (ROOT / "native_bridge" / "src" / "CapabilityRegistry.hpp").read_text(encoding="utf-8")
        domain_header = (ROOT / "native_bridge" / "src" / "NativeDomain.hpp").read_text(encoding="utf-8")
        bridge_source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")

        expected_actions = {
            "ping", "stop", "capabilities", "get_document_info", "get_layers", "get_objects",
            "selection", "create_object", "batch_create_objects", "create_wall", "create_text",
            "create_linear_dimension", "set_property", "manage_classes", "find_objects",
            "drawing_summary", "apply_operations",
        }
        registry_actions = set(re.findall(r'\{"([^"]+)", ExecutionContext::', registry_source))
        for action in expected_actions:
            self.assertIn('{"' + action + '", ExecutionContext::', registry_source)
        self.assertTrue(expected_actions <= registry_actions)
        self.assertIn("constexpr std::uint32_t kCapabilityRevision = 4u", registry_header)
        self.assertIn("struct ActionSpec", domain_header)
        self.assertIn("CapabilitiesResultJson", bridge_source)
        self.assertIn('request.action == "capabilities"', bridge_source)
        self.assertIn("ImplementedActionsJson(true)", bridge_source)
        self.assertIn("CreateObjectTypesJson(true)", bridge_source)
        self.assertRegex(registry_source, r'\{"slab",\s*"slab",\s*"kSlabNode",\s*"available"')
        self.assertRegex(registry_source, r'\{"roof",\s*"roof",\s*"kRoofContainerNode",\s*"available"')
        self.assertRegex(registry_source, r'\{"space",\s*"space",\s*"kParametricNode",\s*"available"')
        self.assertRegex(registry_source, r'\{"door",\s*"door",\s*"kParametricNode",\s*"requires_runtime_schema"')
        self.assertRegex(registry_source, r'\{"window",\s*"window",\s*"kParametricNode",\s*"requires_runtime_schema"')
        self.assertNotIn('"insert_door"', registry_source)
        self.assertNotIn('"insert_window"', registry_source)
        self.assertNotIn('"create_space"', registry_source)
        self.assertNotIn('"create_slab"', registry_source)
        self.assertNotIn('"create_roof"', registry_source)
        self.assertNotIn('"insert_symbol"', registry_source)

        for script_name in (
            "copy-native-bridge-scaffold.ps1",
            "build-native-bridge.ps1",
            "wire-native-bridge-project.ps1",
            "test-native-bridge-scaffold.ps1",
        ):
            script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
            self.assertIn("CapabilityRegistry.cpp", script)

        for script_name in (
            "copy-native-bridge-scaffold.ps1",
            "build-native-bridge.ps1",
            "wire-native-bridge-project.ps1",
        ):
            script = (ROOT / "scripts" / script_name).read_text(encoding="utf-8")
            self.assertIn("ParametricObjectAdapter.hpp", script)
            self.assertIn("ParametricObjectAdapter.cpp", script)
            self.assertIn("NativeObjectFactory.hpp", script)
            self.assertIn("NativeObjectFactory.cpp", script)
            self.assertIn("NativeTransaction.hpp", script)
            self.assertIn("NativeTransaction.cpp", script)
            self.assertIn("BimObjectHandlers.hpp", script)
            self.assertIn("BimObjectHandlers.cpp", script)
            self.assertIn("SpaceObjectHandlers.hpp", script)
            self.assertIn("SpaceObjectHandlers.cpp", script)
            self.assertIn("NativeIOHandlers.hpp", script)
            self.assertIn("NativeIOHandlers.cpp", script)

    def test_native_fast_path_supports_polygons_replay_guards_and_timing(self):
        bridge_source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        queue_source = (ROOT / "native_bridge" / "src" / "CadRequestQueue.hpp").read_text(encoding="utf-8")

        self.assertIn('spec.objectType == "polygon"', bridge_source)
        self.assertIn("CreatePolyshape", bridge_source)
        self.assertIn("SetPolyShapeClose", bridge_source)
        self.assertIn("vertex_count", bridge_source)
        self.assertIn("operationsFingerprint", bridge_source)
        self.assertIn("different operations payload", bridge_source)
        self.assertIn("different active document", bridge_source)
        self.assertIn("kMaxApplyOperationsCacheEntries", bridge_source)
        self.assertIn('constexpr std::string_view uuidPrefix = "uuid:"', bridge_source)
        self.assertIn('constexpr std::string_view namePrefix = "name:"', bridge_source)
        self.assertIn("name operation target is ambiguous", bridge_source)
        self.assertIn("QueueWaitMillisecondsForDiagnostics", queue_source)
        self.assertIn("queue_wait_ms", bridge_source)
        self.assertIn("handler_ms", bridge_source)
        self.assertIn("kMainContextPumpMessage", bridge_source)
        self.assertIn("PostMessageW", bridge_source)
        self.assertIn("kPumpBudget", bridge_source)
        self.assertIn("kMaxRequestsPerPump", bridge_source)
        self.assertIn("dirtyHandles.insert(object)", bridge_source)
        self.assertIn("CompactCreatedPrimitiveListJson(created)", bridge_source)

    def test_native_atomic_object_mutations_are_explicit_prevalidated_and_verified(self):
        source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        parser = source[source.index("std::vector<ApplyOperation> ParseApplyOperations"):]
        transaction = source[source.index("std::string HandleApplyOperations"):]

        for operation in ("object.transform", "object.duplicate", "object.delete"):
            self.assertIn(operation, parser)
            self.assertIn(operation, transaction)
        self.assertIn("target must be an explicit uuid:, name:, or handle: object reference", source)
        self.assertIn("PreparedApplyOperationTargets preparedTargets = PrepareApplyOperationTargets(operations)", transaction)
        self.assertLess(
            transaction.index("PrepareApplyOperationTargets(operations)"),
            transaction.index("Transactions::NativeTransaction transaction("),
        )
        self.assertIn("gSDK->MoveObject(object, operation.deltaX, operation.deltaY)", transaction)
        self.assertIn("gSDK->RotateObjectN(rotatedObject, pivot, operation.rotationDegrees)", transaction)
        self.assertIn("gSDK->ScaleObjectN(object, pivot, operation.scaleX, operation.scaleY)", transaction)
        self.assertIn("gSDK->DuplicateObject(object)", transaction)
        self.assertIn("gSDK->InsertObjectAfter(duplicate, object)", transaction)
        self.assertIn("gSDK->DeleteObject(object, true)", transaction)
        self.assertIn("gSDK->GetObjectByUuid(TXString(uuid.c_str())) != nullptr", transaction)
        self.assertIn(
            "transaction.TrackExternalBefore(object, ExternalMutationFamily(object))",
            transaction,
        )
        classifier = source[
            source.index("Transactions::ObjectFamily ExternalMutationFamily") :
            source.index("std::string CreatedPrimitiveListJson")
        ]
        for family in ("Parametric", "Space", "Slab", "Roof", "Door", "Window"):
            self.assertIn(f"Transactions::ObjectFamily::{family}", classifier)
        self.assertIn("return Transactions::ObjectFamily::Simple", classifier)
        self.assertIn("transaction.TrackExternalDeleted(mutation->second)", transaction)
        self.assertIn("transaction.DisposeFinal(artifactEntry->second)", transaction)
        self.assertIn("transaction.Commit()", transaction)
        self.assertIn("transaction.RollbackAndRethrow(std::current_exception())", transaction)
        self.assertIn("CreatePrimitiveFromSpec(\n                    operation.primitive, transaction, &artifact, &warnings)", transaction)
        self.assertNotIn(
            "gSDK->AddAfterSwapObject(object)",
            transaction[:transaction.index("if (operation.kind == ApplyOperationKind::SetProperty)")],
        )
        self.assertNotIn("CollectSelectedObjects", transaction)

    def test_native_writable_layer_requires_an_existing_design_layer(self):
        source = (ROOT / "native_bridge" / "src" / "VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        layer_block = source[source.index("MCObjectHandle EnsureWritableLayer("):]

        self.assertIn("active Vectorworks document has no writable design layer", layer_block)
        self.assertNotIn('CreateLayer(TXString("Vectorworks MCP Layer"), 1)', layer_block)
        self.assertNotIn("OpenDocumentPath(nullptr", layer_block)
        self.assertIn("if (gSDK->GetCurrentLayer() != layer)", layer_block)

    def test_native_smoke_write_fixture_aborts_cleanup_when_create_fails(self):
        calls = []
        original_record_call = smoke_module._record_call

        def fake_record_call(_sock, _report, action, iteration, params=None):
            calls.append((action, iteration, params or {}))
            if action == "create_object":
                return None
            return {"success": True, "result": "OK"}

        try:
            smoke_module._record_call = fake_record_call
            report = {"checks": [], "failures": []}
            smoke_module._run_phase_one_write_fixture(object(), report)
        finally:
            smoke_module._record_call = original_record_call

        self.assertEqual([call[0] for call in calls], ["create_object"])
        self.assertIn("skipped fixture cleanup because fixture creation did not succeed", report["failures"])
        self.assertNotIn(("selection", "fixture-clear-after-skip", {"action": "clear"}), calls)

    def test_native_smoke_harness_requires_write_fixture_flag_for_writes(self):
        with MockNativeBridge() as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1)

        self.assertTrue(report["ok"], report["failures"])
        self.assertNotIn(
            "create_object",
            [request["action"] for request in bridge.requests],
        )

    def test_native_smoke_harness_rejects_transport_only_bridge(self):
        status = {
            "pong": True,
            "handlers": 1,
            "version": "mock-transport-only",
            "bridge_kind": "python_transport_only",
            "dispatch_mode": "background",
            "cad_api_safe": False,
            "transport_only": True,
            "native_bridge": False,
        }
        with MockNativeBridge(status=status) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1)

        self.assertFalse(report["ok"])
        self.assertIn("bridge did not report cad_api_safe=true", report["failures"])
        self.assertIn("bridge did not report transport_only=false", report["failures"])
        self.assertIn("bridge did not report native_bridge=true", report["failures"])
        self.assertIn("ping dispatch_mode reported unsafe mode background", report["failures"])
        self.assertIn("ping bridge_kind reported unsafe bridge python_transport_only", report["failures"])

    def test_native_smoke_harness_rejects_incomplete_ping_schema(self):
        status = {
            "pong": True,
            "cad_api_safe": True,
            "transport_only": False,
            "native_bridge": True,
        }
        with MockNativeBridge(status=status) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=0, timeout=1, phase=0)

        self.assertFalse(report["ok"])
        self.assertIn("ping version was not a non-empty string", report["failures"])
        self.assertIn("ping bridge_kind was not a non-empty string", report["failures"])
        self.assertIn("ping dispatch_mode was not a non-empty string", report["failures"])
        self.assertIn("ping handlers was not an integer >= 2", report["failures"])

    def test_native_smoke_harness_rejects_non_boolean_success_envelope(self):
        with MockNativeBridge(
            response_overrides={
                "ping": {
                    "success": "true",
                    "result": {
                        "pong": True,
                        "handlers": 7,
                        "version": "mock-native-bridge",
                        "bridge_kind": "native_sdk_bridge_mock",
                        "dispatch_mode": "native_sdk",
                        "cad_api_safe": True,
                        "transport_only": False,
                        "native_bridge": True,
                    },
                }
            }
        ) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=0, timeout=1, phase=0)

        self.assertFalse(report["ok"])
        self.assertIn("bridge response success for ping was not boolean true/false", report["failures"])

    def test_native_smoke_harness_rejects_non_native_claims(self):
        for bridge_kind, dispatch_mode, expected in (
            (
                "python_foreground_diagnostic",
                "foreground",
                (
                    "ping dispatch_mode reported unsafe mode foreground",
                    "ping bridge_kind reported unsafe bridge python_foreground_diagnostic",
                    "native bridge dispatch_mode was not native_sdk",
                    "native bridge bridge_kind did not start with native_sdk_bridge",
                ),
            ),
            (
                "python_dialog_agent_session",
                "dialog",
                (
                    "native bridge dispatch_mode was not native_sdk",
                    "native bridge bridge_kind did not start with native_sdk_bridge",
                ),
            ),
        ):
            status = {
                "pong": True,
                "handlers": 7,
                "version": "claimed-native",
                "bridge_kind": bridge_kind,
                "dispatch_mode": dispatch_mode,
                "cad_api_safe": True,
                "transport_only": False,
                "native_bridge": True,
            }
            with self.subTest(bridge_kind=bridge_kind, dispatch_mode=dispatch_mode):
                with MockNativeBridge(status=status) as bridge:
                    report = run_smoke(port=bridge.port, ping_count=1, read_count=0, timeout=1, phase=0)

                self.assertFalse(report["ok"])
                for failure in expected:
                    self.assertIn(failure, report["failures"])

    def test_native_smoke_harness_rejects_bad_handler_count(self):
        for handlers in (True, 1):
            status = {
                "pong": True,
                "handlers": handlers,
                "version": "mock-native-bridge",
                "bridge_kind": "native_sdk_bridge_mock",
                "dispatch_mode": "native_sdk",
                "cad_api_safe": True,
                "transport_only": False,
                "native_bridge": True,
            }
            with self.subTest(handlers=handlers):
                with MockNativeBridge(status=status) as bridge:
                    report = run_smoke(port=bridge.port, ping_count=1, read_count=0, timeout=1, phase=0)

                self.assertFalse(report["ok"])
                self.assertIn("ping handlers was not an integer >= 2", report["failures"])

    def test_native_smoke_harness_rejects_malformed_phase_one_read_schemas(self):
        with MockNativeBridge(
            document_info={
                "filename": "",
                "filepath": 123,
                "layers": ["Design Layer-1", 42],
                "layer_count": 99,
                "total_objects": -1,
            },
            layers=[{"visible": True}, {"name": "Design Layer-2", "visible": "yes"}, "not-a-layer"],
            objects=[
                {
                    "handle": "",
                    "type": "",
                    "name": 42,
                    "type_id": -1,
                    "bounds": {"top_left": [0], "bottom_right": ["x", 1]},
                },
                "not-an-object",
            ],
        ) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1)

        self.assertFalse(report["ok"])
        for expected in (
            "get_document_info filename was not a non-empty string",
            "get_document_info filepath was not a string",
            "get_document_info layers was not a list of strings",
            "get_document_info layer_count did not match layers length",
            "get_document_info total_objects was not a non-negative integer",
            "get_layers item 0 name was not a non-empty string",
            "get_layers item 1 visible was not a boolean",
            "get_layers item 2 was not an object",
            "get_objects item 0 handle was not a non-empty string",
            "get_objects item 0 type was not a non-empty string",
            "get_objects item 0 type_id was not a non-negative integer",
            "get_objects item 0 name was not a string",
            "get_objects object 0 bounds.top_left was not a two-number list",
            "get_objects object 0 bounds.bottom_right was not a two-number list",
            "get_objects item 1 was not an object",
        ):
            self.assertIn(expected, report["failures"])

    def test_native_smoke_harness_rejects_malformed_selection_get_schema(self):
        with MockNativeBridge(
            response_overrides={
                "selection": {"success": True, "result": [{"handle": "", "type": ""}]},
            }
        ) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1)

        self.assertFalse(report["ok"])
        self.assertIn("selection get item 0 handle was not a non-empty string", report["failures"])
        self.assertIn("selection get item 0 type was not a non-empty string", report["failures"])

    def test_native_smoke_harness_cross_checks_phase_one_read_snapshots(self):
        with MockNativeBridge(
            document_info={
                "filename": "Mock.vwx",
                "filepath": "",
                "layers": ["Wrong Layer", "Extra Layer"],
                "layer_count": 2,
                "total_objects": 0,
            },
        ) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1)

        self.assertFalse(report["ok"])
        self.assertIn("get_document_info layers did not match get_layers names", report["failures"])
        self.assertIn("get_document_info layer_count did not match get_layers length", report["failures"])
        self.assertIn("get_document_info total_objects was less than returned get_objects count", report["failures"])

    def test_native_smoke_harness_rejects_object_limit_and_type_drift(self):
        objects = [
            {"handle": "line-1", "type": "line", "name": "Line"},
            *[
                {"handle": "rect-{0}".format(index), "type": "rect", "name": "Rect {0}".format(index)}
                for index in range(11)
            ],
        ]
        with MockNativeBridge(objects=objects, respect_object_filters=False) as bridge:
            report = run_smoke(port=bridge.port, ping_count=1, read_count=1, timeout=1)

        self.assertFalse(report["ok"])
        self.assertIn("get_objects returned more objects than requested limit 10", report["failures"])

        report = {"checks": [], "failures": []}
        smoke_module._validate_read_result(
            report,
            "get_objects",
            [{"handle": "line-1", "type": "line", "name": "Line"}],
            params={"object_type": "rect"},
        )
        self.assertIn("get_objects item 0 type did not match requested object_type rect", report["failures"])

    def test_native_smoke_write_fixture_validates_fixture_object_schema(self):
        calls = []
        state = {"fixture_name": ""}
        original_record_call = smoke_module._record_call

        def fake_record_call(_sock, _report, action, iteration, params=None):
            calls.append((action, iteration, params or {}))
            if action == "create_object":
                state["fixture_name"] = str((params or {}).get("name", ""))
                return {"success": True, "result": "Created rect, handle: fixture-1"}
            if action == "get_objects" and iteration == "fixture-present":
                return {
                    "success": True,
                    "result": [{"handle": "fixture-1", "type": "line", "name": state["fixture_name"]}],
                }
            if action == "selection" and iteration == "fixture-get":
                return {
                    "success": True,
                    "result": [{"handle": "fixture-1", "type": "line", "name": state["fixture_name"]}],
                }
            return {"success": True, "result": "OK"}

        try:
            smoke_module._record_call = fake_record_call
            report = {"checks": [], "failures": []}
            smoke_module._run_phase_one_write_fixture(object(), report)
        finally:
            smoke_module._record_call = original_record_call

        self.assertIn(
            "fixture object check item 0 type did not match requested object_type rect",
            report["failures"],
        )
        self.assertIn("skipped fixture delete because fixture selection was not proven safe", report["failures"])
        self.assertNotIn(("selection", "fixture-delete", {"action": "delete", "confirm": "DELETE_SELECTED"}), calls)

    def test_handler_matrix_matches_listener_and_server_wire_actions(self):
        listener_handlers = _listener_handlers()
        server_actions = _server_actions()
        matrix_rows = _matrix_rows()
        native_only_actions = {"apply_operations"}

        self.assertEqual(server_actions - native_only_actions, set(listener_handlers))
        self.assertTrue(native_only_actions <= server_actions)
        self.assertEqual(set(matrix_rows), set(listener_handlers) | native_only_actions)
        for action, handler in listener_handlers.items():
            if action == "ping":
                continue
            self.assertEqual(matrix_rows[action], "`{0}`".format(handler))

    def test_handler_matrix_documents_mixed_action_safety(self):
        matrix = _matrix_text()

        for text in (
            "`selection.get`",
            "`selection.delete`",
            "`worksheet.read_range`",
            "`worksheet.write`",
            "`manage_classes.list`",
            "`manage_classes.delete`",
            "`symbol.list`",
            "`symbol.insert`",
            "retry-safe",
            "unknown commit state",
        ):
            self.assertIn(text, matrix)


if __name__ == "__main__":
    unittest.main()
