import ast
import json
from pathlib import Path
import socket
import time
import unittest

import server
from native_bridge.mock.mock_bridge import MockNativeBridge


ROOT = Path(__file__).resolve().parents[1]


def _configure_server(port):
    server._close()
    server.HOST = "127.0.0.1"
    server.PORT = port
    server.TIMEOUT = 1
    server.MAX_FRAME_BYTES = 1024 * 1024
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
    tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    actions = set()

    class Visitor(ast.NodeVisitor):
        def visit_Call(self, node):
            if (
                isinstance(node.func, ast.Name)
                and node.func.id == "_send"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                actions.add(node.args[0].value)
            self.generic_visit(node)

    Visitor().visit(tree)
    return actions


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
            ["get_document_info", "get_layers", "get_objects", "selection", "create_object"],
        )

    def test_mock_native_bridge_stop_releases_listener_port(self):
        with MockNativeBridge() as bridge:
            port = bridge.port
            _configure_server(port)

            result = server.vw_stop_listener()
            server._close()

        self.assertEqual(result, "Mock bridge stop requested")
        self.assertTrue(_wait_for_port_released(port))

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
