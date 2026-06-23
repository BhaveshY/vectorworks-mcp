import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import time
import types
import unittest
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MISSING = object()


def _read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        assert chunk
        data.extend(chunk)
    return bytes(data)


def _read_json_frame(sock):
    (size,) = struct.unpack(">I", _read_exact(sock, 4))
    return json.loads(_read_exact(sock, size).decode("utf-8"))


def _write_json_frame(sock, payload):
    data = json.dumps(payload).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _connect_with_retry(port):
    deadline = time.time() + 3
    last_error = None
    while time.time() < deadline:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=0.2)
        except OSError as exc:
            last_error = exc
            time.sleep(0.02)
    raise last_error


class ListenerProtocolTests(unittest.TestCase):
    def load_listener(self):
        alerts = []
        fake_vs = types.SimpleNamespace(AlrtDialog=alerts.append)

        old_vs = sys.modules.get("vs", MISSING)
        old_autostart = os.environ.get("VW_MCP_NO_AUTOSTART", MISSING)
        old_insecure_no_auth = os.environ.get("VW_MCP_INSECURE_NO_AUTH", MISSING)
        old_auth_token = os.environ.get("VW_MCP_AUTH_TOKEN", MISSING)
        sys.modules["vs"] = fake_vs
        os.environ["VW_MCP_NO_AUTOSTART"] = "1"
        os.environ["VW_MCP_INSECURE_NO_AUTH"] = "1"
        os.environ.pop("VW_MCP_AUTH_TOKEN", None)

        def restore():
            if old_vs is MISSING:
                sys.modules.pop("vs", None)
            else:
                sys.modules["vs"] = old_vs
            if old_autostart is MISSING:
                os.environ.pop("VW_MCP_NO_AUTOSTART", None)
            else:
                os.environ["VW_MCP_NO_AUTOSTART"] = old_autostart
            if old_insecure_no_auth is MISSING:
                os.environ.pop("VW_MCP_INSECURE_NO_AUTH", None)
            else:
                os.environ["VW_MCP_INSECURE_NO_AUTH"] = old_insecure_no_auth
            if old_auth_token is MISSING:
                os.environ.pop("VW_MCP_AUTH_TOKEN", None)
            else:
                os.environ["VW_MCP_AUTH_TOKEN"] = old_auth_token

        self.addCleanup(restore)

        module_name = "vw_listener_test_{id}".format(id=uuid.uuid4().hex)
        spec = importlib.util.spec_from_file_location(module_name, ROOT / "vw_listener.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        self.addCleanup(lambda: sys.modules.pop(module_name, None))
        spec.loader.exec_module(module)
        return module, alerts

    def test_client_state_buffers_partial_frames(self):
        listener, _alerts = self.load_listener()
        state = listener._ClientState(max_frame_bytes=64)
        payload = b'{"action":"ping"}'
        frame = struct.pack(">I", len(payload)) + payload

        state.feed(frame[:3])
        self.assertIsNone(state.pop_frame())
        state.feed(frame[3:7])
        self.assertIsNone(state.pop_frame())
        state.feed(frame[7:])

        self.assertEqual(state.pop_frame(), payload)

    def test_client_state_rejects_oversized_frames(self):
        listener, _alerts = self.load_listener()
        state = listener._ClientState(max_frame_bytes=8)
        state.feed(struct.pack(">I", 9))

        with self.assertRaises(listener.ProtocolError):
            state.pop_frame()

    def test_client_state_rejects_pending_read_buffer_over_limit(self):
        listener, _alerts = self.load_listener()
        state = listener._ClientState(max_frame_bytes=64, max_pending_read_bytes=8)

        with self.assertRaises(listener.ProtocolError):
            state.feed(b"x" * 9)

    def test_client_state_rejects_pending_write_buffer_over_limit(self):
        listener, _alerts = self.load_listener()
        state = listener._ClientState(max_frame_bytes=64, max_pending_write_bytes=12)

        with self.assertRaises(listener.ProtocolError):
            state.enqueue(b"x" * 9)

    def test_client_with_pending_write_is_write_only(self):
        listener, _alerts = self.load_listener()
        state = listener._ClientState(max_frame_bytes=64, max_pending_write_bytes=128)
        state.enqueue(b"{}")

        self.assertEqual(listener._client_events(state), listener.selectors.EVENT_WRITE)

    def test_dispatch_handles_ping_and_unknown_action(self):
        listener, _alerts = self.load_listener()

        ping = listener.dispatch({"id": "1", "action": "ping", "params": {}})
        self.assertEqual(ping["id"], "1")
        self.assertTrue(ping["success"])
        self.assertTrue(ping["result"]["pong"])
        self.assertEqual(ping["result"]["bridge_kind"], "python_unknown")
        self.assertFalse(ping["result"]["cad_api_safe"])
        self.assertFalse(ping["result"]["native_bridge"])

        unknown = listener.dispatch({"id": "2", "action": "missing", "params": {}})
        self.assertEqual(unknown, {"id": "2", "success": False, "error": "Unknown action: missing"})

    def test_dispatch_rejects_invalid_action_and_params(self):
        listener, _alerts = self.load_listener()

        bad_id = listener.dispatch({"id": 12, "action": "ping", "params": {}})
        self.assertFalse(bad_id["success"])
        self.assertIn("id", bad_id["error"])

        missing_action = listener.dispatch({"id": "missing", "params": {}})
        self.assertFalse(missing_action["success"])
        self.assertIn("action", missing_action["error"])

        non_string_action = listener.dispatch({"id": "bad-action", "action": 12, "params": {}})
        self.assertFalse(non_string_action["success"])
        self.assertIn("action", non_string_action["error"])

        array_params = listener.dispatch({"id": "bad-params", "action": "ping", "params": []})
        self.assertFalse(array_params["success"])
        self.assertIn("params", array_params["error"])

        null_params = listener.dispatch({"id": "null-params", "action": "ping", "params": None})
        self.assertFalse(null_params["success"])
        self.assertIn("params", null_params["error"])

    def test_dispatch_enforces_optional_auth_token(self):
        listener, _alerts = self.load_listener()
        listener.AUTH_TOKEN = "secret"

        missing = listener.dispatch({"id": "missing-auth", "action": "ping", "params": {}})
        valid = listener.dispatch({"id": "valid-auth", "auth_token": "secret", "action": "ping", "params": {}})

        self.assertFalse(missing["success"])
        self.assertIn("authentication", missing["error"])
        self.assertTrue(valid["success"])

    def test_dispatch_rejects_duplicate_and_non_finite_json(self):
        listener, _alerts = self.load_listener()

        with self.assertRaises(ValueError):
            listener._json_loads_strict('{"id":"1","id":"2","action":"ping","params":{}}')
        with self.assertRaises(ValueError):
            listener._json_loads_strict('{"id":"1","action":"ping","params":{"x":NaN}}')

    def test_raw_destructive_handlers_require_confirmation(self):
        listener, _alerts = self.load_listener()

        run_script = listener.dispatch({"id": "script", "action": "run_script", "params": {"code": "print('x')"}})
        delete_class = listener.dispatch(
            {"id": "class", "action": "manage_classes", "params": {"action": "delete", "class_name": "A-Test"}}
        )
        delete_selection = listener.dispatch({"id": "selection", "action": "selection", "params": {"action": "delete"}})
        inspect_plugin = listener.dispatch(
            {"id": "inspect", "action": "inspect_object", "params": {"plugin_name": "Door"}}
        )

        self.assertFalse(run_script["success"])
        self.assertIn("confirm", run_script["error"])
        self.assertFalse(delete_class["success"])
        self.assertIn("confirm", delete_class["error"])
        self.assertFalse(delete_selection["success"])
        self.assertIn("confirm", delete_selection["error"])
        self.assertFalse(inspect_plugin["success"])
        self.assertIn("confirm", inspect_plugin["error"])

    def test_raw_selection_delete_rejects_arbitrary_criteria(self):
        listener, _alerts = self.load_listener()

        delete_all = listener.dispatch(
            {
                "id": "selection-all",
                "action": "selection",
                "params": {"action": "delete", "criteria": "ALL", "confirm": "DELETE_EXACT_NAME"},
            }
        )
        missing_exact_confirm = listener.dispatch(
            {
                "id": "selection-name",
                "action": "selection",
                "params": {"action": "delete", "criteria": "((N='Fixture'))", "confirm": "DELETE_SELECTED"},
            }
        )

        self.assertFalse(delete_all["success"])
        self.assertIn("exact object-name", delete_all["error"])
        self.assertFalse(missing_exact_confirm["success"])
        self.assertIn("DELETE_EXACT_NAME", missing_exact_confirm["error"])

    def test_raw_phase_two_handlers_reject_invalid_params_before_api_call(self):
        listener, _alerts = self.load_listener()

        huge_text = listener.dispatch({
            "id": "text-large",
            "action": "create_text",
            "params": {"text": "x" * 4097},
        })
        bad_width = listener.dispatch({
            "id": "text-width",
            "action": "create_text",
            "params": {"text": "Room", "width": -1},
        })
        bad_dimension_type = listener.dispatch({
            "id": "dim-type",
            "action": "create_linear_dimension",
            "params": {"start_x": 0, "start_y": 0, "end_x": 100, "end_y": 0, "dimension_type": 9},
        })

        self.assertFalse(huge_text["success"])
        self.assertIn("4096", huge_text["error"])
        self.assertFalse(bad_width["success"])
        self.assertIn("width", bad_width["error"])
        self.assertFalse(bad_dimension_type["success"])
        self.assertIn("dimension_type", bad_dimension_type["error"])

    def test_listener_drops_idle_clients(self):
        listener, _alerts = self.load_listener()
        port = _free_port()
        listener.CLIENT_IDLE_SECONDS = 1
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        client = socket.create_connection(("127.0.0.1", port), timeout=1)
        conn, _addr = server.accept()
        sel = listener.selectors.DefaultSelector()
        try:
            state = listener._ClientState()
            state.last_activity = time.time() - 2
            sel.register(conn, listener.selectors.EVENT_READ, data=state)

            listener._drop_idle_clients(sel)

            self.assertEqual(len(sel.get_map()), 0)
        finally:
            sel.close()
            try: conn.close()
            except OSError: pass
            client.close()
            server.close()

    def test_listener_enforces_max_clients(self):
        listener, _alerts = self.load_listener()
        port = _free_port()
        listener.HOST = "127.0.0.1"
        listener.PORT = port
        listener.STOP_DIR = str(self.tmp_path())
        listener.STOP_FILE = str(Path(listener.STOP_DIR) / "STOP")
        listener.SCREENSHOT_DIR = listener.STOP_DIR
        listener.MAX_CLIENTS = 1
        listener.CLIENT_IDLE_SECONDS = 600

        thread = threading.Thread(target=listener.main, kwargs={"show_alerts": False}, daemon=True)
        thread.start()

        with _connect_with_retry(port) as first:
            _write_json_frame(first, {"id": "first", "action": "ping", "params": {}})
            ping = _read_json_frame(first)
            self.assertTrue(ping["success"])

            second = socket.create_connection(("127.0.0.1", port), timeout=1)
            try:
                second.settimeout(1)
                _write_json_frame(second, {"id": "second", "action": "ping", "params": {}})
                time.sleep(0.2)
                try:
                    self.assertEqual(second.recv(1), b"")
                except OSError:
                    pass
            finally:
                second.close()

            _write_json_frame(first, {"id": "stop", "action": "stop", "params": {}})
            stop = _read_json_frame(first)
            self.assertTrue(stop["success"])

        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_transport_only_modes_reject_cad_handlers(self):
        listener, _alerts = self.load_listener()
        for mode in ("background", "win_timer", "foreground"):
            with self.subTest(mode=mode):
                listener._DISPATCH_MODE = mode

                ping = listener.dispatch({"id": "1", "action": "ping", "params": {}})
                self.assertTrue(ping["success"])
                expected_kind = "python_foreground_diagnostic" if mode == "foreground" else "python_transport_only"
                self.assertEqual(ping["result"]["bridge_kind"], expected_kind)
                self.assertFalse(ping["result"]["cad_api_safe"])
                self.assertTrue(ping["result"]["transport_only"])

                stop = listener.dispatch({"id": "stop", "action": "stop", "params": {}})
                self.assertTrue(stop["success"])

                for action in ("get_layers", "get_document_info", "create_object", "run_script"):
                    result = listener.dispatch({"id": action, "action": action, "params": {}})
                    self.assertEqual(result["id"], action)
                    self.assertFalse(result["success"])
                    self.assertIn("transport-only", result["error"])

    def test_dialog_mode_ping_reports_cad_api_safe(self):
        listener, _alerts = self.load_listener()
        listener._DISPATCH_MODE = "dialog"

        ping = listener.dispatch({"id": "1", "action": "ping", "params": {}})

        self.assertTrue(ping["success"])
        self.assertEqual(ping["result"]["bridge_kind"], "python_dialog_agent_session")
        self.assertEqual(ping["result"]["dispatch_mode"], "dialog")
        self.assertTrue(ping["result"]["cad_api_safe"])
        self.assertFalse(ping["result"]["transport_only"])

    def test_runtime_handlers_use_documented_vectorworks_api_shapes(self):
        listener, _alerts = self.load_listener()

        class FakeVS:
            def __init__(self):
                self.calls = []

            def GetObject(self, name):
                self.calls.append(("GetObject", name))
                return "worksheet-handle" if name == "Schedule" else None

            def GetWSCellString(self, worksheet, row, col):
                self.calls.append(("GetWSCellString", worksheet, row, col))
                return "cell text"

            def SetWSCellFormula(self, worksheet, top, left, bottom, right, formula):
                self.calls.append(("SetWSCellFormula", worksheet, top, left, bottom, right, formula))

            def ImportDXFDWGFile(self, file_path):
                self.calls.append(("ImportDXFDWGFile", file_path))
                return 0

            def ImportImageFile(self, file_path, point):
                self.calls.append(("ImportImageFile", file_path, point))
                return "image-handle"

            def DoMenuTextByName(self, menu, index):
                self.calls.append(("DoMenuTextByName", menu, index))

            def CreateTextBlock(self, text, origin, fixed_size, width):
                self.calls.append(("CreateTextBlock", text, origin, fixed_size, width))
                return "text-handle"

            def SetName(self, handle, name):
                self.calls.append(("SetName", handle, name))

            def SetClass(self, handle, class_name):
                self.calls.append(("SetClass", handle, class_name))

            def SetTextWidth(self, handle, width):
                self.calls.append(("SetTextWidth", handle, width))

            def SetTextWrap(self, handle, wrapped):
                self.calls.append(("SetTextWrap", handle, wrapped))

            def PagePointsToCoordLength(self, points):
                self.calls.append(("PagePointsToCoordLength", points))
                return points * 10

            def SetTextSize(self, handle, first_char, num_chars, char_size):
                self.calls.append(("SetTextSize", handle, first_char, num_chars, char_size))

            def HRotate(self, handle, x, y, angle):
                self.calls.append(("HRotate", handle, x, y, angle))

            def SetFillFore(self, handle, color):
                self.calls.append(("SetFillFore", handle, color))

            def SetPenFore(self, handle, color):
                self.calls.append(("SetPenFore", handle, color))

            def ReDrawAll(self):
                self.calls.append(("ReDrawAll",))

        fake_vs = FakeVS()
        listener.vs = fake_vs

        read = listener.handle_worksheet({"action": "read", "worksheet_name": "Schedule", "row": 2, "col": 3})
        self.assertTrue(read["success"])
        self.assertEqual(read["result"]["value"], "cell text")
        self.assertIn(("GetWSCellString", "worksheet-handle", 2, 3), fake_vs.calls)

        write = listener.handle_worksheet(
            {"action": "write", "worksheet_name": "Schedule", "row": 4, "col": 5, "value": "Door A"}
        )
        self.assertTrue(write["success"])
        self.assertIn(("SetWSCellFormula", "worksheet-handle", 4, 5, 4, 5, "Door A"), fake_vs.calls)

        dxf_path = self.tmp_path() / "fixture.dxf"
        dxf_path.write_text("0\nEOF\n", encoding="utf-8")
        imported = listener.handle_import_file({"file_path": str(dxf_path), "format": "dxf"})
        self.assertTrue(imported["success"])
        self.assertIn(("ImportDXFDWGFile", str(dxf_path)), fake_vs.calls)
        self.assertNotIn(("ImportDXFDWGFile", str(dxf_path), False), fake_vs.calls)

        image_path = self.tmp_path() / "fixture.png"
        image_path.write_bytes(b"not a real png")
        image = listener.handle_import_file({"file_path": str(image_path), "format": "png"})
        self.assertTrue(image["success"])
        self.assertIn(("ImportImageFile", str(image_path), (0, 0)), fake_vs.calls)

        text = listener.handle_create_text(
            {
                "text": "Room 101",
                "x": 10,
                "y": 20,
                "width": 250,
                "text_size": 12,
                "fixed_size": True,
                "wrap": True,
                "rotation": 30,
                "name": "label",
                "class_name": "A-Annotation",
            }
        )
        self.assertTrue(text["success"])
        self.assertIn(("CreateTextBlock", "Room 101", (10.0, 20.0), True, 250.0), fake_vs.calls)
        self.assertIn(("SetTextWidth", "text-handle", 250.0), fake_vs.calls)
        self.assertIn(("SetTextWrap", "text-handle", True), fake_vs.calls)
        self.assertIn(("PagePointsToCoordLength", 12.0), fake_vs.calls)
        self.assertIn(("SetTextSize", "text-handle", 0, 8, 120.0), fake_vs.calls)
        self.assertIn(("HRotate", "text-handle", 10.0, 20.0, 30.0), fake_vs.calls)

        handle_id = listener._reg(object())
        color = listener.handle_set_property({"handle": handle_id, "property_name": "fillColor", "value": "1,2,3"})
        self.assertTrue(color["success"])
        self.assertTrue(any(call[0] == "SetFillFore" and call[2] == (1, 2, 3) for call in fake_vs.calls))

        export = listener.handle_export({"format": "pdf", "file_path": "C:\\Temp\\out.pdf"})
        self.assertTrue(export["success"])
        self.assertTrue(export["result"]["requires_user_save"])
        self.assertFalse(export["result"]["saved"])
        self.assertIn(("DoMenuTextByName", "Export PDF", 0), fake_vs.calls)

        screenshot = listener.handle_screenshot({"file_path": "C:\\Temp\\view.png"})
        self.assertTrue(screenshot["success"])
        self.assertTrue(screenshot["result"]["requires_user_save"])
        self.assertFalse(screenshot["result"]["saved"])
        self.assertIn(("DoMenuTextByName", "Export Image File", 0), fake_vs.calls)

    def test_default_autostart_mode_is_dialog(self):
        listener, _alerts = self.load_listener()
        old_mode = os.environ.get("VW_MCP_MODE", MISSING)
        old_background = os.environ.get("VW_MCP_BACKGROUND", MISSING)
        try:
            os.environ.pop("VW_MCP_MODE", None)
            os.environ.pop("VW_MCP_BACKGROUND", None)
            self.assertEqual(listener._autostart_mode(), "dialog")
        finally:
            if old_mode is MISSING:
                os.environ.pop("VW_MCP_MODE", None)
            else:
                os.environ["VW_MCP_MODE"] = old_mode
            if old_background is MISSING:
                os.environ.pop("VW_MCP_BACKGROUND", None)
            else:
                os.environ["VW_MCP_BACKGROUND"] = old_background

    def test_listener_main_serves_ping_and_graceful_stop(self):
        listener, alerts = self.load_listener()
        port = _free_port()
        listener.HOST = "127.0.0.1"
        listener.PORT = port
        listener.STOP_DIR = str(self.tmp_path())
        listener.STOP_FILE = str(Path(listener.STOP_DIR) / "STOP")
        listener.SCREENSHOT_DIR = listener.STOP_DIR
        listener.MAX_FRAME_BYTES = 1024 * 1024

        thread = threading.Thread(target=listener.main, daemon=True)
        thread.start()

        with _connect_with_retry(port) as sock:
            _write_json_frame(sock, {"id": "ping-1", "action": "ping", "params": {}})
            ping = _read_json_frame(sock)
            self.assertEqual(ping["id"], "ping-1")
            self.assertTrue(ping["success"])
            self.assertEqual(ping["result"]["version"], listener.__VERSION__)

            _write_json_frame(sock, {"id": "stop-1", "action": "stop", "params": {}})
            stop = _read_json_frame(sock)
            self.assertEqual(stop, {"id": "stop-1", "success": True, "result": "Listener stop requested"})

        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(any("STARTED" in message for message in alerts))
        self.assertTrue(any("STOPPED" in message for message in alerts))

    def test_dialog_startup_replaces_transport_only_listener(self):
        listener, _alerts = self.load_listener()
        port = _free_port()
        listener.HOST = "127.0.0.1"
        listener.PORT = port
        listener.STOP_DIR = str(self.tmp_path())
        listener.STOP_FILE = str(Path(listener.STOP_DIR) / "STOP")
        listener.SCREENSHOT_DIR = listener.STOP_DIR
        listener.MAX_FRAME_BYTES = 1024 * 1024
        listener._DISPATCH_MODE = "background"

        thread = threading.Thread(target=listener.main, kwargs={"show_alerts": False}, daemon=True)
        thread.start()

        with _connect_with_retry(port) as sock:
            _write_json_frame(sock, {"id": "ping-1", "action": "ping", "params": {}})
            ping = _read_json_frame(sock)
            self.assertTrue(ping["success"])
            self.assertTrue(ping["result"]["transport_only"])
            self.assertFalse(ping["result"]["cad_api_safe"])

        should_return_without_starting = listener._report_existing_or_stale_listener()
        thread.join(3)

        self.assertFalse(should_return_without_starting)
        self.assertFalse(thread.is_alive())

    def test_malformed_startup_ping_is_not_healthy(self):
        listener, _alerts = self.load_listener()
        port = _free_port()
        listener.HOST = "127.0.0.1"
        listener.PORT = port

        ready = threading.Event()
        stop = threading.Event()

        def serve_malformed_ping():
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
                server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                server.bind(("127.0.0.1", port))
                server.listen(1)
                server.settimeout(0.2)
                ready.set()
                while not stop.is_set():
                    try:
                        conn, _addr = server.accept()
                    except socket.timeout:
                        continue
                    with conn:
                        conn.recv(1024)
                        conn.sendall(struct.pack(">I", 0))
                    return

        thread = threading.Thread(target=serve_malformed_ping, daemon=True)
        thread.start()
        self.assertTrue(ready.wait(2))

        try:
            self.assertIsNone(listener._existing_listener_status())
            self.assertFalse(listener._existing_listener_healthy())
        finally:
            stop.set()
            thread.join(2)

    def tmp_path(self):
        base = Path(os.environ.get("TMPDIR", "/tmp"))
        path = base / "vectorworks_mcp_tests" / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
        self.addCleanup(lambda: self._remove_tmp_dir(path))
        return path

    @staticmethod
    def _remove_tmp_dir(path):
        try:
            for child in path.iterdir():
                child.unlink()
            path.rmdir()
            path.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
