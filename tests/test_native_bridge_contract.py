import ast
import json
from pathlib import Path
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


def _listener_actions():
    tree = ast.parse((ROOT / "vw_listener.py").read_text(encoding="utf-8"))
    actions = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "HANDLERS":
                    actions.update(key.value for key in node.value.keys if isinstance(key, ast.Constant))
    actions.add("ping")
    return actions


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

    def test_handler_matrix_covers_every_listener_action(self):
        matrix = (ROOT / "native_bridge" / "HANDLER_MATRIX.md").read_text(encoding="utf-8")
        missing = [action for action in sorted(_listener_actions()) if "`{0}`".format(action) not in matrix]

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
