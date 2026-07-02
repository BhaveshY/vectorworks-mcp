import json
import inspect
import socket
import struct
import threading
import time
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
    def __init__(self, handler, max_requests=1):
        self.handler = handler
        self.max_requests = max_requests
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
            self.sock.settimeout(1)
            while len(self.requests) < self.max_requests:
                try:
                    conn, _addr = self.sock.accept()
                except (OSError, TimeoutError, socket.timeout):
                    break
                with conn:
                    conn.settimeout(1)
                    while len(self.requests) < self.max_requests:
                        try:
                            request = json.loads(_read_frame(conn).decode("utf-8"))
                        except (AssertionError, ConnectionError, OSError, socket.timeout):
                            break
                        self.requests.append(request)
                        response = self.handler(request)
                        if response is None:
                            break
                        if isinstance(response, bytes):
                            conn.sendall(response)
                        else:
                            _write_frame(conn, json.dumps(response).encode("utf-8"))
        finally:
            self.done.set()


class ConcurrentFakeListener(FakeListener):
    def __init__(self, handler, max_requests=2):
        super().__init__(handler, max_requests=max_requests)
        self.client_threads = []
        self.sock.listen(4)

    def __exit__(self, exc_type, exc, tb):
        try:
            self.sock.close()
        except OSError:
            pass
        self.thread.join(2)
        for thread in self.client_threads:
            thread.join(2)
        server._close()

    def _serve(self):
        self.ready.set()
        try:
            self.sock.settimeout(1)
            while len(self.requests) < self.max_requests:
                try:
                    conn, _addr = self.sock.accept()
                except (OSError, TimeoutError, socket.timeout):
                    break
                thread = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
                self.client_threads.append(thread)
                thread.start()
        finally:
            self.done.set()

    def _handle_client(self, conn):
        with conn:
            conn.settimeout(1)
            while len(self.requests) < self.max_requests:
                try:
                    request = json.loads(_read_frame(conn).decode("utf-8"))
                except (AssertionError, ConnectionError, OSError, socket.timeout):
                    break
                self.requests.append(request)
                response = self.handler(request)
                if response is None:
                    break
                if isinstance(response, bytes):
                    conn.sendall(response)
                else:
                    _write_frame(conn, json.dumps(response).encode("utf-8"))


def _configure_server(port, max_frame_bytes=1024 * 1024):
    server._close()
    server._clear_cad_safe_cache()
    server.HOST = "127.0.0.1"
    server.PORT = port
    server.TIMEOUT = 1
    server.HEALTH_TIMEOUT = 0.25
    server.MAX_FRAME_BYTES = max_frame_bytes
    server.PREFLIGHT_CACHE_SECONDS = 0.75
    server.AUTH_TOKEN = "test-token"
    server.ALLOW_INSECURE_NO_AUTH = False
    server._CONFIG_ERROR = None


def _native_phase_one_status():
    return {
        "pong": True,
        "cad_api_safe": True,
        "transport_only": False,
        "native_bridge": True,
        "native_phase": 1,
        "implemented_actions": sorted(server.NATIVE_PHASE_ONE_REQUIRED_ACTIONS),
        "bridge_kind": "native_sdk_bridge_phase1",
        "dispatch_mode": "native_sdk",
        "handlers": 8,
        "version": "native-sdk-bridge-phase1",
        "main_context_pump": "win32_ui_timer",
        "main_context_pump_ready": True,
    }


def _native_phase_two_status():
    return {
        "pong": True,
        "cad_api_safe": True,
        "transport_only": False,
        "native_bridge": True,
        "native_phase": 2,
        "implemented_actions": sorted(server.NATIVE_PHASE_TWO_REQUIRED_ACTIONS),
        "bridge_kind": "native_sdk_bridge_phase2",
        "dispatch_mode": "native_sdk",
        "handlers": 13,
        "version": "native-sdk-bridge-phase2",
        "main_context_pump": "win32_ui_timer",
        "main_context_pump_ready": True,
    }


def _native_phase_two_with_set_property_status():
    status = _native_phase_two_status()
    return status


def _native_phase_two_without_set_property_status():
    status = _native_phase_two_status()
    status["implemented_actions"] = sorted(set(status["implemented_actions"]) - {"set_property"})
    status["handlers"] = status["handlers"] - 1
    return status


def _native_phase_three_status():
    status = _native_phase_two_status()
    status["native_phase"] = 3
    status["version"] = "native-sdk-bridge-phase3"
    status["bridge_kind"] = "native_sdk_bridge_phase3"
    status["implemented_actions"] = sorted(set(status["implemented_actions"]) | {"find_objects", "drawing_summary"})
    status["handlers"] = 15
    return status


def _python_dialog_status():
    return {
        "pong": True,
        "cad_api_safe": True,
        "transport_only": False,
        "native_bridge": False,
        "bridge_kind": "python_dialog_agent_session",
        "dispatch_mode": "dialog",
        "mode": "dialog",
        "version": "python-dialog",
    }


