import json
import unittest

import server
from tests.test_server_protocol import FakeListener, _configure_server, _native_phase_four_status
from tests.test_documentation_workflow_contract import BINDING


def status():
    result = _native_phase_four_status()
    result["implemented_actions"] += ["query_objects", "transaction_status"]
    result["object_read_features"] = ["text_content", "paged_object_reads", "bound_apply_operations"]
    return result


def response(request, result):
    return {"id": request["id"], "success": True, "result": result}


class NativeQueryPageTests(unittest.TestCase):
    def tearDown(self):
        server._close()

    def test_late_page_does_not_request_prior_records_and_projects_natively(self):
        def handler(request):
            if request["action"] == "ping":
                return response(request, status())
            self.assertEqual(request["action"], "query_objects")
            params = request["params"]
            self.assertEqual((params["offset"], params["limit"]), (1500, 2))
            self.assertEqual({params[f"field_{i}"] for i in range(1, params["field_count"] + 1)}, {"uuid", "text", "type"})
            self.assertEqual(params["expected_document_fingerprint"], BINDING["document_fingerprint"])
            return response(request, {"offset": 1500, "has_more": True, "binding": BINDING,
                "items": [{"uuid": "a", "text": "ä", "type": "text"}, {"uuid": "b", "type": "rect"}]})
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_read("query", limit=2, cursor="1500", fields=["uuid", "text"], target_binding=BINDING))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["page"]["next_cursor"], "1502")
        self.assertEqual(result["data"], [{"uuid": "a", "text": "ä"}, {"uuid": "b"}])

    def test_unprojected_selection_in_unsaved_document_retains_all_fields(self):
        binding = dict(BINDING, file_path="", file_name="Untitled.vwx")
        item = {"uuid": "a", "type": "text", "text": "", "bounds": {"top_left": [0, 1]}, "opacity": 60}
        def handler(request):
            if request["action"] == "ping":
                return response(request, status())
            self.assertEqual(request["params"]["mode"], "selection")
            self.assertEqual(request["params"]["field_count"], 0)
            return response(request, {"offset": 0, "has_more": False, "binding": binding, "items": [item]})
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_read("selection"))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"], [item])

    def test_malformed_page_does_not_fall_back_or_invent_text(self):
        for page in ({"items": [{"type": "text"}], "has_more": False, "offset": 0},
                     {"items": [], "has_more": True, "offset": 0},
                     {"items": [], "has_more": False, "offset": 1}):
            with self.subTest(page=page):
                actions = []
                def handler(request):
                    actions.append(request["action"])
                    return response(request, status() if request["action"] == "ping" else {**page, "binding": BINDING})
                with FakeListener(handler, max_requests=2) as listener:
                    _configure_server(listener.port)
                    result = json.loads(server.vw_read("query", fields=["text"]))
                self.assertFalse(result["ok"])
                self.assertEqual(actions, ["ping", "query_objects"])

    def test_transaction_unknown_is_not_permission_to_retry(self):
        def handler(request):
            if request["action"] == "ping":
                return response(request, status())
            self.assertEqual(request["action"], "transaction_status")
            return response(request, {"state": "unknown", "retry_safe": False})
        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_status("transaction", idempotency_key="unknown-key"))
        self.assertTrue(result["ok"])
        self.assertFalse(result["data"]["retry_safe"])

    def test_general_binding_is_forwarded_and_old_bridge_rejected(self):
        for supported in (True, False):
            with self.subTest(supported=supported):
                actions = []
                def handler(request):
                    actions.append(request["action"])
                    if request["action"] == "ping":
                        return response(request, status() if supported else _native_phase_four_status())
                    self.assertEqual(request["params"]["expected_dirty"], False)
                    self.assertEqual(request["params"]["expected_file_path"], BINDING["file_path"])
                    return {"id": request["id"], "success": False, "error": "target binding mismatch: simulated document switch"}
                with FakeListener(handler, max_requests=2 if supported else 1) as listener:
                    _configure_server(listener.port)
                    result = json.loads(server.vw_apply([{"type": "create", "params": {"object_type": "rect", "x1": 0, "y1": 0, "x2": 1, "y2": 1}}], f"bound-{supported}", target_binding=BINDING))
                self.assertFalse(result["ok"])
                self.assertEqual(actions, ["ping", "apply_operations"] if supported else ["ping"])


if __name__ == "__main__":
    unittest.main()
