import json
import socket
import struct
import threading
import unittest

import server


def _read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        assert chunk
        data.extend(chunk)
    return bytes(data)


def _read_frame(sock):
    (size,) = struct.unpack(">I", _read_exact(sock, 4))
    return _read_exact(sock, size)


def _write_frame(sock, payload):
    sock.sendall(struct.pack(">I", len(payload)) + payload)


class FakeListener:
    def __init__(self, handler):
        self.handler = handler
        self.ready = threading.Event()
        self.done = threading.Event()
        self.requests = []
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        self.ready.wait(1)
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(2)
        server._close()

    def _serve(self):
        self.ready.set()
        try:
            conn, _addr = self.sock.accept()
            with conn:
                request = json.loads(_read_frame(conn).decode("utf-8"))
                self.requests.append(request)
                response = self.handler(request)
                if response is not None:
                    if isinstance(response, bytes):
                        conn.sendall(response)
                    else:
                        _write_frame(conn, json.dumps(response).encode("utf-8"))
        finally:
            self.done.set()


def _configure_server(port, max_frame_bytes=1024 * 1024):
    server._close()
    server.HOST = "127.0.0.1"
    server.PORT = port
    server.TIMEOUT = 1
    server.MAX_FRAME_BYTES = max_frame_bytes
    server._CONFIG_ERROR = None


class ServerProtocolTests(unittest.TestCase):
    def tearDown(self):
        server._close()

    def test_send_success_round_trips_length_prefixed_json(self):
        def handler(request):
            return {"id": request["id"], "success": True, "result": {"pong": True}}

        with FakeListener(handler) as listener:
            _configure_server(listener.port)
            result = server._send("ping", {"probe": True})

        self.assertEqual(json.loads(result), {"pong": True})
        self.assertEqual(listener.requests[0]["action"], "ping")
        self.assertEqual(listener.requests[0]["params"], {"probe": True})

    def test_send_formats_listener_errors(self):
        def handler(request):
            return {"id": request["id"], "success": False, "error": "bad wall height"}

        with FakeListener(handler) as listener:
            _configure_server(listener.port)
            result = server._send("create_wall", {"height": -1})

        self.assertEqual(result, "VW Error (create_wall): bad wall height")

    def test_send_reports_malformed_listener_json(self):
        bad_payload = b"not json"
        bad_frame = struct.pack(">I", len(bad_payload)) + bad_payload

        with FakeListener(lambda _request: bad_frame) as listener:
            _configure_server(listener.port)
            result = server._send("ping")

        self.assertTrue(result.startswith("Protocol error: listener returned malformed JSON"))

    def test_send_rejects_oversized_listener_frame(self):
        oversized_header = struct.pack(">I", 2048)

        with FakeListener(lambda _request: oversized_header) as listener:
            _configure_server(listener.port, max_frame_bytes=1024)
            result = server._send("ping")

        self.assertIn("Protocol error: listener frame is 2048 bytes", result)

    def test_send_reports_connection_help_when_listener_is_missing(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        _configure_server(port)
        result = server._send("ping")

        self.assertTrue(result.startswith("Connection error:"))
        self.assertIn(f"127.0.0.1:{port}", result)
        self.assertIn("run vw_listener.py", result)

    def test_server_info_does_not_connect_to_vectorworks(self):
        _configure_server(1)
        result = json.loads(server.vw_server_info())

        self.assertEqual(result["server"], "Vectorworks 2024/2025 MCP Server")
        self.assertEqual(result["version"], server.SERVER_VERSION)
        self.assertTrue(result["ready"])
        self.assertEqual(result["transport"], "stdio MCP via FastMCP")
        self.assertEqual(result["vectorworks_listener"]["host"], "127.0.0.1")
        self.assertEqual(result["vectorworks_listener"]["port"], 1)
        self.assertTrue(result["vectorworks_listener"]["requires_connection_for_vectorworks_tools"])


if __name__ == "__main__":
    unittest.main()
