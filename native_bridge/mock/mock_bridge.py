import json
import socket
import struct
import threading


IMPLEMENTED_ACTIONS = {
    "ping",
    "stop",
    "get_document_info",
    "get_layers",
    "get_objects",
    "selection",
    "create_object",
}


def _read_exact(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("connection closed")
        data.extend(chunk)
    return bytes(data)


def _read_frame(sock):
    header = _read_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    if size <= 0 or size > 16 * 1024 * 1024:
        raise ValueError("invalid frame length {0}".format(size))
    return _read_exact(sock, size)


def _write_json_frame(sock, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


class MockNativeBridge:
    """Small TCP bridge used by tests to exercise the native protocol contract."""

    def __init__(self, status=None):
        self.status = status or {
            "pong": True,
            "handlers": len(IMPLEMENTED_ACTIONS),
            "version": "mock-native-bridge",
            "bridge_kind": "native_sdk_bridge_mock",
            "dispatch_mode": "native_sdk",
            "cad_api_safe": True,
            "transport_only": False,
            "native_bridge": True,
        }
        self.requests = []
        self.ready = threading.Event()
        self.stop_event = threading.Event()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.1)
        self.port = self.sock.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, name="MockNativeBridge", daemon=True)

    def __enter__(self):
        self.thread.start()
        self.ready.wait(2)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        self.thread.join(2)

    def stop(self):
        self.stop_event.set()
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve(self):
        self.ready.set()
        while not self.stop_event.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            thread.start()

    def _handle_client(self, conn):
        with conn:
            conn.settimeout(1.0)
            while not self.stop_event.is_set():
                try:
                    request = json.loads(_read_frame(conn).decode("utf-8"))
                except (ConnectionError, TimeoutError, socket.timeout, OSError, ValueError, json.JSONDecodeError):
                    return

                self.requests.append(request)
                action = request.get("action", "")
                request_id = request.get("id", "")
                if action == "ping":
                    _write_json_frame(conn, {"id": request_id, "success": True, "result": self.status})
                elif action == "get_document_info":
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": {
                                "filename": "Mock.vwx",
                                "filepath": "",
                                "layers": ["Design Layer-1"],
                                "layer_count": 1,
                                "total_objects": 1,
                            },
                        },
                    )
                elif action == "get_layers":
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": [{"name": "Design Layer-1", "visible": True}],
                        },
                    )
                elif action == "get_objects":
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": [
                                {
                                    "handle": "mock-rect-1",
                                    "type": "rect",
                                    "name": "Mock Rect",
                                    "bounds": {
                                        "top_left": [0, 0],
                                        "bottom_right": [100, 100],
                                    },
                                }
                            ],
                        },
                    )
                elif action == "selection":
                    selection_action = request.get("params", {}).get("action", "get")
                    if selection_action in ("get", "clear"):
                        result = [] if selection_action == "get" else "Selection cleared"
                        _write_json_frame(conn, {"id": request_id, "success": True, "result": result})
                    else:
                        _write_json_frame(
                            conn,
                            {
                                "id": request_id,
                                "success": False,
                                "error": "Mock bridge only implements selection get/clear",
                            },
                        )
                elif action == "create_object":
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": "Created mock object, handle: mock-created-1",
                        },
                    )
                elif action == "stop":
                    _write_json_frame(conn, {"id": request_id, "success": True, "result": "Mock bridge stop requested"})
                    self.stop()
                    return
                else:
                    _write_json_frame(
                        conn,
                        {"id": request_id, "success": False, "error": "Mock handler not implemented: {0}".format(action)},
                    )
