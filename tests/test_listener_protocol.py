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

    def test_dispatch_handles_ping_and_unknown_action(self):
        listener, _alerts = self.load_listener()

        ping = listener.dispatch({"id": "1", "action": "ping", "params": {}})
        self.assertEqual(ping["id"], "1")
        self.assertTrue(ping["success"])
        self.assertTrue(ping["result"]["pong"])

        unknown = listener.dispatch({"id": "2", "action": "missing", "params": {}})
        self.assertEqual(unknown, {"id": "2", "success": False, "error": "Unknown action: missing"})

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