class ServerProtocolTests(unittest.TestCase):
    def tearDown(self):
        server._close()

    def test_main_forces_stdio_transport(self):
        class FakeMCP:
            def __init__(self):
                self.calls = []

            def run(self, **kwargs):
                self.calls.append(kwargs)

        fake_mcp = FakeMCP()
        original_mcp = server.mcp
        original_config_error = server._CONFIG_ERROR
        try:
            server.mcp = fake_mcp
            server._CONFIG_ERROR = None

            self.assertEqual(server.main(), 0)
        finally:
            server.mcp = original_mcp
            server._CONFIG_ERROR = original_config_error

        self.assertEqual(fake_mcp.calls, [{"transport": "stdio", "show_banner": False}])

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

    def test_send_reports_pre_send_frame_errors_without_unknown_commit_state(self):
        with FakeListener(lambda _request: self.fail("oversized request should not reach listener")) as listener:
            _configure_server(listener.port, max_frame_bytes=64)
            result = server._send("create_object", {"payload": "x" * 1024})
            listener.done.wait(1)

        self.assertEqual(listener.requests, [])
        self.assertIn("Request was not sent to Vectorworks for action 'create_object'", result)
        self.assertIn("No CAD changes were started", result)
        self.assertNotIn("Unknown commit state", result)

    def test_send_rejects_missing_or_mismatched_response_id(self):
        for response in (
            {"success": True, "result": "pong"},
            {"id": "", "success": True, "result": "pong"},
            {"id": "wrong", "success": True, "result": "pong"},
        ):
            with self.subTest(response=response):
                with FakeListener(lambda _request, response=response: response) as listener:
                    _configure_server(listener.port)
                    result = server._send("ping")

                self.assertIn("Protocol error: response id mismatch for ping", result)

    def test_send_rejects_non_boolean_success_envelope(self):
        def handler(request):
            return {"id": request["id"], "success": "true", "result": "pong"}

        with FakeListener(handler) as listener:
            _configure_server(listener.port)
            result = server._send("ping")

        self.assertIn("Protocol error: listener response success for ping was not boolean true/false", result)

    def test_send_rejects_success_without_result(self):
        def handler(request):
            return {"id": request["id"], "success": True}

        with FakeListener(handler) as listener:
            _configure_server(listener.port)
            result = server._send("ping")

        self.assertIn("Protocol error: listener success response for ping did not include result", result)

    def test_send_rejects_failure_without_error(self):
        for response in (
            {"success": False},
            {"success": False, "error": ""},
            {"success": False, "error": 42},
        ):
            with self.subTest(response=response):
                def handler(request, response=response):
                    payload = {"id": request["id"]}
                    payload.update(response)
                    return payload

                with FakeListener(handler) as listener:
                    _configure_server(listener.port)
                    result = server._send("ping")

                self.assertIn(
                    "Protocol error: listener failure response for ping did not include a non-empty error string",
                    result,
                )

    def test_send_reports_connection_help_when_listener_is_missing(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()

        _configure_server(port)
        result = server._send("ping")

        self.assertTrue(result.startswith("Connection error:"))
        self.assertIn(f"127.0.0.1:{port}", result)
        self.assertIn("run the generated vw_load_listener_2024.py", result)
        self.assertIn("scripts\\test-vectorworks-listener.ps1", result)
        self.assertIn("C:\\Users\\<you>\\.vectorworks-mcp\\STOP", result)

    def test_bridge_status_tool_uses_ping_action(self):
        calls = []
        original_send = server._send_health
        try:
            server._send_health = (
                lambda action, params=None, require_cad_safe=False:
                calls.append((action, params, require_cad_safe)) or '{"pong": true}'
            )
            self.assertEqual(server.vw_bridge_status(), '{"pong": true}')
        finally:
            server._send_health = original_send

        self.assertEqual(calls, [("ping", None, False)])

    def test_config_rejects_non_loopback_hosts(self):
        self.assertEqual(server._validate_loopback_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(server._validate_loopback_host("::1"), "::1")
        self.assertEqual(server._validate_loopback_host("localhost"), "localhost")

        with self.assertRaises(server.ConfigError):
            server._validate_loopback_host("0.0.0.0")
        with self.assertRaises(server.ConfigError):
            server._validate_loopback_host("192.168.1.20")

    def test_health_ping_uses_dedicated_connection_while_cad_call_is_blocked(self):
        cad_started = threading.Event()
        release_cad = threading.Event()
        status = {
            "pong": True,
            "version": "fake",
            "bridge_kind": "python_dialog_agent_session",
            "dispatch_mode": "dialog",
            "handlers": 1,
            "cad_api_safe": True,
            "transport_only": False,
        }

        def handler(request):
            if request["action"] == "get_layers":
                cad_started.set()
                release_cad.wait(1)
                return {"id": request["id"], "success": True, "result": []}
            return {"id": request["id"], "success": True, "result": status}

        with ConcurrentFakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            worker = threading.Thread(target=lambda: server._send("get_layers"), daemon=True)
            worker.start()
            self.assertTrue(cad_started.wait(1))

            started = time.perf_counter()
            result = server.vw_ping()
            elapsed = time.perf_counter() - started

            release_cad.set()
            worker.join(2)

        self.assertLess(elapsed, 0.2)
        self.assertEqual(json.loads(result)["pong"], True)
        self.assertEqual([request["action"] for request in listener.requests], ["get_layers", "ping"])

    def test_health_ping_uses_health_timeout_without_retry_loop(self):
        def handler(_request):
            time.sleep(0.6)
            return None

        with ConcurrentFakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            server.TIMEOUT = 1.5
            server.HEALTH_TIMEOUT = 0.1

            started = time.perf_counter()
            result = server.vw_ping()
            elapsed = time.perf_counter() - started

        self.assertLess(elapsed, 0.5)
        self.assertTrue(result.startswith("Connection error:"))

    def test_send_tool_blocks_transport_only_cad_handler_before_action(self):
        def handler(request):
            self.assertEqual(request["action"], "ping")
            return {
                "id": request["id"],
                "success": True,
                "result": {
                    "pong": True,
                    "cad_api_safe": False,
                    "transport_only": True,
                    "bridge_kind": "python_transport_only",
                    "dispatch_mode": "win_timer",
                },
            }

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = server.vw_get_layers()

        blocked = json.loads(result)
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["blocked_action"], "get_layers")
        self.assertEqual(blocked["reason"], "transport_only_bridge")
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_send_tool_allows_cad_handler_after_safe_preflight(self):
        def handler(request):
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "bridge_kind": "native_sdk_bridge",
                        "dispatch_mode": "native_sdk",
                    },
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1"}]}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = server.vw_get_layers()

        self.assertEqual(json.loads(result), [{"name": "Layer 1"}])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_layers"])

    def test_send_tool_blocks_unimplemented_native_action_before_dispatch(self):
        def handler(request):
            self.assertEqual(request["action"], "ping")
            return {
                "id": request["id"],
                "success": True,
                "result": {
                    "pong": True,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "native_bridge": True,
                    "native_phase": 1,
                    "implemented_actions": sorted(server.NATIVE_PHASE_ONE_REQUIRED_ACTIONS),
                    "bridge_kind": "native_sdk_bridge_phase1",
                    "dispatch_mode": "native_sdk",
                    "handlers": 8,
                    "version": "native-sdk-bridge-phase1",
                    "main_context_pump": "win32_ui_timer",
                    "main_context_pump_ready": True,
                },
            }

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = server.vw_inspect_object(handle="0x1")

        blocked = json.loads(result)
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["blocked_action"], "inspect_object")
        self.assertEqual(blocked["reason"], "native_bridge_action_not_implemented")
        self.assertIn("action is not implemented by native bridge: inspect_object", blocked["native_readiness_errors"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_send_tool_blocks_unimplemented_native_create_variant_before_dispatch(self):
        def handler(request):
            self.assertEqual(request["action"], "ping")
            return {
                "id": request["id"],
                "success": True,
                "result": {
                    "pong": True,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "native_bridge": True,
                    "native_phase": 1,
                    "implemented_actions": sorted(server.NATIVE_PHASE_ONE_REQUIRED_ACTIONS),
                    "bridge_kind": "native_sdk_bridge_phase1",
                    "dispatch_mode": "native_sdk",
                    "handlers": 8,
                    "version": "native-sdk-bridge-phase1",
                    "main_context_pump": "win32_ui_timer",
                    "main_context_pump_ready": True,
                },
            }

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = server.vw_create_object("polygon", points=[[0, 0], [10, 0], [10, 10]])

        blocked = json.loads(result)
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "native_bridge_action_not_implemented")
        self.assertIn("create_object object_type is not implemented by native bridge: polygon", blocked["native_readiness_errors"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_send_tool_blocks_unimplemented_native_selection_variant_from_cache(self):
        def handler(request):
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "native_bridge": True,
                        "native_phase": 1,
                        "implemented_actions": sorted(server.NATIVE_PHASE_ONE_REQUIRED_ACTIONS),
                        "bridge_kind": "native_sdk_bridge_phase1",
                        "dispatch_mode": "native_sdk",
                        "handlers": 8,
                        "version": "native-sdk-bridge-phase1",
                        "main_context_pump": "win32_ui_timer",
                        "main_context_pump_ready": True,
                    },
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1"}]}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            layers = server.vw_get_layers()
            result = server.vw_selection("move", "1,1")

        blocked = json.loads(result)
        self.assertEqual(json.loads(layers), [{"name": "Layer 1"}])
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["blocked_action"], "selection")
        self.assertEqual(blocked["reason"], "native_bridge_action_not_implemented")
        self.assertIn("selection action is not implemented by native bridge: move", blocked["native_readiness_errors"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_layers"])

    def test_capabilities_reports_native_phase_one_surface(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_capabilities(include_tools=False))

        self.assertTrue(result["ok"])
        self.assertIn("rect", result["native_phase_one_create_object_types"])
        self.assertIn("selection", result["native_phase_one_required_actions"])
        self.assertFalse(result["host_capabilities"]["true_bim_objects"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_capabilities_reports_native_phase_two_production_surface(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_capabilities(include_tools=False))

        self.assertTrue(result["ok"])
        self.assertIn("wall", result["native_phase_two_create_object_types"])
        self.assertTrue(result["host_capabilities"]["true_bim_objects"])
        self.assertTrue(result["host_capabilities"]["native_text_creation"])
        self.assertTrue(result["host_capabilities"]["native_linear_dimension_creation"])
        self.assertTrue(result["host_capabilities"]["native_class_management"])

    def test_capabilities_do_not_advertise_native_writes_when_bridge_is_not_cad_safe(self):
        unsafe_status = _native_phase_two_status()
        unsafe_status["cad_api_safe"] = False
        unsafe_status["transport_only"] = True
        unsafe_status["main_context_pump_ready"] = False

        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": unsafe_status}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_capabilities(include_tools=False))

        self.assertTrue(result["ok"])
        self.assertFalse(result["host_capabilities"]["atomic_mixed_production_batch_creation"])
        self.assertFalse(result["host_capabilities"]["native_wall_creation"])
        self.assertFalse(result["host_capabilities"]["native_text_creation"])
        self.assertFalse(result["host_capabilities"]["native_linear_dimension_creation"])
        self.assertFalse(result["host_capabilities"]["native_class_management"])
        self.assertFalse(result["host_capabilities"]["true_bim_objects"])

    def test_agent_context_returns_compact_preflight_capabilities_and_summary(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] == "get_document_info":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {"filename": "Demo.vwx", "layers": ["Layer 1"], "layer_count": 1, "total_objects": 2},
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1", "visible": True}]}
            if request["action"] == "get_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [
                        {"handle": "h1", "type": "wall", "name": "Wall", "layer": "Layer 1"},
                        {"handle": "h2", "type": "text", "name": "Label", "layer": "Layer 1"},
                    ],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_agent_context())

        self.assertTrue(result["ok"])
        self.assertEqual(result["profile"], "production")
        self.assertTrue(result["preflight"]["ok"])
        self.assertTrue(result["host_capabilities"]["atomic_mixed_production_batch_creation"])
        self.assertTrue(result["host_capabilities"]["native_class_management"])
        self.assertEqual(result["drawing_summary"]["counts_by_type"], {"text": 1, "wall": 1})
        self.assertNotIn("examples", result["drawing_summary"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_document_info", "get_layers", "get_objects"])

    def test_agent_context_does_not_read_drawing_when_preflight_blocks(self):
        unsafe_status = _native_phase_two_status()
        unsafe_status["cad_api_safe"] = False
        unsafe_status["transport_only"] = True

        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": unsafe_status}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_agent_context())

        self.assertFalse(result["ok"])
        self.assertFalse(result["preflight"]["ok"])
        self.assertFalse(result["host_capabilities"]["drawing_summary"])
        self.assertIsNone(result["drawing_summary"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_phase_two_direct_text_and_dimension_actions(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] == "create_text":
                return {"id": request["id"], "success": True, "result": {"type": "text", "handle": "text-1"}}
            if request["action"] == "create_linear_dimension":
                return {"id": request["id"], "success": True, "result": {"type": "linear_dimension", "handle": "dim-1"}}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=3) as listener:
            _configure_server(listener.port)
            text_result = json.loads(server.vw_create_text("Office", x=10, y=20, text_size=12))
            dim_result = json.loads(server.vw_create_linear_dimension(0, 0, 4000, 0, offset=500))

        self.assertEqual(text_result["type"], "text")
        self.assertEqual(dim_result["type"], "linear_dimension")
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "create_text", "create_linear_dimension"])
        self.assertEqual(listener.requests[1]["params"]["text"], "Office")
        self.assertEqual(listener.requests[2]["params"]["end_x"], 4000)

    def test_batch_create_objects_accepts_phase_two_mixed_production_objects(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] == "batch_create_objects":
                object_count = request["params"]["object_count"]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": object_count,
                        "created": [
                            {"index": index, "type": json.loads(request["params"][f"object_{index}_json"])["object_type"], "handle": f"h-{index}"}
                            for index in range(1, object_count + 1)
                        ],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        objects = [
            {"object_type": "wall", "start_x": 0, "start_y": 0, "end_x": 4000, "end_y": 0, "height": 3000, "thickness": 200},
            {"object_type": "text", "text": "Office", "x": 2000, "y": 1500, "text_size": 10},
            {"object_type": "linear_dimension", "start_x": 0, "start_y": 0, "end_x": 4000, "end_y": 0, "offset": 500},
        ]
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_batch_create_objects(objects, atomic=True))

        self.assertTrue(result["ok"])
        self.assertEqual(result["created_count"], 3)
        sent = [json.loads(listener.requests[1]["params"][f"object_{index}_json"]) for index in range(1, 4)]
        self.assertEqual([item["object_type"] for item in sent], ["wall", "text", "linear_dimension"])

    def test_batch_create_objects_preserves_non_ascii_for_native_params(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] == "batch_create_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": 1,
                        "created": [{"index": 1, "type": "text", "handle": "h-1"}],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_batch_create_objects(
                    [{"object_type": "text", "text": "Café", "x": 0, "y": 0}],
                    atomic=True,
                )
            )

        self.assertTrue(result["ok"])
        encoded = listener.requests[1]["params"]["object_1_json"]
        self.assertIn("Café", encoded)
        self.assertNotIn("\\u00e9", encoded)

    def test_phase_one_batch_rejects_production_object_types_before_write(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_batch_create_objects(
                    [{"object_type": "wall", "start_x": 0, "start_y": 0, "end_x": 1000, "end_y": 0}]
                )
            )

        self.assertFalse(result["ok"])
        self.assertIn("wall", result["unsupported_object_types"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_batch_create_objects_composes_native_primitives(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "batch_create_objects":
                object_count = request["params"]["object_count"]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": object_count,
                        "created": [
                            {"index": index, "type": json.loads(request["params"][f"object_{index}_json"])["object_type"], "handle": f"h-{index}"}
                            for index in range(1, object_count + 1)
                        ],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        objects = [
            {"object_type": "rectangle", "x1": 0, "y1": 0, "x2": 100, "y2": 50, "name": "desk"},
            {"object_type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0, "role": "axis"},
        ]
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_batch_create_objects(objects, default_class_name="A-Test", name_prefix="Batch")
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["atomic"])
        self.assertTrue(result["native_batch"])
        self.assertEqual(result["created_count"], 2)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "batch_create_objects"])
        batch_params = listener.requests[1]["params"]
        self.assertEqual(batch_params["object_count"], 2)
        created_params = [json.loads(batch_params[f"object_{index}_json"]) for index in (1, 2)]
        self.assertEqual(created_params[0]["object_type"], "rect")
        self.assertEqual(created_params[0]["name"], "Batch desk")
        self.assertEqual(created_params[0]["class_name"], "A-Test")
        self.assertNotIn("role", created_params[1])
        self.assertEqual(result["created"][1]["role"], "axis")

    def test_batch_create_objects_rejects_bad_payload_without_connecting(self):
        result = json.loads(server.vw_batch_create_objects([{"object_type": "line", "x1": 1, "y1": 1, "x2": 1, "y2": 1}]))

        self.assertFalse(result["ok"])
        self.assertIn("endpoints", result["error"])

    def test_batch_create_objects_can_use_legacy_non_atomic_fallback(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "create_object":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {"type": request["params"]["object_type"], "handle": "h-{0}".format(len(listener.requests))},
                }
            self.fail(f"Unexpected action: {request['action']}")

        objects = [
            {"object_type": "rect", "x1": 0, "y1": 0, "x2": 100, "y2": 50},
            {"object_type": "line", "x1": 0, "y1": 0, "x2": 100, "y2": 0},
        ]
        with FakeListener(handler, max_requests=3) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_batch_create_objects(objects, atomic=False))

        self.assertTrue(result["ok"])
        self.assertFalse(result["atomic"])
        self.assertEqual(result["created_count"], 2)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "create_object", "create_object"])

    def test_batch_create_objects_non_atomic_routes_phase_two_types_to_typed_actions(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] in {"create_wall", "create_text", "create_linear_dimension"}:
                object_type = {
                    "create_wall": "wall",
                    "create_text": "text",
                    "create_linear_dimension": "linear_dimension",
                }[request["action"]]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {"type": object_type, "handle": "h-{0}".format(len(listener.requests))},
                }
            self.fail(f"Unexpected action: {request['action']}")

        objects = [
            {"object_type": "wall", "start_x": 0, "start_y": 0, "end_x": 4000, "end_y": 0},
            {"object_type": "text", "text": "Office", "x": 0, "y": 0},
            {"object_type": "linear_dimension", "start_x": 0, "start_y": 0, "end_x": 4000, "end_y": 0},
        ]
        with FakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_batch_create_objects(objects, atomic=False))

        self.assertTrue(result["ok"])
        self.assertEqual(result["created_count"], 3)
        self.assertEqual(
            [request["action"] for request in listener.requests],
            ["ping", "create_wall", "create_text", "create_linear_dimension"],
        )

    def test_plan_schematic_floor_plan_is_read_only(self):
        result = json.loads(
            server.vw_plan_schematic_floor_plan(
                rooms=[{"name": "Office", "x": 0, "y": 0, "width": 4000, "depth": 3000}],
                walls=[{"name": "Partition", "x1": 4000, "y1": 0, "x2": 4000, "y2": 3000}],
                doors=[{"name": "D1", "hinge_x": 1000, "hinge_y": 0, "width": 900, "rotation": 0}],
                windows=[{"name": "W1", "x1": 2000, "y1": 3000, "x2": 3000, "y2": 3000}],
                name="Suite",
            )
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["schematic"])
        self.assertFalse(result["bim_objects"])
        self.assertEqual(result["primitive_count"], 9)
        self.assertEqual(result["rooms_count"], 1)
        self.assertEqual(result["doors_count"], 1)
        self.assertEqual(result["windows_count"], 1)
        self.assertEqual(result["primitives"][0]["name"], "Suite Office south wall")

    def test_create_schematic_floor_plan_composes_generated_primitives(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "batch_create_objects":
                object_count = request["params"]["object_count"]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": object_count,
                        "created": [
                            {"index": index, "type": json.loads(request["params"][f"object_{index}_json"])["object_type"], "handle": f"h-{index}"}
                            for index in range(1, object_count + 1)
                        ],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_create_schematic_floor_plan(
                    rooms=[{"name": "Office", "x": 0, "y": 0, "width": 4000, "depth": 3000}],
                    doors=[{"name": "D1", "hinge_x": 1000, "hinge_y": 0}],
                    windows=[{"name": "W1", "x1": 2000, "y1": 3000, "x2": 3000, "y2": 3000}],
                    name="Suite",
                )
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["atomic"])
        self.assertEqual(result["created_count"], 8)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "batch_create_objects"])
        created_params = [
            json.loads(listener.requests[1]["params"][f"object_{index}_json"])
            for index in range(1, 9)
        ]
        self.assertEqual([params["object_type"] for params in created_params], ["rect"] * 4 + ["line", "arc", "line", "line"])

    def test_create_bim_floor_plan_composes_native_walls_labels_and_dimensions(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] == "batch_create_objects":
                object_count = request["params"]["object_count"]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": object_count,
                        "created": [
                            {"index": index, "type": json.loads(request["params"][f"object_{index}_json"])["object_type"], "handle": f"h-{index}"}
                            for index in range(1, object_count + 1)
                        ],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_create_bim_floor_plan(
                    rooms=[{"name": "Office", "x": 0, "y": 0, "width": 4000, "depth": 3000}],
                    name="Suite",
                )
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["bim_objects"])
        self.assertEqual(result["created_count"], 7)
        created_params = [
            json.loads(listener.requests[1]["params"][f"object_{index}_json"])
            for index in range(1, 8)
        ]
        self.assertEqual([params["object_type"] for params in created_params], ["wall", "wall", "wall", "wall", "text", "linear_dimension", "linear_dimension"])
        self.assertEqual(created_params[0]["name"], "Suite Office south wall")
        self.assertEqual(created_params[4]["text"], "Suite Office")

    def test_create_bim_floor_plan_accepts_wall_only_layout(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] == "batch_create_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": 1,
                        "created": [{"index": 1, "type": "wall", "handle": "h-1"}],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_create_bim_floor_plan(
                    walls=[{"name": "Partition", "x1": 0, "y1": 0, "x2": 4000, "y2": 0}],
                    name="Suite",
                    label_rooms=False,
                    dimension_rooms=False,
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["rooms_count"], 0)
        self.assertEqual(result["wall_segments_count"], 1)
        created_params = json.loads(listener.requests[1]["params"]["object_1_json"])
        self.assertEqual(created_params["object_type"], "wall")
        self.assertEqual(created_params["name"], "Suite Partition")

    def test_drawing_summary_composes_bounded_read_inventory(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "get_document_info":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {"filename": "Demo.vwx", "layers": ["Layer 1"], "layer_count": 1, "total_objects": 2},
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1", "visible": True}]}
            if request["action"] == "get_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [
                        {
                            "handle": "h1",
                            "type": "rect",
                            "name": "Room",
                            "layer": "Layer 1",
                            "bounds": {"top_left": [0, 100], "bottom_right": [100, 0]},
                        },
                        {
                            "handle": "h2",
                            "type": "line",
                            "name": "",
                            "layer": "Layer 1",
                            "bounds": {"top_left": [50, -10], "bottom_right": [200, 10]},
                        },
                    ],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_drawing_summary(limit=100))

        self.assertTrue(result["ok"])
        self.assertEqual(result["objects_returned"], 2)
        self.assertEqual(result["counts_by_type"], {"line": 1, "rect": 1})
        self.assertEqual(result["counts_by_layer_type"], {"Layer 1": {"line": 1, "rect": 1}})
        self.assertEqual(result["bounds"], {"left": 0.0, "top": -10.0, "right": 200.0, "bottom": 100.0})
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_document_info", "get_layers", "get_objects"])

    def test_drawing_summary_prefers_native_compact_summary_when_advertised(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_three_status()}
            if request["action"] == "drawing_summary":
                self.assertEqual(
                    request["params"],
                    {
                        "layer": "Layer 1",
                        "object_type": "wall",
                        "limit": 1000,
                        "include_examples": False,
                        "example_limit": 0,
                        "scan_limit": 50000,
                    },
                )
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "ok": True,
                        "tool": "vw_drawing_summary",
                        "native_summary": True,
                        "objects_scanned": 2500,
                        "counts_by_type": {"wall": 2500},
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_drawing_summary(layer="Layer 1", object_type="wall", include_examples=False, example_limit=0))

        self.assertTrue(result["ok"])
        self.assertTrue(result["native_summary"])
        self.assertEqual(result["objects_scanned"], 2500)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "drawing_summary"])

    def test_find_objects_uses_get_objects_for_exact_name_on_native_bridge(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "get_objects":
                self.assertEqual(request["params"], {"layer": "", "object_type": "", "limit": 10})
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [
                        {"handle": "h1", "type": "rect", "name": "Target", "class": "A-Test"},
                        {"handle": "h2", "type": "line", "name": "Other", "class": "A-Test"},
                    ],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_find_objects("((N='Target'))", limit=10))

        self.assertTrue(result["ok"])
        self.assertEqual(result["fallback_action"], "get_objects")
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["objects"][0]["handle"], "h1")
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_objects"])

    def test_find_objects_uses_native_criteria_for_exact_name_when_advertised(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_three_status()}
            if request["action"] == "find_objects":
                self.assertEqual(request["params"], {"criteria": "((N='Target'))", "limit": 10})
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [{"handle": "h-2501", "type": "rect", "name": "Target"}],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_find_objects("((N='Target'))", limit=10))

        self.assertEqual(result[0]["handle"], "h-2501")
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "find_objects"])

    def test_lookup_objects_returns_compact_refs_from_get_objects(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "get_objects":
                self.assertEqual(request["params"], {"layer": "", "object_type": "wall", "limit": 10})
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [
                        {
                            "handle": "h1",
                            "uuid": "u1",
                            "type": "wall",
                            "name": "Target Wall",
                            "layer": "Layer 1",
                            "bounds": {"top_left": [0, 100], "bottom_right": [100, 0]},
                        },
                        {"handle": "h2", "uuid": "u2", "type": "wall", "name": "Other", "layer": "Layer 1"},
                    ],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_lookup_objects(criteria="T=WALL", name="Target Wall", limit=10))

        self.assertTrue(result["ok"])
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["objects"][0]["ref"], "uuid:u1")
        self.assertEqual(result["objects"][0]["refs"], ["uuid:u1", "name:Target Wall", "handle:h1"])
        self.assertEqual(result["objects"][0]["type"], "wall")
        self.assertNotIn("bounds", result["objects"][0])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_objects"])

    def test_lookup_objects_supports_field_limited_normal_detail(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "get_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [
                        {"handle": "h1", "type": "rect", "name": "Room", "layer": "Layer 1", "bounds": {"top_left": [0, 1], "bottom_right": [1, 0]}},
                    ],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_lookup_objects(
                    criteria="ALL",
                    limit=5,
                    detail="normal",
                    fields=["handle", "bounds"],
                    include_refs=False,
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["objects"], [{"handle": "h1", "bounds": {"top_left": [0, 1], "bottom_right": [1, 0]}}])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_objects"])

    def test_lookup_objects_rejects_complex_criteria_before_connecting(self):
        result = json.loads(server.vw_lookup_objects("((T=RECT) & (C='A-Test'))"))

        self.assertFalse(result["ok"])
        self.assertIn("use vw_find_objects", result["error"])

    def test_batch_set_object_properties_resolves_writes_and_verifies(self):
        get_objects_calls = 0

        def handler(request):
            nonlocal get_objects_calls
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_with_set_property_status()}
            if request["action"] == "get_objects":
                get_objects_calls += 1
                self.assertEqual(request["params"], {"layer": "Layer 1", "object_type": "rect", "limit": 1000})
                objects = (
                    [{"handle": "h1", "uuid": "u1", "type": "rect", "name": "Old", "layer": "Layer 1", "class": "None"}]
                    if get_objects_calls == 1
                    else [{"handle": "h1", "uuid": "u1", "type": "rect", "name": "New", "layer": "Layer 1", "class": "A-Casework"}]
                )
                return {"id": request["id"], "success": True, "result": objects}
            if request["action"] == "set_property":
                self.assertEqual(request["params"]["handle"], "h1")
                self.assertIn(request["params"], [
                    {"handle": "h1", "property_name": "name", "value": "New"},
                    {"handle": "h1", "property_name": "class", "value": "A-Casework"},
                ])
                return {"id": request["id"], "success": True, "result": {"changed": True}}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=5) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_batch_set_object_properties(
                    edits=[
                        {
                            "ref": "uuid:u1",
                            "expected_type": "rect",
                            "expected_layer": "Layer 1",
                            "properties": {"name": "New", "class": "A-Casework"},
                        }
                    ]
                )
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["write_attempts"], 2)
        self.assertEqual(result["edits"][0]["target_ref"], "uuid:u1")
        self.assertTrue(result["edits"][0]["verified"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_objects", "set_property", "set_property", "get_objects"])

    def test_set_object_property_resolves_handle_before_write(self):
        get_objects_calls = 0

        def handler(request):
            nonlocal get_objects_calls
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_with_set_property_status()}
            if request["action"] == "get_objects":
                get_objects_calls += 1
                expected_params = (
                    {"layer": "", "object_type": "", "limit": 1000}
                    if get_objects_calls == 1
                    else {"layer": "Layer 1", "object_type": "rect", "limit": 1000}
                )
                self.assertEqual(request["params"], expected_params)
                objects = (
                    [{"handle": "h1", "uuid": "u1", "type": "rect", "name": "Old", "layer": "Layer 1"}]
                    if get_objects_calls == 1
                    else [{"handle": "h1", "uuid": "u1", "type": "rect", "name": "New", "layer": "Layer 1"}]
                )
                return {"id": request["id"], "success": True, "result": objects}
            if request["action"] == "set_property":
                self.assertEqual(request["params"], {"handle": "h1", "property_name": "name", "value": "New"})
                return {"id": request["id"], "success": True, "result": {"changed": True}}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_set_object_property("h1", "name", "New"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["edits"][0]["verified"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_objects", "set_property", "get_objects"])

    def test_batch_set_object_properties_rejects_ambiguous_name_before_write(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_with_set_property_status()}
            if request["action"] == "get_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [
                        {"handle": "h1", "uuid": "u1", "type": "rect", "name": "Door", "layer": "Layer 1"},
                        {"handle": "h2", "uuid": "u2", "type": "rect", "name": "Door", "layer": "Layer 1"},
                    ],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_batch_set_object_properties(
                    edits=[{"ref": "name:Door", "properties": {"class": "A-Door"}}]
                )
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "resolve")
        self.assertFalse(result["writes_started"])
        self.assertEqual(result["failures"][0]["match_count"], 2)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_objects"])

    def test_batch_set_object_properties_rejects_invalid_property_before_connecting(self):
        result = json.loads(
            server.vw_batch_set_object_properties(
                edits=[{"ref": "uuid:u1", "properties": {"fill_colour": "red"}}]
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "validate")
        self.assertFalse(result["writes_started"])
        self.assertEqual(result["failures"][0]["property_name"], "fill_colour")

    def test_batch_set_object_properties_rejects_oversized_value_before_connecting(self):
        result = json.loads(
            server.vw_batch_set_object_properties(
                edits=[{"ref": "uuid:u1", "properties": {"name": "x" * (server.MAX_PROPERTY_VALUE_CHARS + 1)}}]
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "validate")
        self.assertFalse(result["writes_started"])
        self.assertEqual(result["failures"][0]["property_name"], "name")
        self.assertIn("limited", result["failures"][0]["error"])

    def test_batch_set_object_properties_rejects_invalid_values_before_connecting(self):
        result = json.loads(
            server.vw_batch_set_object_properties(
                edits=[
                    {"ref": "uuid:u1", "properties": {"name": "Valid Name"}},
                    {"ref": "uuid:u2", "properties": {"opacity": "101"}},
                    {"ref": "uuid:u3", "properties": {"lineWeight": "-1"}},
                    {"ref": "uuid:u4", "properties": {"fillColor": "65536,0,0"}},
                    {"ref": "uuid:u5", "properties": {"class": "  "}},
                ]
            )
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "validate")
        self.assertFalse(result["writes_started"])
        self.assertEqual(
            [(failure["ref"], failure["property_name"]) for failure in result["failures"]],
            [
                ("uuid:u2", "opacity"),
                ("uuid:u3", "lineWeight"),
                ("uuid:u4", "fillColor"),
                ("uuid:u5", "class"),
            ],
        )

    def test_batch_set_object_properties_normalizes_numeric_and_color_values(self):
        get_objects_calls = 0

        def handler(request):
            nonlocal get_objects_calls
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_with_set_property_status()}
            if request["action"] == "get_objects":
                get_objects_calls += 1
                objects = (
                    [{"handle": "h1", "uuid": "u1", "type": "rect", "name": "Old", "layer": "Layer 1"}]
                    if get_objects_calls == 1
                    else [{"handle": "h1", "uuid": "u1", "type": "rect", "name": "Old", "layer": "Layer 1", "opacity": 5, "fillColor": "1,2,3"}]
                )
                return {"id": request["id"], "success": True, "result": objects}
            if request["action"] == "set_property":
                self.assertIn(
                    request["params"],
                    [
                        {"handle": "h1", "property_name": "opacity", "value": "5"},
                        {"handle": "h1", "property_name": "fillColor", "value": "1,2,3"},
                    ],
                )
                return {"id": request["id"], "success": True, "result": {"changed": True}}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=5) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_batch_set_object_properties(
                    edits=[{"ref": "uuid:u1", "properties": {"opacity": "005", "fillColor": "1, 2, 3"}}]
                )
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["edits"][0]["verified"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_objects", "set_property", "set_property", "get_objects"])

    def test_batch_set_object_properties_blocks_native_bridge_without_set_property(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_without_set_property_status()}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=1) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_batch_set_object_properties(
                    edits=[{"ref": "uuid:u1", "properties": {"name": "New"}}]
                )
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["phase"], "write_preflight")
        self.assertFalse(result["writes_started"])
        self.assertEqual(result["preflight"]["reason"], "native_bridge_action_not_implemented")
        self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_drawing_summary_can_omit_examples_for_compact_agent_context(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "get_document_info":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {"filename": "Demo.vwx", "layers": ["Layer 1"], "layer_count": 1, "total_objects": 1},
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1", "visible": True}]}
            if request["action"] == "get_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [{"handle": "h1", "type": "rect", "name": "Room", "layer": "Layer 1"}],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_drawing_summary(limit=100, include_examples=False))

        self.assertTrue(result["ok"])
        self.assertFalse(result["query"]["include_examples"])
        self.assertNotIn("examples", result)
        self.assertEqual(result["counts_by_type"], {"rect": 1})
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_document_info", "get_layers", "get_objects"])

    def test_find_objects_preserves_listener_path_for_complex_criteria(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _python_dialog_status()}
            if request["action"] == "find_objects":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": [{"handle": "h1", "type": "rect", "name": "Target"}],
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_find_objects("((T=RECT) & (C='A-Test'))", limit=10))

        self.assertEqual(result[0]["handle"], "h1")
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "find_objects"])

    def test_destructive_and_trusted_code_tools_require_explicit_confirmation(self):
        delete_selection = json.loads(server.vw_selection("delete"))
        delete_class = json.loads(server.vw_manage_classes("delete", "A-Demo"))

        self.assertFalse(delete_selection["ok"])
        self.assertEqual(delete_selection["required_confirmation"], "DELETE_SELECTED")
        self.assertFalse(delete_class["ok"])
        self.assertEqual(delete_class["required_confirmation"], "DELETE_CLASS")

    def test_run_script_is_disabled_by_default_before_confirmation(self):
        result = json.loads(server.vw_run_script("print('hi')", confirm="RUN_TRUSTED_CODE"))

        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "run_script_disabled")
        self.assertFalse(result["writes_started"])

    def test_run_script_requires_confirmation_when_env_gate_enabled(self):
        original = server.ENABLE_RUN_SCRIPT
        try:
            server.ENABLE_RUN_SCRIPT = True
            result = json.loads(server.vw_run_script("print('hi')"))
        finally:
            server.ENABLE_RUN_SCRIPT = original

        self.assertFalse(result["ok"])
        self.assertEqual(result["required_confirmation"], "RUN_TRUSTED_CODE")

    def test_manage_classes_validates_class_name_before_connecting(self):
        missing = json.loads(server.vw_manage_classes("create", "  "))
        too_long = json.loads(server.vw_manage_classes("create", "x" * (server.MAX_PROPERTY_VALUE_CHARS + 1)))

        self.assertFalse(missing["ok"])
        self.assertEqual(missing["phase"], "validate")
        self.assertFalse(missing["writes_started"])
        self.assertFalse(too_long["ok"])
        self.assertEqual(too_long["phase"], "validate")
        self.assertFalse(too_long["writes_started"])

    def test_manage_classes_trims_class_name_before_sending(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_two_status()}
            if request["action"] == "manage_classes":
                self.assertEqual(request["params"], {"action": "create", "class_name": "A-Wall", "confirm": ""})
                return {"id": request["id"], "success": True, "result": {"class_name": "A-Wall", "created": True}}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_manage_classes("create", "  A-Wall  "))

        self.assertEqual(result["class_name"], "A-Wall")
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "manage_classes"])

    def test_selection_delete_blocks_arbitrary_criteria(self):
        result = json.loads(server.vw_selection("delete", "ALL", confirm="DELETE_EXACT_NAME"))

        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertEqual(result["reason"], "unsafe_delete_criteria")

    def test_create_schematic_room_composes_native_rectangles(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "batch_create_objects":
                object_count = request["params"]["object_count"]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": object_count,
                        "created": [
                            {"index": index, "type": json.loads(request["params"][f"object_{index}_json"])["object_type"], "handle": f"h-{index}"}
                            for index in range(1, object_count + 1)
                        ],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_create_schematic_room(0, 0, 4000, 3000, 200, name="Bedroom"))

        self.assertTrue(result["ok"])
        self.assertTrue(result["schematic"])
        self.assertFalse(result["bim_objects"])
        self.assertEqual(result["created_count"], 4)
        self.assertTrue(result["atomic"])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "batch_create_objects"])
        created_params = [
            json.loads(listener.requests[1]["params"][f"object_{index}_json"])
            for index in range(1, 5)
        ]
        self.assertEqual([params["object_type"] for params in created_params], ["rect", "rect", "rect", "rect"])
        self.assertEqual(created_params[0]["name"], "Bedroom south wall")
        self.assertEqual(created_params[0]["class_name"], "A-FP-Schematic-Wall")
        self.assertEqual(
            [(params["x1"], params["y1"], params["x2"], params["y2"]) for params in created_params],
            [(0, 0, 4000, 200), (0, 2800, 4000, 3000), (0, 200, 200, 2800), (3800, 200, 4000, 2800)],
        )

    def test_create_schematic_room_rejects_impossible_wall_thickness_without_connecting(self):
        result = json.loads(server.vw_create_schematic_room(0, 0, 300, 300, 200))

        self.assertFalse(result["ok"])
        self.assertIn("wall_thickness", result["error"])

    def test_create_schematic_door_composes_native_line_and_arc(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "batch_create_objects":
                object_count = request["params"]["object_count"]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": object_count,
                        "created": [
                            {"index": index, "type": json.loads(request["params"][f"object_{index}_json"])["object_type"], "handle": f"h-{index}"}
                            for index in range(1, object_count + 1)
                        ],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(
                server.vw_create_schematic_door(1000, 2000, width=900, rotation=0, swing="left", name="D1")
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["created_count"], 2)
        line_params = json.loads(listener.requests[1]["params"]["object_1_json"])
        arc_params = json.loads(listener.requests[1]["params"]["object_2_json"])
        self.assertEqual(line_params["object_type"], "line")
        self.assertEqual(line_params["name"], "D1 leaf")
        self.assertAlmostEqual(line_params["x1"], 1000)
        self.assertAlmostEqual(line_params["y1"], 2000)
        self.assertAlmostEqual(line_params["x2"], 1000)
        self.assertAlmostEqual(line_params["y2"], 2900)
        self.assertEqual(arc_params["object_type"], "arc")
        self.assertEqual(arc_params["name"], "D1 swing")
        self.assertEqual(arc_params["radius"], 900)
        self.assertEqual(arc_params["start_angle"], 0)
        self.assertEqual(arc_params["sweep_angle"], 90)

    def test_create_schematic_window_composes_parallel_native_lines(self):
        def handler(request):
            if request["action"] == "ping":
                return {"id": request["id"], "success": True, "result": _native_phase_one_status()}
            if request["action"] == "batch_create_objects":
                object_count = request["params"]["object_count"]
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "atomic": True,
                        "rollback_on_error": True,
                        "created_count": object_count,
                        "created": [
                            {"index": index, "type": json.loads(request["params"][f"object_{index}_json"])["object_type"], "handle": f"h-{index}"}
                            for index in range(1, object_count + 1)
                        ],
                    },
                }
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_create_schematic_window(0, 0, 1000, 0, marker_depth=200, name="W1"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["created_count"], 2)
        line_a = json.loads(listener.requests[1]["params"]["object_1_json"])
        line_b = json.loads(listener.requests[1]["params"]["object_2_json"])
        self.assertEqual(line_a["object_type"], "line")
        self.assertEqual(line_b["object_type"], "line")
        self.assertEqual((line_a["x1"], line_a["y1"], line_a["x2"], line_a["y2"]), (0.0, 100.0, 1000.0, 100.0))
        self.assertEqual((line_b["x1"], line_b["y1"], line_b["x2"], line_b["y2"]), (0.0, -100.0, 1000.0, -100.0))

    def test_create_schematic_window_rejects_zero_length_marker_without_connecting(self):
        result = json.loads(server.vw_create_schematic_window(10, 10, 10, 10))

        self.assertFalse(result["ok"])
        self.assertIn("endpoints", result["error"])

    def test_send_tool_reuses_recent_safe_preflight(self):
        def handler(request):
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "bridge_kind": "python_dialog_agent_session",
                        "dispatch_mode": "dialog",
                    },
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1"}]}
            if request["action"] == "get_document_info":
                return {"id": request["id"], "success": True, "result": {"filename": "Test.vwx"}}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=3) as listener:
            _configure_server(listener.port)
            layers = server.vw_get_layers()
            info = server.vw_get_document_info()

        self.assertEqual(json.loads(layers), [{"name": "Layer 1"}])
        self.assertEqual(json.loads(info), {"filename": "Test.vwx"})
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_layers", "get_document_info"])

    def test_send_tool_rechecks_preflight_after_cache_expires(self):
        now = [100.0]
        original_monotonic = server.time.monotonic
        ping_count = 0

        def handler(request):
            nonlocal ping_count
            if request["action"] == "ping":
                ping_count += 1
                safe = ping_count == 1
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": safe,
                        "transport_only": not safe,
                        "bridge_kind": "python_dialog_agent_session" if safe else "python_transport_only",
                        "dispatch_mode": "dialog" if safe else "background",
                    },
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1"}]}
            self.fail(f"Unexpected action: {request['action']}")

        try:
            server.time.monotonic = lambda: now[0]
            with ConcurrentFakeListener(handler, max_requests=3) as listener:
                _configure_server(listener.port)
                layers = server.vw_get_layers()
                now[0] += 1.0
                blocked = json.loads(server.vw_get_document_info())
        finally:
            server.time.monotonic = original_monotonic

        self.assertEqual(json.loads(layers), [{"name": "Layer 1"}])
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["blocked"])
        self.assertEqual(blocked["reason"], "transport_only_bridge")
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "get_layers", "ping"])

    def test_send_tool_can_disable_preflight_cache(self):
        def handler(request):
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "bridge_kind": "native_sdk_bridge",
                        "dispatch_mode": "native_sdk",
                    },
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1"}]}
            if request["action"] == "get_document_info":
                return {"id": request["id"], "success": True, "result": {"filename": "Test.vwx"}}
            self.fail(f"Unexpected action: {request['action']}")

        with ConcurrentFakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            server.PREFLIGHT_CACHE_SECONDS = 0
            layers = server.vw_get_layers()
            info = server.vw_get_document_info()

        self.assertEqual(json.loads(layers), [{"name": "Layer 1"}])
        self.assertEqual(json.loads(info), {"filename": "Test.vwx"})
        self.assertEqual(
            [request["action"] for request in listener.requests],
            ["ping", "get_layers", "ping", "get_document_info"],
        )

    def test_send_tool_does_not_cache_unsafe_preflight(self):
        ping_count = 0

        def handler(request):
            nonlocal ping_count
            if request["action"] == "ping":
                ping_count += 1
                safe = ping_count == 2
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": safe,
                        "transport_only": not safe,
                        "bridge_kind": "native_sdk_bridge" if safe else "python_transport_only",
                        "dispatch_mode": "native_sdk" if safe else "background",
                    },
                }
            if request["action"] == "get_layers":
                return {"id": request["id"], "success": True, "result": [{"name": "Layer 1"}]}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=3) as listener:
            _configure_server(listener.port)
            blocked = json.loads(server.vw_get_layers())
            layers = server.vw_get_layers()

        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "transport_only_bridge")
        self.assertEqual(json.loads(layers), [{"name": "Layer 1"}])
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "ping", "get_layers"])

    def test_send_tool_does_not_retry_non_idempotent_after_response_loss(self):
        def handler(request):
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "bridge_kind": "native_sdk_bridge",
                        "dispatch_mode": "native_sdk",
                    },
                }
            if request["action"] == "create_object":
                return None
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            server.TIMEOUT = 0.2
            result = server.vw_create_object("rect")

        self.assertIn("Unknown commit state", result)
        self.assertIn("did not retry", result)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "create_object"])

    def test_send_tool_reports_unknown_commit_state_for_non_idempotent_protocol_error(self):
        bad_payload = b"not json"
        bad_frame = struct.pack(">I", len(bad_payload)) + bad_payload

        def handler(request):
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "bridge_kind": "native_sdk_bridge",
                        "dispatch_mode": "native_sdk",
                    },
                }
            if request["action"] == "create_object":
                return bad_frame
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = server.vw_create_object("rect")

        self.assertIn("Unknown commit state", result)
        self.assertIn("did not retry", result)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "create_object"])

    def test_send_tool_retries_read_only_mixed_variant_after_response_loss(self):
        calls = []

        def handler(request):
            calls.append(request["action"])
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "bridge_kind": "native_sdk_bridge",
                        "dispatch_mode": "native_sdk",
                    },
                }
            if request["action"] == "selection" and calls.count("selection") == 1:
                return None
            if request["action"] == "selection":
                return {"id": request["id"], "success": True, "result": []}
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            server.TIMEOUT = 0.2
            result = server.vw_selection("get")

        self.assertEqual(json.loads(result), [])
        self.assertEqual(
            [request["action"] for request in listener.requests],
            ["ping", "selection", "ping", "selection"],
        )

    def test_send_tool_does_not_retry_write_mixed_variant_after_response_loss(self):
        def handler(request):
            if request["action"] == "ping":
                return {
                    "id": request["id"],
                    "success": True,
                    "result": {
                        "pong": True,
                        "cad_api_safe": True,
                        "transport_only": False,
                        "bridge_kind": "native_sdk_bridge",
                        "dispatch_mode": "native_sdk",
                    },
                }
            if request["action"] == "worksheet":
                return None
            self.fail(f"Unexpected action: {request['action']}")

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            server.TIMEOUT = 0.2
            result = server.vw_worksheet("write", "Schedule", row=1, col=1, value="Door")

        self.assertIn("Unknown commit state", result)
        self.assertIn("did not retry", result)
        self.assertEqual([request["action"] for request in listener.requests], ["ping", "worksheet"])

    def test_action_retry_policy_uses_tool_safety_metadata(self):
        self.assertTrue(server._action_safe_to_retry("get_layers"))
        self.assertTrue(server._action_safe_to_retry("ping"))
        self.assertTrue(server._action_safe_to_retry("selection", {"action": "get"}))
        self.assertTrue(server._action_safe_to_retry("worksheet", {"action": "read"}))
        self.assertTrue(server._action_safe_to_retry("worksheet", {"action": "read_range"}))
        self.assertTrue(server._action_safe_to_retry("manage_classes", {"action": "list"}))
        self.assertTrue(server._action_safe_to_retry("symbol", {"action": "list"}))
        self.assertFalse(server._action_safe_to_retry("create_object"))
        self.assertFalse(server._action_safe_to_retry("batch_create_objects"))
        self.assertFalse(server._action_safe_to_retry("selection"))
        self.assertFalse(server._action_safe_to_retry("selection", {"action": "delete"}))
        self.assertFalse(server._action_safe_to_retry("selection", {"action": "move"}))
        self.assertFalse(server._action_safe_to_retry("worksheet", {"action": "write"}))
        self.assertFalse(server._action_safe_to_retry("manage_classes", {"action": "delete"}))
        self.assertFalse(server._action_safe_to_retry("symbol", {"action": "insert"}))
        self.assertFalse(server._action_safe_to_retry("run_script"))

    def test_action_safety_keeps_canonical_create_object_metadata(self):
        self.assertEqual(server._ACTION_SAFETY["create_object"], server.TOOL_SAFETY["vw_create_object"])
        self.assertEqual(server._ACTION_SAFETY["batch_create_objects"], server.TOOL_SAFETY["vw_batch_create_objects"])
        self.assertEqual(server._ACTION_SAFETY["selection"], server.TOOL_SAFETY["vw_selection"])

    def test_send_tool_leaves_health_tools_unguarded(self):
        calls = []
        original_send = server._send_health
        try:
            server._send_health = (
                lambda action, params=None, require_cad_safe=False:
                calls.append((action, params, require_cad_safe)) or '{"pong": true}'
            )
            self.assertEqual(server.vw_ping(), '{"pong": true}')
        finally:
            server._send_health = original_send

        self.assertEqual(calls, [("ping", None, False)])

    def test_tool_safety_covers_all_server_tools(self):
        tool_functions = {
            name
            for name, value in vars(server).items()
            if name.startswith("vw_") and inspect.isfunction(value)
        }

        self.assertEqual(set(server.TOOL_SAFETY), tool_functions)

    def test_tool_safety_annotations_are_consistent(self):
        required = {
            "category",
            "wire_action",
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
            "requires_cad_preflight",
        }
        for tool_name, safety in server.TOOL_SAFETY.items():
            with self.subTest(tool=tool_name):
                self.assertTrue(required.issubset(safety))
                self.assertIsInstance(safety["category"], str)
                if "actions" in safety:
                    self.assertEqual(safety.get("action_param"), "action")
                    self.assertIsInstance(safety["actions"], dict)
                    self.assertGreater(len(safety["actions"]), 0)
                    for variant_name, variant in safety["actions"].items():
                        self.assertIsInstance(variant_name, str)
                        self.assertTrue({"readOnlyHint", "destructiveHint", "idempotentHint"}.issubset(variant))
                        operation = server._operation_safety(safety["wire_action"], {"action": variant_name})
                        self.assertIsNotNone(operation)
                        self.assertTrue(set(server._ANNOTATION_KEYS).issubset(operation))
                        self.assertIn("writesDocument", variant)
                        self.assertIn("writesFiles", variant)
                        self.assertIn("confirmationRequired", variant)
                        if variant["readOnlyHint"]:
                            self.assertFalse(variant["destructiveHint"])
                        if variant["destructiveHint"]:
                            self.assertFalse(variant["readOnlyHint"])
                if safety["readOnlyHint"]:
                    self.assertFalse(safety["destructiveHint"])
                if safety["destructiveHint"]:
                    self.assertFalse(safety["readOnlyHint"])
                annotations = server._annotations_for(tool_name)
                self.assertEqual(set(annotations), set(server._ANNOTATION_KEYS))

    def test_tool_safety_tool_returns_structured_metadata(self):
        safety = json.loads(server.vw_tool_safety())

        self.assertIn("vw_run_script", safety)
        self.assertIn("vw_agent_context", safety)
        self.assertIn("vw_capabilities", safety)
        self.assertIn("vw_batch_create_objects", safety)
        self.assertIn("vw_plan_schematic_floor_plan", safety)
        self.assertIn("vw_create_schematic_floor_plan", safety)
        self.assertIn("vw_drawing_summary", safety)
        self.assertIn("vw_lookup_objects", safety)
        self.assertIn("vw_batch_set_object_properties", safety)
        self.assertTrue(safety["vw_ping"]["readOnlyHint"])
        self.assertTrue(safety["vw_agent_context"]["readOnlyHint"])
        self.assertEqual(safety["vw_agent_context"]["composes_actions"], ["ping", "get_document_info", "get_layers", "get_objects"])
        self.assertTrue(safety["vw_capabilities"]["readOnlyHint"])
        self.assertFalse(safety["vw_preflight_for_cad"]["requires_cad_preflight"])
        self.assertTrue(safety["vw_get_layers"]["requires_cad_preflight"])
        self.assertTrue(safety["vw_run_script"]["destructiveHint"])
        self.assertTrue(safety["vw_create_object"]["requires_cad_preflight"])
        self.assertEqual(safety["vw_batch_create_objects"]["wire_action"], "batch_create_objects")
        self.assertNotIn("composes_actions", safety["vw_batch_create_objects"])
        self.assertIsNone(safety["vw_plan_schematic_floor_plan"]["wire_action"])
        self.assertFalse(safety["vw_plan_schematic_floor_plan"]["requires_cad_preflight"])
        self.assertTrue(safety["vw_plan_schematic_floor_plan"]["readOnlyHint"])
        self.assertIsNone(safety["vw_create_schematic_floor_plan"]["wire_action"])
        self.assertEqual(safety["vw_create_schematic_floor_plan"]["composes_actions"], ["create_object"])
        self.assertEqual(safety["vw_drawing_summary"]["wire_action"], "drawing_summary")
        self.assertEqual(safety["vw_drawing_summary"]["composes_actions"], ["drawing_summary", "get_document_info", "get_layers", "get_objects"])
        self.assertEqual(safety["vw_find_objects"]["wire_action"], "find_objects")
        self.assertEqual(safety["vw_find_objects"]["composes_actions"], ["get_objects"])
        self.assertIsNone(safety["vw_lookup_objects"]["wire_action"])
        self.assertEqual(safety["vw_lookup_objects"]["composes_actions"], ["get_objects"])
        self.assertIsNone(safety["vw_batch_set_object_properties"]["wire_action"])
        self.assertEqual(safety["vw_batch_set_object_properties"]["composes_actions"], ["get_objects", "set_property"])
        self.assertTrue(safety["vw_selection"]["actions"]["get"]["readOnlyHint"])
        self.assertTrue(safety["vw_selection"]["actions"]["delete"]["destructiveHint"])
        self.assertTrue(safety["vw_selection"]["actions"]["delete"]["confirmationRequired"])
        self.assertTrue(safety["vw_worksheet"]["actions"]["read_range"]["readOnlyHint"])
        self.assertTrue(safety["vw_worksheet"]["actions"]["write"]["writesDocument"])
        self.assertTrue(safety["vw_manage_classes"]["actions"]["delete"]["destructiveHint"])
        self.assertTrue(safety["vw_symbol"]["actions"]["list"]["readOnlyHint"])
        self.assertTrue(safety["vw_symbol"]["actions"]["insert"]["writesDocument"])
        self.assertTrue(safety["vw_run_script"]["executesCode"])
        self.assertTrue(safety["vw_run_script"]["confirmationRequired"])

    def test_annotations_for_read_only_tool(self):
        annotations = server._annotations_for("vw_get_layers")

        self.assertEqual(set(annotations), set(server._ANNOTATION_KEYS))
        self.assertTrue(annotations["readOnlyHint"])
        self.assertFalse(annotations["destructiveHint"])

    def test_cad_preflight_allows_cad_safe_bridge(self):
        original_send = server._send_health
        try:
            server._send_health = lambda action, params=None: json.dumps(
                {
                    "pong": True,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "bridge_kind": "python_dialog_agent_session",
                    "dispatch_mode": "dialog",
                    "handlers": 23,
                    "version": "test",
                }
            )
            result = server.vw_preflight_for_cad()
        finally:
            server._send_health = original_send

        preflight = json.loads(result)
        self.assertTrue(preflight["ok"])
        self.assertTrue(preflight["cad_api_safe"])
        self.assertEqual(preflight["bridge_kind"], "python_dialog_agent_session")
        self.assertEqual(preflight["reason"], "cad_api_safe")
        self.assertIn("vw_get_document_info", preflight["next_action"])

    def test_cad_preflight_blocks_transport_only_bridge(self):
        original_send = server._send_health
        try:
            server._send_health = lambda action, params=None: json.dumps(
                {
                    "pong": True,
                    "cad_api_safe": False,
                    "transport_only": True,
                    "bridge_kind": "python_transport_only",
                    "dispatch_mode": "win_timer",
                }
            )
            result = server.vw_preflight_for_cad()
        finally:
            server._send_health = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertFalse(preflight["cad_api_safe"])
        self.assertTrue(preflight["transport_only"])
        self.assertEqual(preflight["reason"], "transport_only_bridge")

    def test_cad_preflight_blocks_native_bridge_missing_capabilities(self):
        original_send = server._send_health
        try:
            server._send_health = lambda action, params=None: json.dumps(
                {
                    "pong": True,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "native_bridge": True,
                    "native_phase": 0,
                    "implemented_actions": ["ping", "stop"],
                    "bridge_kind": "native_sdk_bridge_scaffold",
                    "dispatch_mode": "native_sdk",
                    "handlers": 2,
                    "version": "native-scaffold-phase0",
                }
            )
            result = server.vw_preflight_for_cad()
        finally:
            server._send_health = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertFalse(preflight["cad_api_safe"])
        self.assertTrue(preflight["native_bridge"])
        self.assertEqual(preflight["reason"], "native_bridge_not_phase1_ready")
        self.assertIn("native_phase is not >= 1", preflight["native_readiness_errors"])
        self.assertIn("implemented_actions missing", "\n".join(preflight["native_readiness_errors"]))

    def test_cad_preflight_blocks_native_bridge_without_ready_pump(self):
        original_send = server._send_health
        try:
            server._send_health = lambda action, params=None: json.dumps(
                {
                    "pong": True,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "native_bridge": True,
                    "native_phase": 1,
                    "implemented_actions": sorted(server.NATIVE_PHASE_ONE_REQUIRED_ACTIONS),
                    "bridge_kind": "native_sdk_bridge_phase1",
                    "dispatch_mode": "native_sdk",
                    "handlers": 8,
                    "version": "native-sdk-bridge-phase1",
                    "main_context_pump": "win32_ui_timer",
                    "main_context_pump_ready": False,
                }
            )
            result = server.vw_preflight_for_cad()
        finally:
            server._send_health = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertFalse(preflight["cad_api_safe"])
        self.assertTrue(preflight["native_bridge"])
        self.assertEqual(preflight["reason"], "native_bridge_not_phase1_ready")
        self.assertEqual(preflight["main_context_pump"], "win32_ui_timer")
        self.assertFalse(preflight["main_context_pump_ready"])
        self.assertIn("main_context_pump_ready is not true", preflight["native_readiness_errors"])

    def test_cad_preflight_blocks_legacy_foreground_bridge(self):
        original_send = server._send_health
        try:
            server._send_health = lambda action, params=None: json.dumps(
                {
                    "pong": True,
                    "cad_api_safe": True,
                    "transport_only": False,
                    "bridge_kind": "python_foreground_diagnostic",
                    "dispatch_mode": "foreground",
                }
            )
            result = server.vw_preflight_for_cad()
        finally:
            server._send_health = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertFalse(preflight["cad_api_safe"])
        self.assertEqual(preflight["reason"], "foreground_diagnostic_bridge")
        self.assertIn("vw_load_listener_2024.py", preflight["next_action"])

    def test_cad_preflight_blocks_legacy_status_without_safety_field(self):
        original_send = server._send_health
        try:
            server._send_health = lambda action, params=None: json.dumps({"pong": True, "version": "legacy"})
            result = server.vw_preflight_for_cad()
        finally:
            server._send_health = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["reason"], "legacy_status_without_cad_api_safe")

    def test_cad_preflight_blocks_connection_error_text(self):
        original_send = server._send_health
        try:
            server._send_health = lambda action, params=None: "Connection error: listener missing"
            result = server.vw_preflight_for_cad()
        finally:
            server._send_health = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["reason"], "ping_failed_or_non_json")


if __name__ == "__main__":
    unittest.main()
