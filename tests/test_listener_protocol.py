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
        sys.modules["vs"] = fake_vs
        os.environ["VW_MCP_NO_AUTOSTART"] = "1"

        def restore():
            if old_vs is MISSING:
                sys.modules.pop("vs", None)
            else:
                sys.modules["vs"] = old_vs
            if old_autostart is MISSING:
                os.environ.pop("VW_MCP_NO_AUTOSTART", None)
            else:
                os.environ["VW_MCP_NO_AUTOSTART"] = old_autostart

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
