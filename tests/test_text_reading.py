import json
import unittest
from pathlib import Path

import server
from tests.test_server_protocol import FakeListener, _configure_server, _native_phase_four_status


def response(request, result):
    return {"id": request["id"], "success": True, "result": result}


def text_status():
    status = _native_phase_four_status()
    status["object_read_features"] = ["text_content"]
    return status


class TextReadingTests(unittest.TestCase):
    def tearDown(self):
        server._close()

    def test_query_and_selection_preserve_exact_text_and_non_text_records(self):
        texts = ["", 'B\u00fcro \u2013 \u00c4\u00d6\u00dc \u00df\r\n"quoted"\tC:\\Plans',
                 "\u65e5\u672c\u8a9e \U0001f3e0\nsecond line\rthird\x00end", "long \u00e4\n" * 10000]
        objects = [{"uuid": f"text-{index}", "type": "text", "type_id": 10, "text": value}
                   for index, value in enumerate(texts)]
        objects.append({"uuid": "rect-1", "type": "rect"})
        for action in ("query", "selection"):
            for fields in (None, ["uuid", "type", "text"]):
                with self.subTest(action=action, fields=fields):
                    def handler(request):
                        if request["action"] == "ping":
                            return response(request, text_status())
                        self.assertEqual(request["action"], "get_objects" if action == "query" else "selection")
                        return response(request, objects)

                    with FakeListener(handler, max_requests=2) as listener:
                        _configure_server(listener.port)
                        result = json.loads(server.vw_read(action, fields=fields))
                    self.assertTrue(result["ok"], result)
                    self.assertEqual([obj["text"] for obj in result["data"][:-1]], texts)
                    self.assertNotIn("text", result["data"][-1])
                    self.assertEqual(result["bridge"]["object_read_features"], ["text_content"])

    def test_explicit_text_read_rejects_old_or_malformed_capability_before_dispatch(self):
        for features in (None, [], "text_content"):
            with self.subTest(features=features):
                status = _native_phase_four_status()
                if features is not None:
                    status["object_read_features"] = features
                with FakeListener(lambda request: response(request, status), max_requests=1) as listener:
                    _configure_server(listener.port)
                    result = json.loads(server.vw_read("query", object_type="text", fields=["uuid", "text"]))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "capability_unavailable")
                self.assertFalse(result["error"]["writes_started"])
                self.assertEqual(result["error"]["detail"]["required_object_read_feature"], "text_content")
                self.assertEqual([request["action"] for request in listener.requests], ["ping"])

    def test_older_bridge_still_supports_metadata_only_queries(self):
        def handler(request):
            return response(request, _native_phase_four_status() if request["action"] == "ping"
                            else [{"uuid": "old-text", "type": "text"}])

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_read("query", fields=["uuid", "type"]))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"], [{"uuid": "old-text", "type": "text"}])

    def test_named_query_reads_content_distinct_from_object_name(self):
        def handler(request):
            if request["action"] == "ping":
                return response(request, text_status())
            self.assertEqual(request["action"], "find_objects")
            self.assertEqual(request["params"]["criteria"], "((N='Label 36'))")
            return response(request, [{"uuid": "label-36", "type": "text", "name": "Label 36", "text": "Actual edited content"}])

        with FakeListener(handler, max_requests=2) as listener:
            _configure_server(listener.port)
            result = json.loads(server.vw_read("query", criteria="((N='Label 36'))", fields=["uuid", "name", "text"]))
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"], [{"uuid": "label-36", "name": "Label 36", "text": "Actual edited content"}])

    def test_malformed_text_read_payload_is_rejected(self):
        for payload in ({"unexpected": []}, [None], ["not an object"]):
            with self.subTest(payload=payload):
                def handler(request):
                    return response(request, text_status() if request["action"] == "ping" else payload)

                with FakeListener(handler, max_requests=2) as listener:
                    _configure_server(listener.port)
                    result = json.loads(server.vw_read("query", fields=["text"]))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "native_action_failed")

    def test_missing_or_invalid_text_is_not_reported_as_empty(self):
        for invalid in ({}, {"text": None}, {"text": 42}):
            with self.subTest(invalid=invalid):
                def handler(request):
                    return response(request, text_status() if request["action"] == "ping"
                                    else [{"uuid": "bad-text", "type": "text", **invalid}])

                with FakeListener(handler, max_requests=2) as listener:
                    _configure_server(listener.port)
                    result = json.loads(server.vw_read("query", fields=["text"]))
                self.assertFalse(result["ok"])
                self.assertEqual(result["error"]["code"], "native_action_failed")

    def test_pagination_keeps_every_text_value(self):
        objects = [{"uuid": f"text-{i}", "type": "text", "text": f"Room {i}\n\u00e4"} for i in range(5)]

        def handler(request):
            if request["action"] == "ping":
                return response(request, text_status())
            self.assertEqual(request["params"]["object_type"], "text")
            return response(request, objects[:request["params"]["limit"]])

        with FakeListener(handler, max_requests=4) as listener:
            _configure_server(listener.port)
            received = []
            cursor = ""
            for _ in range(3):
                result = json.loads(server.vw_read("query", object_type="text", limit=2,
                                                 cursor=cursor, fields=["uuid", "type", "text"]))
                self.assertTrue(result["ok"], result)
                received.extend(result["data"])
                cursor = result["page"]["next_cursor"]
        self.assertIsNone(cursor)
        self.assertEqual(received, objects)

    def test_repeated_reads_observe_updated_native_content(self):
        values = iter(["Original text", "Edited \u00fc\nnew line"])

        def handler(request):
            if request["action"] == "ping":
                return response(request, text_status())
            return response(request, [{"uuid": "same-object", "type": "text", "text": next(values)}])

        with FakeListener(handler, max_requests=3) as listener:
            _configure_server(listener.port)
            first = json.loads(server.vw_read("query", fields=["uuid", "text"]))
            second = json.loads(server.vw_read("query", fields=["uuid", "text"]))
        self.assertEqual(first["data"], [{"uuid": "same-object", "text": "Original text"}])
        self.assertEqual(second["data"], [{"uuid": "same-object", "text": "Edited \u00fc\nnew line"}])

    def test_native_serializer_reads_live_text_only_for_text_nodes(self):
        source = (Path(__file__).resolve().parents[1] / "native_bridge/src/VectorworksMCPBridge.cpp").read_text(encoding="utf-8")
        serializer = source.split("std::string ObjectJson(MCObjectHandle object,", 1)[1].split("std::string ObjectListJson", 1)[0]
        self.assertIn('if (type == kTextNode && wants("text"))', serializer)
        self.assertIn("JsonString(TxToUtf8(gSDK->GetTextChars(object)))", serializer)


if __name__ == "__main__":
    unittest.main()
