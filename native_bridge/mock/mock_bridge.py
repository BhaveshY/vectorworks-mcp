import json
import re
import socket
import struct
import threading


EXACT_NAME_CRITERIA_RE = re.compile(r"^\(\(N='[^']{1,255}'\)\)$")


IMPLEMENTED_ACTIONS = {
    "ping",
    "stop",
    "get_document_info",
    "get_layers",
    "get_objects",
    "drawing_summary",
    "selection",
    "create_object",
    "batch_create_objects",
    "create_wall",
    "create_text",
    "create_linear_dimension",
    "set_property",
    "find_objects",
    "manage_classes",
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


def _mock_bounds(params, object_type):
    if object_type == "wall":
        return {
            "top_left": [params.get("start_x", params.get("x1", 0)), params.get("start_y", params.get("y1", 0))],
            "bottom_right": [params.get("end_x", params.get("x2", 100)), params.get("end_y", params.get("y2", 100))],
        }
    if object_type == "text":
        return {
            "top_left": [params.get("x", params.get("x1", 0)), params.get("y", params.get("y1", 0))],
            "bottom_right": [params.get("x", params.get("x1", 0)) + params.get("width", 100), params.get("y", params.get("y1", 0))],
        }
    if object_type == "linear_dimension":
        return {
            "top_left": [params.get("start_x", params.get("x1", 0)), params.get("start_y", params.get("y1", 0))],
            "bottom_right": [params.get("end_x", params.get("x2", 100)), params.get("end_y", params.get("y2", 0))],
        }
    return {
        "top_left": [params.get("x1", 0), params.get("y1", 0)],
        "bottom_right": [params.get("x2", 100), params.get("y2", 100)],
    }


def _object_matches_criteria(obj, criteria):
    text = str(criteria or "ALL").strip()
    upper = text.upper()
    if upper == "ALL":
        return True
    exact_name = re.fullmatch(r"\(\(N='([^']{1,255})'\)\)", text)
    if exact_name:
        return str(obj.get("name", "")) == exact_name.group(1)
    simple = re.fullmatch(r"\(?\(?\s*([NTC])\s*=\s*'?([^')]+)'?\s*\)?\)?", text, flags=re.IGNORECASE)
    if not simple:
        return False
    key = simple.group(1).upper()
    value = simple.group(2).strip()
    if key == "N":
        return str(obj.get("name", "")) == value
    if key == "T":
        return str(obj.get("type", "")).lower() == value.lower()
    if key == "C":
        return str(obj.get("class") or obj.get("class_name") or "") == value
    return False


def _summary_from_state(document_info, layers, objects, params):
    layer_filter = str(params.get("layer", ""))
    object_type = str(params.get("object_type", "")).lower()
    scan_limit = int(params.get("scan_limit", params.get("limit", 1000)))
    include_examples = bool(params.get("include_examples", True))
    example_limit = int(params.get("example_limit", 20))
    filtered = []
    for obj in objects:
        if len(filtered) >= scan_limit:
            break
        if not isinstance(obj, dict):
            continue
        if layer_filter and str(obj.get("layer", "")) != layer_filter:
            continue
        if object_type and str(obj.get("type", "")).lower() != object_type:
            continue
        filtered.append(obj)

    by_type = {}
    by_layer = {}
    by_class = {}
    by_layer_type = {}
    named_count = 0
    bounds = None
    for obj in filtered:
        obj_type = str(obj.get("type") or "unknown")
        obj_layer = str(obj.get("layer") or "unknown")
        obj_class = str(obj.get("class") or obj.get("class_name") or "")
        by_type[obj_type] = by_type.get(obj_type, 0) + 1
        by_layer[obj_layer] = by_layer.get(obj_layer, 0) + 1
        by_layer_type.setdefault(obj_layer, {})[obj_type] = by_layer_type.setdefault(obj_layer, {}).get(obj_type, 0) + 1
        if obj_class:
            by_class[obj_class] = by_class.get(obj_class, 0) + 1
        if str(obj.get("name") or "").strip():
            named_count += 1
        obj_bounds = obj.get("bounds")
        if isinstance(obj_bounds, dict):
            top_left = obj_bounds.get("top_left")
            bottom_right = obj_bounds.get("bottom_right")
            if isinstance(top_left, list) and isinstance(bottom_right, list) and len(top_left) >= 2 and len(bottom_right) >= 2:
                left, right = sorted([float(top_left[0]), float(bottom_right[0])])
                top, bottom = sorted([float(top_left[1]), float(bottom_right[1])])
                if bounds is None:
                    bounds = {"left": left, "top": top, "right": right, "bottom": bottom}
                else:
                    bounds["left"] = min(bounds["left"], left)
                    bounds["top"] = min(bounds["top"], top)
                    bounds["right"] = max(bounds["right"], right)
                    bounds["bottom"] = max(bounds["bottom"], bottom)

    payload = {
        "ok": True,
        "tool": "vw_drawing_summary",
        "native_summary": True,
        "query": {
            "layer": layer_filter,
            "object_type": object_type,
            "scan_limit": scan_limit,
            "include_examples": include_examples,
            "example_limit": example_limit,
            "source": "native_drawing_summary",
        },
        "document": document_info,
        "layer_count": len(layers),
        "layers": layers,
        "objects_returned": len(filtered),
        "objects_scanned": len(filtered),
        "document_total_objects": document_info.get("total_objects") if isinstance(document_info, dict) else None,
        "possibly_truncated": len(filtered) >= scan_limit,
        "named_objects_returned": named_count,
        "counts_by_type": dict(sorted(by_type.items())),
        "counts_by_layer": dict(sorted(by_layer.items())),
        "counts_by_layer_type": {key: dict(sorted(value.items())) for key, value in sorted(by_layer_type.items())},
        "counts_by_class": dict(sorted(by_class.items())),
        "bounds": bounds,
    }
    if include_examples:
        payload["examples"] = filtered[:example_limit]
    return payload


class MockNativeBridge:
    """Small TCP bridge used by tests to exercise the native protocol contract."""

    def __init__(
        self,
        status=None,
        document_info=None,
        layers=None,
        objects=None,
        response_overrides=None,
        respect_object_filters=True,
        release_on_stop=True,
    ):
        self.status = status or {
            "pong": True,
            "handlers": len(IMPLEMENTED_ACTIONS),
            "version": "mock-native-bridge",
            "bridge_kind": "native_sdk_bridge_mock",
            "dispatch_mode": "native_sdk",
            "cad_api_safe": True,
            "transport_only": False,
            "native_bridge": True,
            "native_phase": 2,
            "implemented_actions": sorted(IMPLEMENTED_ACTIONS),
            "main_context_pump": "win32_ui_timer",
            "main_context_pump_ready": True,
        }
        self.layers = layers if layers is not None else [{"name": "Design Layer-1", "visible": True}]
        self.objects = objects if objects is not None else [
            {
                "handle": "mock-rect-1",
                "type": "rect",
                "name": "Mock Rect",
                "bounds": {
                    "top_left": [0, 0],
                    "bottom_right": [100, 100],
                },
            }
        ]
        self.document_info = document_info if document_info is not None else {
            "filename": "Mock.vwx",
            "filepath": "",
            "layers": [layer["name"] for layer in self.layers if isinstance(layer, dict) and "name" in layer],
            "layer_count": len(self.layers),
            "total_objects": len(self.objects),
        }
        self.selection = []
        self.classes = {"None"}
        for obj in self.objects:
            if isinstance(obj, dict) and obj.get("class"):
                self.classes.add(str(obj["class"]))
        self.created_count = 0
        self.response_overrides = response_overrides or {}
        self.respect_object_filters = respect_object_filters
        self.release_on_stop = release_on_stop
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
                if action in self.response_overrides:
                    override = self.response_overrides[action]
                    payload = override(request) if callable(override) else dict(override)
                    payload.setdefault("id", request_id)
                    _write_json_frame(conn, payload)
                    continue
                if action == "ping":
                    _write_json_frame(conn, {"id": request_id, "success": True, "result": self.status})
                elif action == "get_document_info":
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": self.document_info,
                        },
                    )
                elif action == "get_layers":
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": self.layers,
                        },
                    )
                elif action == "get_objects":
                    params = request.get("params", {})
                    limit = int(params.get("limit", 100))
                    object_type = str(params.get("object_type", "")).lower()
                    objects = list(self.objects)
                    if self.respect_object_filters:
                        objects = [
                            obj for obj in objects
                            if not object_type or obj.get("type") == object_type
                        ][:limit]
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": objects,
                        },
                    )
                elif action == "drawing_summary":
                    params = request.get("params", {})
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": _summary_from_state(self.document_info, self.layers, self.objects, params),
                        },
                    )
                elif action == "find_objects":
                    params = request.get("params", {})
                    limit = int(params.get("limit", 100))
                    criteria = params.get("criteria", "ALL")
                    matches = [
                        obj for obj in self.objects
                        if isinstance(obj, dict) and _object_matches_criteria(obj, criteria)
                    ][:limit]
                    _write_json_frame(conn, {"id": request_id, "success": True, "result": matches})
                elif action == "selection":
                    params = request.get("params", {})
                    selection_action = params.get("action", "get")
                    if selection_action == "get":
                        selected = [
                            obj for obj in self.objects
                            if isinstance(obj, dict) and obj.get("handle") in self.selection
                        ]
                        _write_json_frame(conn, {"id": request_id, "success": True, "result": selected})
                    elif selection_action == "clear":
                        self.selection = []
                        _write_json_frame(conn, {"id": request_id, "success": True, "result": "Selection cleared"})
                    elif selection_action == "select":
                        criteria = str(params.get("criteria", ""))
                        self.selection = [
                            obj["handle"] for obj in self.objects
                            if obj["handle"] in criteria or obj["name"] in criteria
                        ]
                        result = "Selected {0} objects".format(len(self.selection))
                        _write_json_frame(conn, {"id": request_id, "success": True, "result": result})
                    elif selection_action == "delete":
                        criteria = str(params.get("criteria", ""))
                        if criteria:
                            expected_confirm = "DELETE_EXACT_NAME"
                            criteria_valid = EXACT_NAME_CRITERIA_RE.fullmatch(criteria) is not None
                        else:
                            expected_confirm = "DELETE_SELECTED"
                            criteria_valid = True
                        if params.get("confirm") != expected_confirm:
                            _write_json_frame(
                                conn,
                                {
                                    "id": request_id,
                                    "success": False,
                                    "error": "selection delete requires confirm='{0}'".format(expected_confirm),
                                },
                            )
                            continue
                        if not criteria_valid:
                            _write_json_frame(
                                conn,
                                {
                                    "id": request_id,
                                    "success": False,
                                    "error": "selection delete criteria must be exact object-name criteria",
                                },
                            )
                            continue
                        if criteria:
                            selected = {
                                obj["handle"] for obj in self.objects
                                if obj["handle"] in criteria or obj["name"] in criteria
                            }
                        else:
                            selected = set(self.selection)
                        deleted = len(selected)
                        self.objects = [obj for obj in self.objects if obj["handle"] not in selected]
                        self.selection = []
                        _write_json_frame(
                            conn,
                            {"id": request_id, "success": True, "result": "Deleted {0} objects".format(deleted)},
                        )
                    else:
                        _write_json_frame(
                            conn,
                            {
                                "id": request_id,
                                "success": False,
                                "error": "Mock bridge only implements selection get/clear/select/delete",
                            },
                        )
                elif action == "create_object":
                    params = request.get("params", {})
                    self.created_count += 1
                    handle = "mock-created-{0}".format(self.created_count)
                    name = params.get("name") or "Mock Created {0}".format(self.created_count)
                    object_type = params.get("object_type") or params.get("type") or "rect"
                    if object_type == "dimension":
                        object_type = "linear_dimension"
                    self.objects.append(
                        {
                            "handle": handle,
                            "type": object_type,
                            "name": name,
                            "uuid": "mock-uuid-{0}".format(self.created_count),
                            "bounds": _mock_bounds(params, object_type),
                        }
                    )
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": {"type": object_type, "handle": handle, "uuid": "mock-uuid-{0}".format(self.created_count)},
                        },
                    )
                elif action in ("create_wall", "create_text", "create_linear_dimension"):
                    params = request.get("params", {})
                    self.created_count += 1
                    handle = "mock-created-{0}".format(self.created_count)
                    object_type = {
                        "create_wall": "wall",
                        "create_text": "text",
                        "create_linear_dimension": "linear_dimension",
                    }[action]
                    name = params.get("name") or "Mock Created {0}".format(self.created_count)
                    self.objects.append(
                        {
                            "handle": handle,
                            "type": object_type,
                            "name": name,
                            "uuid": "mock-uuid-{0}".format(self.created_count),
                            "bounds": _mock_bounds(params, object_type),
                        }
                    )
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": {"type": object_type, "handle": handle, "uuid": "mock-uuid-{0}".format(self.created_count)},
                        },
                    )
                elif action == "batch_create_objects":
                    params = request.get("params", {})
                    created = []
                    object_count = int(params.get("object_count", 0))
                    if object_count < 1:
                        _write_json_frame(
                            conn,
                            {"id": request_id, "success": False, "error": "object_count must be >= 1"},
                        )
                        continue
                    for index in range(1, object_count + 1):
                        object_params = json.loads(params.get("object_{0}_json".format(index), "{}"))
                        self.created_count += 1
                        handle = "mock-created-{0}".format(self.created_count)
                        name = object_params.get("name") or "Mock Created {0}".format(self.created_count)
                        object_type = object_params.get("object_type") or object_params.get("type") or "rect"
                        if object_type in ("rectangle", "box"):
                            object_type = "rect"
                        if object_type == "dimension":
                            object_type = "linear_dimension"
                        self.objects.append(
                            {
                                "handle": handle,
                                "type": object_type,
                                "name": name,
                                "uuid": "mock-uuid-{0}".format(self.created_count),
                                "bounds": _mock_bounds(object_params, object_type),
                            }
                        )
                        created.append({"index": index, "type": object_type, "handle": handle, "uuid": "mock-uuid-{0}".format(self.created_count)})
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": {
                                "atomic": True,
                                "rollback_on_error": True,
                                "created_count": len(created),
                                "created": created,
                            },
                        },
                    )
                elif action == "set_property":
                    params = request.get("params", {})
                    handle = str(params.get("handle", ""))
                    property_name = str(params.get("property_name", ""))
                    value = str(params.get("value", ""))
                    target = next((obj for obj in self.objects if isinstance(obj, dict) and str(obj.get("handle")) == handle), None)
                    if target is None:
                        _write_json_frame(conn, {"id": request_id, "success": False, "error": "handle not found"})
                        continue
                    if property_name not in {"name", "class", "fillColor", "penColor", "lineWeight", "opacity"}:
                        _write_json_frame(conn, {"id": request_id, "success": False, "error": "unsupported property"})
                        continue
                    before = dict(target)
                    if property_name in {"lineWeight", "opacity"}:
                        try:
                            parsed = int(value)
                        except ValueError:
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": "property must be integer"})
                            continue
                        max_value = 32767 if property_name == "lineWeight" else 100
                        if parsed < 0 or parsed > max_value:
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": f"{property_name} must be between 0 and {max_value}"})
                            continue
                        target[property_name] = parsed
                    elif property_name in {"fillColor", "penColor"}:
                        parts = value.split(",")
                        try:
                            components = [int(part.strip()) for part in parts]
                        except ValueError:
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": "color must be r,g,b with components in 0..65535"})
                            continue
                        if len(components) != 3 or any(component < 0 or component > 65535 for component in components):
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": "color must be r,g,b with components in 0..65535"})
                            continue
                        target[property_name] = ",".join(str(component) for component in components)
                    elif property_name == "class":
                        if not value:
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": "class value is required"})
                            continue
                        target[property_name] = value
                        self.classes.add(value)
                        target["class_name"] = value
                    else:
                        target[property_name] = value
                    _write_json_frame(
                        conn,
                        {
                            "id": request_id,
                            "success": True,
                            "result": {
                                "changed": True,
                                "handle": handle,
                                "property_name": property_name,
                                "value": value,
                                "before": before,
                                "after": dict(target),
                            },
                        },
                    )
                elif action == "manage_classes":
                    params = request.get("params", {})
                    class_action = str(params.get("action", "list")).lower()
                    class_name = str(params.get("class_name", ""))
                    if class_action == "list":
                        _write_json_frame(conn, {"id": request_id, "success": True, "result": sorted(self.classes)})
                    elif class_action == "create":
                        if not class_name:
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": "class_name is required"})
                            continue
                        existed = class_name in self.classes
                        self.classes.add(class_name)
                        _write_json_frame(
                            conn,
                            {
                                "id": request_id,
                                "success": True,
                                "result": {"action": "create", "class_name": class_name, "created": not existed, "existed": existed},
                            },
                        )
                    elif class_action == "delete":
                        if params.get("confirm") != "DELETE_CLASS":
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": "class deletion requires confirm='DELETE_CLASS'"})
                            continue
                        if class_name == "None":
                            _write_json_frame(conn, {"id": request_id, "success": False, "error": "refusing to delete the None class"})
                            continue
                        self.classes.discard(class_name)
                        _write_json_frame(
                            conn,
                            {"id": request_id, "success": True, "result": {"action": "delete", "class_name": class_name, "deleted": True}},
                        )
                    else:
                        _write_json_frame(conn, {"id": request_id, "success": False, "error": "unknown class action"})
                elif action == "stop":
                    _write_json_frame(conn, {"id": request_id, "success": True, "result": "Mock bridge stop requested"})
                    if self.release_on_stop:
                        self.stop()
                    return
                else:
                    _write_json_frame(
                        conn,
                        {"id": request_id, "success": False, "error": "Mock handler not implemented: {0}".format(action)},
                    )
