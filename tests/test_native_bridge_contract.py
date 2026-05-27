import ast
import json
from pathlib import Path
import socket
import time
import unittest

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
        rows[action] = cells[1]
    return rows


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
    def tearDown(self):
        server._close()

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
            created = server.vw_create_object("rect")

        self.assertEqual(info["filename"], "Mock.vwx")
        self.assertEqual(layers[0]["name"], "Design Layer-1")
        self.assertEqual(objects[0]["handle"], "mock-rect-1")
        self.assertEqual(selection, [])
        self.assertIn("mock-created-1", created)
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
            report = run_smoke(port=bridge.port, ping_count=3, read_count=2, timeout=1)

        self.assertTrue(report["ok"], report["failures"])
        self.assertEqual(report["phase"], 1)
        self.assertEqual(len(report["checks"]), 9)
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
            ],
        )

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
                "create_object",
                "get_objects",
                "selection",
                "selection",
                "selection",
                "selection",
                "get_objects",
            ],
        )

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

    def test_native_smoke_harness_fails_if_stop_does_not_release_port(self):
        with MockNativeBridge(release_on_stop=False) as bridge:
            port = bridge.port
            report = run_smoke(port=port, ping_count=1, read_count=1, timeout=0.2, phase=0, stop=True)

        self.assertFalse(report["ok"])
        self.assertFalse(report["stop_port_released"])
        self.assertIn("bridge port did not close after stop", report["failures"])

    def test_native_smoke_write_fixture_refuses_unsafe_selection_delete(self):
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
                    "result": [{"handle": "fixture-1", "type": "rect", "name": state["fixture_name"]}],
                }
            if action == "selection" and iteration == "fixture-get":
                return {
                    "success": True,
                    "result": [
                        {"handle": "fixture-1", "type": "rect", "name": state["fixture_name"]},
                        {"handle": "other-1", "type": "rect", "name": "Do Not Delete"},
                    ],
                }
            return {"success": True, "result": "OK"}

        try:
            smoke_module._record_call = fake_record_call
            report = {"checks": [], "failures": []}
            smoke_module._run_phase_one_write_fixture(object(), report)
        finally:
            smoke_module._record_call = original_record_call

        self.assertIn("selection included non-fixture objects; refusing cleanup delete", report["failures"])
        self.assertIn("skipped fixture delete because fixture selection was not proven safe", report["failures"])
        self.assertNotIn(("selection", "fixture-delete", {"action": "delete"}), calls)
        self.assertIn(("selection", "fixture-clear-after-skip", {"action": "clear"}), calls)

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
        self.assertIn("bridge reported transport_only=true", report["failures"])
        self.assertIn("bridge did not report native_bridge=true", report["failures"])

    def test_handler_matrix_matches_listener_and_server_wire_actions(self):
        listener_handlers = _listener_handlers()
        server_actions = _server_actions()
        matrix_rows = _matrix_rows()

        self.assertEqual(server_actions, set(listener_handlers))
        self.assertEqual(set(matrix_rows), set(listener_handlers))
        for action, handler in listener_handlers.items():
            if action == "ping":
                continue
            self.assertEqual(matrix_rows[action], "`{0}`".format(handler))


if __name__ == "__main__":
    unittest.main()
