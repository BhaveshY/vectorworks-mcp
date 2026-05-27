import json
import inspect
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


def _configure_server(port, max_frame_bytes=1024 * 1024):
    server._close()
    server.HOST = "127.0.0.1"
    server.PORT = port
    server.TIMEOUT = 1
    server.MAX_FRAME_BYTES = max_frame_bytes
    server.PREFLIGHT_CACHE_SECONDS = 0.75
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
        original_send = server._send
        try:
            server._send = (
                lambda action, params=None, require_cad_safe=False:
                calls.append((action, params, require_cad_safe)) or '{"pong": true}'
            )
            self.assertEqual(server.vw_bridge_status(), '{"pong": true}')
        finally:
            server._send = original_send

        self.assertEqual(calls, [("ping", None, False)])

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
            with FakeListener(handler, max_requests=3) as listener:
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

        with FakeListener(handler, max_requests=4) as listener:
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
        self.assertFalse(server._action_safe_to_retry("selection"))
        self.assertFalse(server._action_safe_to_retry("selection", {"action": "delete"}))
        self.assertFalse(server._action_safe_to_retry("selection", {"action": "move"}))
        self.assertFalse(server._action_safe_to_retry("worksheet", {"action": "write"}))
        self.assertFalse(server._action_safe_to_retry("manage_classes", {"action": "delete"}))
        self.assertFalse(server._action_safe_to_retry("symbol", {"action": "insert"}))
        self.assertFalse(server._action_safe_to_retry("run_script"))

    def test_send_tool_leaves_health_tools_unguarded(self):
        calls = []
        original_send = server._send
        try:
            server._send = (
                lambda action, params=None, require_cad_safe=False:
                calls.append((action, params, require_cad_safe)) or '{"pong": true}'
            )
            self.assertEqual(server.vw_ping(), '{"pong": true}')
        finally:
            server._send = original_send

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
        self.assertTrue(safety["vw_ping"]["readOnlyHint"])
        self.assertFalse(safety["vw_preflight_for_cad"]["requires_cad_preflight"])
        self.assertTrue(safety["vw_get_layers"]["requires_cad_preflight"])
        self.assertTrue(safety["vw_run_script"]["destructiveHint"])
        self.assertTrue(safety["vw_create_object"]["requires_cad_preflight"])
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
        original_send = server._send
        try:
            server._send = lambda action, params=None: json.dumps(
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
            server._send = original_send

        preflight = json.loads(result)
        self.assertTrue(preflight["ok"])
        self.assertTrue(preflight["cad_api_safe"])
        self.assertEqual(preflight["bridge_kind"], "python_dialog_agent_session")
        self.assertEqual(preflight["reason"], "cad_api_safe")
        self.assertIn("vw_get_document_info", preflight["next_action"])

    def test_cad_preflight_blocks_transport_only_bridge(self):
        original_send = server._send
        try:
            server._send = lambda action, params=None: json.dumps(
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
            server._send = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertFalse(preflight["cad_api_safe"])
        self.assertTrue(preflight["transport_only"])
        self.assertEqual(preflight["reason"], "transport_only_bridge")

    def test_cad_preflight_blocks_native_bridge_missing_capabilities(self):
        original_send = server._send
        try:
            server._send = lambda action, params=None: json.dumps(
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
            server._send = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertFalse(preflight["cad_api_safe"])
        self.assertTrue(preflight["native_bridge"])
        self.assertEqual(preflight["reason"], "native_bridge_not_phase1_ready")
        self.assertIn("native_phase is not >= 1", preflight["native_readiness_errors"])
        self.assertIn("implemented_actions missing", "\n".join(preflight["native_readiness_errors"]))

    def test_cad_preflight_blocks_legacy_foreground_bridge(self):
        original_send = server._send
        try:
            server._send = lambda action, params=None: json.dumps(
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
            server._send = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertFalse(preflight["cad_api_safe"])
        self.assertEqual(preflight["reason"], "foreground_diagnostic_bridge")
        self.assertIn("vw_load_listener_2024.py", preflight["next_action"])

    def test_cad_preflight_blocks_legacy_status_without_safety_field(self):
        original_send = server._send
        try:
            server._send = lambda action, params=None: json.dumps({"pong": True, "version": "legacy"})
            result = server.vw_preflight_for_cad()
        finally:
            server._send = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["reason"], "legacy_status_without_cad_api_safe")

    def test_cad_preflight_blocks_connection_error_text(self):
        original_send = server._send
        try:
            server._send = lambda action, params=None: "Connection error: listener missing"
            result = server.vw_preflight_for_cad()
        finally:
            server._send = original_send

        preflight = json.loads(result)
        self.assertFalse(preflight["ok"])
        self.assertEqual(preflight["reason"], "ping_failed_or_non_json")


if __name__ == "__main__":
    unittest.main()
