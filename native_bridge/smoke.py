import argparse
import json
import os
from pathlib import Path
import re
import socket
import struct
import sys
import time
from typing import Any


PHASE_ZERO_MIN_HANDLER_COUNT = 2
PHASE_ONE_MIN_HANDLER_COUNT = 8
PHASE_TWO_MIN_HANDLER_COUNT = 13
PHASE_ONE_REQUIRED_ACTIONS = {
    "ping",
    "stop",
    "get_document_info",
    "get_layers",
    "get_objects",
    "selection",
    "create_object",
    "batch_create_objects",
}
PHASE_TWO_REQUIRED_ACTIONS = PHASE_ONE_REQUIRED_ACTIONS | {
    "create_wall",
    "create_text",
    "create_linear_dimension",
    "set_property",
    "manage_classes",
}
PHASE_ONE_READ_ACTIONS = ("get_document_info", "get_layers", "get_objects", "selection")
UNSAFE_DISPATCH_MODES = {"background", "foreground", "win_timer"}
UNSAFE_BRIDGE_KINDS = {"python_foreground_diagnostic", "python_transport_only"}
NATIVE_DISPATCH_MODES = {"native_sdk"}
NATIVE_BRIDGE_KIND_PREFIXES = ("native_sdk_bridge",)
AUTH_TOKEN = ""


def _default_auth_token_file() -> Path:
    configured = os.environ.get("VW_MCP_AUTH_TOKEN_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    stop_dir = os.environ.get("VW_MCP_STOP_DIR", "").strip()
    if stop_dir:
        return Path(stop_dir).expanduser() / "auth-token"
    userprofile = os.environ.get("USERPROFILE", "").strip()
    if userprofile:
        return Path(userprofile) / ".vectorworks-mcp" / "auth-token"
    return Path.home() / ".vectorworks-mcp" / "auth-token"


def _load_auth_token(explicit: str = "") -> str:
    if explicit:
        return explicit
    env_token = os.environ.get("VW_MCP_AUTH_TOKEN", "").strip()
    if env_token:
        return env_token
    try:
        path = _default_auth_token_file()
        if path.is_file():
            return path.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("bridge closed the connection before sending a full frame")
        data.extend(chunk)
    return bytes(data)


def _read_frame(sock: socket.socket) -> dict[str, Any]:
    header = _read_exact(sock, 4)
    (size,) = struct.unpack(">I", header)
    if size <= 0 or size > 16 * 1024 * 1024:
        raise RuntimeError("invalid bridge frame length {0}".format(size))
    return json.loads(_read_exact(sock, size).decode("utf-8"))


def _write_frame(sock: socket.socket, payload: dict[str, Any]) -> None:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sock.sendall(struct.pack(">I", len(data)) + data)


def _call(
    sock: socket.socket,
    action: str,
    params: dict[str, Any] | None,
    request_id: str,
    auth_token: str = "",
) -> dict[str, Any]:
    request = {"id": request_id, "action": action, "params": params or {}}
    if not auth_token:
        auth_token = AUTH_TOKEN
    if auth_token:
        request["auth_token"] = auth_token
    _write_frame(sock, request)
    response = _read_frame(sock)
    if not isinstance(response, dict):
        raise RuntimeError("bridge response for {0} was not an object".format(action))
    if response.get("id") != request_id:
        raise RuntimeError("bridge response id mismatch for {0}".format(action))
    return response


def _record_call(
    sock: socket.socket,
    report: dict[str, Any],
    action: str,
    iteration: int | str,
    params: dict[str, Any] | None = None,
    latency_budget_ms: float | None = None,
    latency_budget_label: str = "latency",
    auth_token: str = "",
) -> dict[str, Any] | None:
    started = time.perf_counter()
    request_id = "{0}-{1}".format(action, iteration)
    try:
        response = _call(sock, action, params, request_id, auth_token=auth_token)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        success = response.get("success")
        check = {
            "action": action,
            "iteration": iteration,
            "ok": False,
            "elapsed_ms": elapsed_ms,
        }
        if success is True:
            if "result" not in response:
                check["error"] = "bridge success response for {0} did not include result".format(action)
                report["failures"].append(check["error"])
                report["checks"].append(check)
                return None
            check["ok"] = True
            if latency_budget_ms is not None and elapsed_ms > latency_budget_ms:
                check["ok"] = False
                check["error"] = (
                    "{0} iteration {1} latency {2:.2f}ms exceeded {3} budget {4:g}ms"
                ).format(action, iteration, elapsed_ms, latency_budget_label, latency_budget_ms)
                report["failures"].append(check["error"])
            report["checks"].append(check)
            return response
        if success is False:
            error = response.get("error")
            if not isinstance(error, str) or not error.strip():
                error = "bridge failure response for {0} did not include a non-empty error string".format(action)
            check["error"] = error
            report["failures"].append(check["error"])
            report["checks"].append(check)
            return None
        check["error"] = "bridge response success for {0} was not boolean true/false".format(action)
        report["failures"].append(check["error"])
        report["checks"].append(check)
        return None
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        check = {
            "action": action,
            "iteration": iteration,
            "ok": False,
            "elapsed_ms": elapsed_ms,
            "error": str(exc),
        }
        report["checks"].append(check)
        report["failures"].append(str(exc))
        return None


def _validate_ping(report: dict[str, Any], result: Any, require_native: bool, phase: int) -> None:
    if not isinstance(result, dict):
        report["failures"].append("ping result was not an object")
        return

    report["last_ping"] = result
    if result.get("pong") is not True:
        report["failures"].append("ping did not return pong=true")
    if not isinstance(result.get("version"), str) or not result.get("version", "").strip():
        report["failures"].append("ping version was not a non-empty string")
    bridge_kind = str(result.get("bridge_kind", "") or "")
    dispatch_mode = str(result.get("dispatch_mode", "") or "")
    bridge_kind_normalized = bridge_kind.strip().lower()
    dispatch_mode_normalized = dispatch_mode.strip().lower()
    if not isinstance(result.get("bridge_kind"), str) or not bridge_kind_normalized:
        report["failures"].append("ping bridge_kind was not a non-empty string")
    if not isinstance(result.get("dispatch_mode"), str) or not dispatch_mode_normalized:
        report["failures"].append("ping dispatch_mode was not a non-empty string")
    if dispatch_mode_normalized in UNSAFE_DISPATCH_MODES:
        report["failures"].append("ping dispatch_mode reported unsafe mode {0}".format(dispatch_mode))
    if bridge_kind_normalized in UNSAFE_BRIDGE_KINDS:
        report["failures"].append("ping bridge_kind reported unsafe bridge {0}".format(bridge_kind))
    if require_native and dispatch_mode_normalized not in NATIVE_DISPATCH_MODES:
        report["failures"].append("native bridge dispatch_mode was not native_sdk")
    if require_native and not bridge_kind_normalized.startswith(NATIVE_BRIDGE_KIND_PREFIXES):
        report["failures"].append("native bridge bridge_kind did not start with native_sdk_bridge")
    handlers = result.get("handlers")
    min_handlers = PHASE_ZERO_MIN_HANDLER_COUNT
    if phase >= 2:
        min_handlers = PHASE_TWO_MIN_HANDLER_COUNT
    elif phase >= 1:
        min_handlers = PHASE_ONE_MIN_HANDLER_COUNT
    if (
        not isinstance(handlers, int)
        or isinstance(handlers, bool)
        or handlers < min_handlers
    ):
        report["failures"].append(
            "ping handlers was not an integer >= {0}".format(min_handlers)
        )
    if phase >= 1:
        implemented_actions = result.get("implemented_actions")
        if not isinstance(implemented_actions, list) or not all(isinstance(action, str) for action in implemented_actions):
            report["failures"].append("ping implemented_actions was not a list of strings")
        else:
            required_actions = PHASE_TWO_REQUIRED_ACTIONS if phase >= 2 else PHASE_ONE_REQUIRED_ACTIONS
            missing_actions = sorted(required_actions - set(implemented_actions))
            if missing_actions:
                report["failures"].append(
                    "ping implemented_actions missing phase-{0} action(s): {1}".format(phase, ", ".join(missing_actions))
                )
        native_phase = result.get("native_phase")
        if not isinstance(native_phase, int) or isinstance(native_phase, bool) or native_phase < phase:
            report["failures"].append("ping native_phase was not an integer >= {0}".format(phase))
        if result.get("main_context_pump") != "win32_ui_timer":
            report["failures"].append("ping main_context_pump was not win32_ui_timer")
        if result.get("main_context_pump_ready") is not True:
            report["failures"].append("ping main_context_pump_ready was not true")
    cad_api_safe = result.get("cad_api_safe")
    transport_only = result.get("transport_only")
    if phase >= 1:
        if cad_api_safe is not True:
            report["failures"].append("bridge did not report cad_api_safe=true")
        if transport_only is not False:
            report["failures"].append("bridge did not report transport_only=false")
    else:
        if cad_api_safe not in (True, False):
            report["failures"].append("bridge cad_api_safe was not boolean")
        if transport_only not in (True, False):
            report["failures"].append("bridge transport_only was not boolean")
        if cad_api_safe is True and transport_only is not False:
            report["failures"].append("CAD-safe phase-0 bridge must report transport_only=false")
        if cad_api_safe is False and transport_only is not True:
            report["failures"].append("transport-only phase-0 bridge must report transport_only=true")
    if require_native and result.get("native_bridge") is not True:
        report["failures"].append("bridge did not report native_bridge=true")


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_bounds(report: dict[str, Any], action: str, index: int, bounds: Any) -> None:
    if bounds is None:
        return
    if not isinstance(bounds, dict):
        report["failures"].append("{0} object {1} bounds was not an object or null".format(action, index))
        return
    for key in ("top_left", "bottom_right"):
        point = bounds.get(key)
        if not isinstance(point, list) or len(point) != 2 or not all(_is_number(coord) for coord in point):
            report["failures"].append(
                "{0} object {1} bounds.{2} was not a two-number list".format(action, index, key)
            )


def _validate_object_list(
    report: dict[str, Any],
    action: str,
    result: Any,
    params: dict[str, Any] | None = None,
) -> bool:
    valid = True
    params = params or {}
    if not isinstance(result, list):
        report["failures"].append("{0} result was not a list".format(action))
        return False

    limit = params.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and len(result) > limit:
        report["failures"].append("{0} returned more objects than requested limit {1}".format(action, limit))
        valid = False
    object_type = str(params.get("object_type", "") or "").lower()
    layer = str(params.get("layer", "") or "")

    for index, obj in enumerate(result):
        if not isinstance(obj, dict):
            report["failures"].append("{0} item {1} was not an object".format(action, index))
            valid = False
            continue
        if not _is_non_empty_string(obj.get("handle")):
            report["failures"].append("{0} item {1} handle was not a non-empty string".format(action, index))
            valid = False
        obj_type = str(obj.get("type", "") or "")
        if not _is_non_empty_string(obj.get("type")):
            report["failures"].append("{0} item {1} type was not a non-empty string".format(action, index))
            valid = False
        elif object_type and obj_type.lower() != object_type:
            report["failures"].append(
                "{0} item {1} type did not match requested object_type {2}".format(action, index, object_type)
            )
            valid = False
        if layer and obj.get("layer") != layer:
            report["failures"].append("{0} item {1} layer did not match requested layer".format(action, index))
            valid = False
        if "type_id" in obj and not _is_non_negative_int(obj.get("type_id")):
            report["failures"].append("{0} item {1} type_id was not a non-negative integer".format(action, index))
            valid = False
        if "name" in obj and not isinstance(obj.get("name"), str):
            report["failures"].append("{0} item {1} name was not a string".format(action, index))
            valid = False
        before = len(report["failures"])
        _validate_bounds(report, action, index, obj.get("bounds"))
        if len(report["failures"]) > before:
            valid = False
    return valid


def _validate_read_result(
    report: dict[str, Any],
    action: str,
    result: Any,
    params: dict[str, Any] | None = None,
) -> None:
    if action == "get_document_info":
        if not isinstance(result, dict):
            report["failures"].append("get_document_info result was not an object")
            return
        if not _is_non_empty_string(result.get("filename")):
            report["failures"].append("get_document_info filename was not a non-empty string")
        if "filepath" in result and not isinstance(result.get("filepath"), str):
            report["failures"].append("get_document_info filepath was not a string")
        layers = result.get("layers")
        if not isinstance(layers, list) or not all(isinstance(name, str) for name in layers):
            report["failures"].append("get_document_info layers was not a list of strings")
        if not _is_non_negative_int(result.get("layer_count")):
            report["failures"].append("get_document_info layer_count was not a non-negative integer")
        elif isinstance(layers, list) and result.get("layer_count") != len(layers):
            report["failures"].append("get_document_info layer_count did not match layers length")
        if not _is_non_negative_int(result.get("total_objects")):
            report["failures"].append("get_document_info total_objects was not a non-negative integer")
        return

    if action == "get_layers":
        if not isinstance(result, list):
            report["failures"].append("get_layers result was not a list")
            return
        for index, layer in enumerate(result):
            if not isinstance(layer, dict):
                report["failures"].append("get_layers item {0} was not an object".format(index))
                continue
            if not _is_non_empty_string(layer.get("name")):
                report["failures"].append("get_layers item {0} name was not a non-empty string".format(index))
            if "visible" in layer and not isinstance(layer.get("visible"), bool):
                report["failures"].append("get_layers item {0} visible was not a boolean".format(index))
        return

    if action == "get_objects":
        _validate_object_list(report, action, result, params=params)
        return

    if action == "selection":
        if not isinstance(params, dict) or params.get("action") != "get":
            report["failures"].append("selection read smoke must use action=get")
            return
        _validate_object_list(report, "selection get", result)


def _object_matches_fixture(obj: Any, fixture_name: str, fixture_handle: str | None = None) -> bool:
    if not isinstance(obj, dict):
        return False
    if fixture_handle and obj.get("handle") == fixture_handle:
        return True
    return obj.get("name") == fixture_name


def _validate_fixture_present(
    report: dict[str, Any],
    result: Any,
    fixture_name: str,
    fixture_handle: str | None = None,
    object_type: str | None = "rect",
) -> bool:
    params = {"limit": 200} if object_type else {}
    if object_type:
        params["object_type"] = object_type
    if not _validate_object_list(report, "fixture object check", result, params=params):
        return False
    if not any(_object_matches_fixture(obj, fixture_name, fixture_handle) for obj in result):
        report["failures"].append("created fixture object was not visible in get_objects")
        return False
    return True


def _validate_fixture_absent(
    report: dict[str, Any],
    result: Any,
    fixture_name: str,
    fixture_handle: str | None = None,
    object_type: str | None = "rect",
) -> bool:
    params = {"limit": 200} if object_type else {}
    if object_type:
        params["object_type"] = object_type
    if not _validate_object_list(report, "fixture cleanup check", result, params=params):
        return False
    if any(_object_matches_fixture(obj, fixture_name, fixture_handle) for obj in result):
        report["failures"].append("created fixture object remained after cleanup")
        return False
    return True


def _validate_fixture_selected(
    report: dict[str, Any],
    result: Any,
    fixture_name: str,
    fixture_handle: str | None = None,
) -> bool:
    if not _validate_object_list(report, "selection get", result):
        return False
    if not result:
        report["failures"].append("fixture object was not selected")
        return False
    fixture_matches = [obj for obj in result if _object_matches_fixture(obj, fixture_name, fixture_handle)]
    if not fixture_matches:
        report["failures"].append("fixture object was not selected")
        return False
    return True


def _validate_fixture_selected_any_type(
    report: dict[str, Any],
    result: Any,
    fixture_name: str,
    fixture_handle: str | None = None,
) -> bool:
    if not _validate_object_list(report, "selection get", result):
        return False
    if not result:
        report["failures"].append("phase-2 fixture object was not selected")
        return False
    fixture_matches = [obj for obj in result if _object_matches_fixture(obj, fixture_name, fixture_handle)]
    if not fixture_matches:
        report["failures"].append("phase-2 fixture object was not selected")
        return False
    if len(fixture_matches) < 1:
        report["failures"].append("selection did not include the phase-2 fixture object; refusing cleanup delete")
        return False
    return True


def _extract_created_handle(result: Any) -> str | None:
    if isinstance(result, dict):
        handle = result.get("handle")
        if handle:
            return str(handle)
        created = result.get("created")
        if isinstance(created, list) and created and isinstance(created[0], dict):
            handle = created[0].get("handle")
            return str(handle) if handle else None
    if isinstance(result, str):
        match = re.search(r"handle:\s*([^\s,;]+)", result)
        if match:
            return match.group(1)
    return None


def _cleanup_fixture_by_exact_name(
    sock: socket.socket,
    report: dict[str, Any],
    label: str,
    fixture_name: str,
    fixture_handle: str | None = None,
) -> bool:
    selection_cleared = _record_call(sock, report, "selection", "{0}-clear".format(label), params={"action": "clear"}) is not None
    selection_select_sent = _record_call(
        sock,
        report,
        "selection",
        "{0}-select".format(label),
        params={"action": "select", "criteria": "((N='{0}'))".format(fixture_name)},
    ) is not None
    selection_response = _record_call(sock, report, "selection", "{0}-get".format(label), params={"action": "get"})
    fixture_selected = False
    if selection_response is not None:
        fixture_selected = _validate_fixture_selected_any_type(report, selection_response.get("result"), fixture_name, fixture_handle)

    if not (selection_cleared and selection_select_sent and fixture_selected):
        report["failures"].append("skipped {0} delete because exact-name fixture selection was not proven".format(label))
        _record_call(sock, report, "selection", "{0}-clear-after-skip".format(label), params={"action": "clear"})
        return False

    delete_response = _record_call(
        sock,
        report,
        "selection",
        "{0}-delete".format(label),
        params={
            "action": "delete",
            "criteria": "((N='{0}'))".format(fixture_name),
            "confirm": "DELETE_EXACT_NAME",
        },
    )
    if delete_response is not None:
        _validate_fixture_delete_result(report, delete_response.get("result"))
    cleanup_response = _record_call(sock, report, "get_objects", "{0}-cleanup".format(label), params={"limit": 500})
    if cleanup_response is not None:
        _validate_fixture_absent(report, cleanup_response.get("result"), fixture_name, fixture_handle, object_type=None)
    return True


def _run_phase_two_write_fixture(sock: socket.socket, report: dict[str, Any]) -> None:
    fixture_prefix = "VW_MCP_NATIVE_PHASE2_SMOKE_{0}".format(int(time.time() * 1000))
    class_fixture = fixture_prefix + "_CLASS"
    create_class_response = _record_call(
        sock,
        report,
        "manage_classes",
        "class-fixture-create",
        params={"action": "create", "class_name": class_fixture},
    )
    if create_class_response is not None:
        create_result = create_class_response.get("result")
        if not isinstance(create_result, dict) or create_result.get("class_name") != class_fixture:
            report["failures"].append("manage_classes create fixture did not return the class name")
        list_response = _record_call(
            sock,
            report,
            "manage_classes",
            "class-fixture-list",
            params={"action": "list"},
        )
        if list_response is not None:
            list_result = list_response.get("result")
            if not isinstance(list_result, list) or class_fixture not in list_result:
                report["failures"].append("manage_classes list did not include the created fixture class")
        delete_response = _record_call(
            sock,
            report,
            "manage_classes",
            "class-fixture-delete",
            params={"action": "delete", "class_name": class_fixture, "confirm": "DELETE_CLASS"},
        )
        if delete_response is not None:
            delete_result = delete_response.get("result")
            if not isinstance(delete_result, dict) or delete_result.get("deleted") is not True:
                report["failures"].append("manage_classes delete fixture did not report deleted=true")

    property_fixture = fixture_prefix + "_PROPERTY"
    property_fixture_renamed = fixture_prefix + "_PROPERTY_EDITED"
    property_response = _record_call(
        sock,
        report,
        "create_object",
        "property-fixture-create",
        params={
            "object_type": "rect",
            "x1": 0,
            "y1": 1200,
            "x2": 300,
            "y2": 1500,
            "name": property_fixture,
        },
    )
    if property_response is not None:
        property_result = property_response.get("result")
        if not isinstance(property_result, dict):
            report["failures"].append("property fixture create_object result was not an object")
        else:
            property_handle = _extract_created_handle(property_result)
            if not property_handle:
                report["failures"].append("property fixture create_object result did not include a handle")
            else:
                set_property_response = _record_call(
                    sock,
                    report,
                    "set_property",
                    "property-fixture-set-name",
                    params={"handle": property_handle, "property_name": "name", "value": property_fixture_renamed},
                )
                cleanup_name = property_fixture
                if set_property_response is not None:
                    set_property_result = set_property_response.get("result")
                    if not isinstance(set_property_result, dict) or set_property_result.get("changed") is not True:
                        report["failures"].append("set_property fixture did not report changed=true")
                    else:
                        cleanup_name = property_fixture_renamed
                    readback_response = _record_call(
                        sock,
                        report,
                        "get_objects",
                        "property-fixture-readback",
                        params={"limit": 500},
                    )
                    if readback_response is not None:
                        readback_result = readback_response.get("result")
                        if _validate_object_list(report, "set_property readback", readback_result):
                            renamed_matches = [
                                obj
                                for obj in readback_result
                                if isinstance(obj, dict)
                                and obj.get("handle") == property_handle
                                and obj.get("name") == property_fixture_renamed
                            ]
                            stale_matches = [
                                obj
                                for obj in readback_result
                                if isinstance(obj, dict)
                                and obj.get("handle") == property_handle
                                and obj.get("name") == property_fixture
                            ]
                            if not renamed_matches:
                                report["failures"].append("set_property readback did not include the renamed fixture")
                            if stale_matches:
                                report["failures"].append("set_property readback still showed the original fixture name")
                cleaned_property_fixture = _cleanup_fixture_by_exact_name(sock, report, "property-fixture", cleanup_name, property_handle)
                if not cleaned_property_fixture and cleanup_name != property_fixture:
                    _cleanup_fixture_by_exact_name(sock, report, "property-fixture-original-name", property_fixture, property_handle)

    fixtures = [
        (
            "create_wall",
            "wall",
            {
                "start_x": 0,
                "start_y": 500,
                "end_x": 1200,
                "end_y": 500,
                "height": 3000,
                "thickness": 200,
                "name": fixture_prefix + "_WALL",
            },
        ),
        (
            "create_text",
            "text",
            {
                "text": "MCP smoke label",
                "x1": 0,
                "y1": 800,
                "text_size": 10,
                "name": fixture_prefix + "_TEXT",
            },
        ),
        (
            "create_linear_dimension",
            "linear_dimension",
            {
                "start_x": 0,
                "start_y": 1000,
                "end_x": 1200,
                "end_y": 1000,
                "offset": 250,
                "name": fixture_prefix + "_DIM",
            },
        ),
    ]

    for action, expected_type, params in fixtures:
        name = str(params["name"])
        response = _record_call(sock, report, action, "phase2-fixture", params=params)
        if response is None:
            report["failures"].append("skipped {0} cleanup because creation did not succeed".format(action))
            continue
        result = response.get("result")
        if not isinstance(result, dict):
            report["failures"].append("{0} result was not an object".format(action))
            continue
        if result.get("type") != expected_type:
            report["failures"].append("{0} result type was not {1}".format(action, expected_type))
        handle = _extract_created_handle(result)
        if not handle:
            report["failures"].append("{0} result did not include a handle".format(action))
            continue

        objects_response = _record_call(sock, report, "get_objects", "{0}-present".format(action), params={"limit": 500})
        fixture_present = False
        if objects_response is not None:
            fixture_present = _validate_fixture_present(report, objects_response.get("result"), name, handle, object_type=None)

        selection_cleared = _record_call(sock, report, "selection", "{0}-clear".format(action), params={"action": "clear"}) is not None
        selection_select_sent = _record_call(
            sock,
            report,
            "selection",
            "{0}-select".format(action),
            params={"action": "select", "criteria": "((N='{0}'))".format(name)},
        ) is not None
        selection_response = _record_call(sock, report, "selection", "{0}-get".format(action), params={"action": "get"})
        fixture_selected = False
        if selection_response is not None:
            fixture_selected = _validate_fixture_selected_any_type(report, selection_response.get("result"), name, handle)

        if not (fixture_present and selection_cleared and selection_select_sent and fixture_selected):
            report["failures"].append("skipped {0} delete because exact-name fixture selection was not proven".format(action))
            _record_call(sock, report, "selection", "{0}-clear-after-skip".format(action), params={"action": "clear"})
            continue

        delete_response = _record_call(
            sock,
            report,
            "selection",
            "{0}-delete".format(action),
            params={
                "action": "delete",
                "criteria": "((N='{0}'))".format(name),
                "confirm": "DELETE_EXACT_NAME",
            },
        )
        if delete_response is not None:
            _validate_fixture_delete_result(report, delete_response.get("result"))
        cleanup_response = _record_call(sock, report, "get_objects", "{0}-cleanup".format(action), params={"limit": 500})
        if cleanup_response is not None:
            _validate_fixture_absent(report, cleanup_response.get("result"), name, handle, object_type=None)


def _validate_batch_create_result(report: dict[str, Any], result: Any) -> bool:
    if not isinstance(result, dict):
        report["failures"].append("batch_create_objects fixture result was not an object")
        return False
    if result.get("atomic") is not True:
        report["failures"].append("batch_create_objects fixture result did not report atomic=true")
        return False
    if result.get("created_count") != 1:
        report["failures"].append("batch_create_objects fixture did not report creating exactly one object")
        return False
    created = result.get("created")
    if not isinstance(created, list) or len(created) != 1 or not isinstance(created[0], dict):
        report["failures"].append("batch_create_objects fixture result did not include one created entry")
        return False
    if not _is_non_empty_string(created[0].get("handle")):
        report["failures"].append("batch_create_objects fixture created entry did not include a handle")
        return False
    return True


def _validate_fixture_delete_result(report: dict[str, Any], result: Any) -> bool:
    deleted_count = None
    if isinstance(result, dict):
        for key in ("deleted", "deleted_count", "count"):
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                deleted_count = value
                break
    elif isinstance(result, str):
        match = re.search(r"Deleted\s+(\d+)\s+objects?", result, flags=re.IGNORECASE)
        if match:
            deleted_count = int(match.group(1))

    if deleted_count is None or deleted_count < 1:
        report["failures"].append("fixture delete result did not report deleting at least one object")
        return False
    return True


def _run_phase_one_write_fixture(sock: socket.socket, report: dict[str, Any]) -> None:
    fixture_name = "VW_MCP_NATIVE_SMOKE_{0}".format(int(time.time() * 1000))
    create_response = _record_call(
        sock,
        report,
        "create_object",
        "fixture",
        params={
            "object_type": "rect",
            "x1": 0,
            "y1": 0,
            "x2": 100,
            "y2": 100,
            "name": fixture_name,
        },
    )
    fixture_handle = _extract_created_handle(create_response.get("result")) if create_response else None
    if create_response is None:
        report["failures"].append("skipped fixture cleanup because fixture creation did not succeed")
        return
    if not fixture_handle:
        report["failures"].append("create_object fixture result did not include a handle")
        report["failures"].append("skipped fixture cleanup because fixture creation did not return a handle")
        return
    fixture_present = False
    fixture_selected = False
    selection_cleared = False
    selection_select_sent = False

    objects_response = _record_call(
        sock,
        report,
        "get_objects",
        "fixture-present",
        params={"limit": 200, "object_type": "rect"},
    )
    if objects_response is not None:
        fixture_present = _validate_fixture_present(report, objects_response.get("result"), fixture_name, fixture_handle)

    selection_cleared = _record_call(sock, report, "selection", "fixture-clear", params={"action": "clear"}) is not None
    selection_select_sent = _record_call(
        sock,
        report,
        "selection",
        "fixture-select",
        params={"action": "select", "criteria": "((N='{0}'))".format(fixture_name)},
    )
    selection_response = _record_call(sock, report, "selection", "fixture-get", params={"action": "get"})
    if selection_response is not None:
        fixture_selected = _validate_fixture_selected(report, selection_response.get("result"), fixture_name, fixture_handle)

    if not (create_response is not None and fixture_present and selection_cleared and selection_select_sent and fixture_selected):
        report["failures"].append("skipped fixture delete because fixture selection was not proven safe")
        _record_call(sock, report, "selection", "fixture-clear-after-skip", params={"action": "clear"})
        return

    delete_response = _record_call(
        sock,
        report,
        "selection",
        "fixture-delete",
        params={
            "action": "delete",
            "criteria": "((N='{0}'))".format(fixture_name),
            "confirm": "DELETE_EXACT_NAME",
        },
    )
    if delete_response is None:
        return
    _validate_fixture_delete_result(report, delete_response.get("result"))

    cleanup_response = _record_call(
        sock,
        report,
        "get_objects",
        "fixture-cleanup",
        params={"limit": 200, "object_type": "rect"},
    )
    if cleanup_response is not None:
        _validate_fixture_absent(report, cleanup_response.get("result"), fixture_name, fixture_handle)

    batch_fixture_name = "VW_MCP_NATIVE_BATCH_SMOKE_{0}".format(int(time.time() * 1000))
    batch_response = _record_call(
        sock,
        report,
        "batch_create_objects",
        "atomic-fixture",
        params={
            "object_count": 1,
            "object_1_json": json.dumps(
                {
                    "object_type": "rect",
                    "x1": 200,
                    "y1": 0,
                    "x2": 300,
                    "y2": 100,
                    "name": batch_fixture_name,
                },
                separators=(",", ":"),
                sort_keys=True,
            ),
        },
    )
    batch_handle = _extract_created_handle(batch_response.get("result")) if batch_response else None
    if batch_response is None or not _validate_batch_create_result(report, batch_response.get("result")) or not batch_handle:
        report["failures"].append("skipped atomic batch fixture cleanup because creation did not return a verified handle")
        return

    batch_objects_response = _record_call(
        sock,
        report,
        "get_objects",
        "batch-fixture-present",
        params={"limit": 200, "object_type": "rect"},
    )
    batch_fixture_present = False
    if batch_objects_response is not None:
        batch_fixture_present = _validate_fixture_present(
            report,
            batch_objects_response.get("result"),
            batch_fixture_name,
            batch_handle,
        )

    batch_selection_cleared = _record_call(sock, report, "selection", "batch-fixture-clear", params={"action": "clear"}) is not None
    batch_selection_select_sent = _record_call(
        sock,
        report,
        "selection",
        "batch-fixture-select",
        params={"action": "select", "criteria": "((N='{0}'))".format(batch_fixture_name)},
    ) is not None
    batch_selection_response = _record_call(sock, report, "selection", "batch-fixture-get", params={"action": "get"})
    batch_fixture_selected = False
    if batch_selection_response is not None:
        batch_fixture_selected = _validate_fixture_selected(
            report,
            batch_selection_response.get("result"),
            batch_fixture_name,
            batch_handle,
        )

    if not (batch_fixture_present and batch_selection_cleared and batch_selection_select_sent and batch_fixture_selected):
        report["failures"].append("skipped atomic batch fixture delete because fixture selection was not proven safe")
        _record_call(sock, report, "selection", "batch-fixture-clear-after-skip", params={"action": "clear"})
        return

    batch_delete_response = _record_call(
        sock,
        report,
        "selection",
        "batch-fixture-delete",
        params={
            "action": "delete",
            "criteria": "((N='{0}'))".format(batch_fixture_name),
            "confirm": "DELETE_EXACT_NAME",
        },
    )
    if batch_delete_response is None:
        return
    _validate_fixture_delete_result(report, batch_delete_response.get("result"))

    batch_cleanup_response = _record_call(
        sock,
        report,
        "get_objects",
        "batch-fixture-cleanup",
        params={"limit": 200, "object_type": "rect"},
    )
    if batch_cleanup_response is not None:
        _validate_fixture_absent(report, batch_cleanup_response.get("result"), batch_fixture_name, batch_handle)


def _validate_phase_one_consistency(report: dict[str, Any], snapshots: dict[str, Any]) -> None:
    document_info = snapshots.get("get_document_info")
    layers = snapshots.get("get_layers")
    objects = snapshots.get("get_objects")

    if isinstance(document_info, dict) and isinstance(layers, list):
        info_layers = document_info.get("layers")
        layer_names = [
            layer.get("name")
            for layer in layers
            if isinstance(layer, dict) and isinstance(layer.get("name"), str)
        ]
        if isinstance(info_layers, list) and all(isinstance(name, str) for name in info_layers):
            if info_layers != layer_names:
                report["failures"].append("get_document_info layers did not match get_layers names")
        if _is_non_negative_int(document_info.get("layer_count")) and document_info["layer_count"] != len(layer_names):
            report["failures"].append("get_document_info layer_count did not match get_layers length")

    if isinstance(document_info, dict) and isinstance(objects, list):
        total_objects = document_info.get("total_objects")
        if _is_non_negative_int(total_objects) and total_objects < len(objects):
            report["failures"].append("get_document_info total_objects was less than returned get_objects count")


def _wait_for_port_closed(host: str, port: int, timeout: float) -> bool:
    deadline = time.time() + max(timeout, 0.1)
    closed_probe_count = 0
    while time.time() < deadline:
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.settimeout(min(0.2, max(deadline - time.time(), 0.01)))
            if probe.connect_ex((host, port)) != 0:
                closed_probe_count += 1
                if closed_probe_count >= 2:
                    return True
            else:
                closed_probe_count = 0
        finally:
            probe.close()
        time.sleep(0.05)
    return False


def _record_stop_port_release(report: dict[str, Any], released: bool, elapsed_ms: float) -> None:
    check = {
        "action": "stop",
        "iteration": "port-release",
        "ok": released,
        "elapsed_ms": round(elapsed_ms, 2),
    }
    if not released:
        check["error"] = "bridge port did not close after stop"
        report["failures"].append(check["error"])
    report["checks"].append(check)


def run_smoke(
    host: str = "127.0.0.1",
    port: int = 9877,
    timeout: float = 5.0,
    ping_count: int = 10,
    read_count: int = 10,
    require_native: bool = True,
    include_objects: bool = False,
    phase: int = 1,
    allow_write_fixture: bool = False,
    stop: bool = False,
    max_ping_ms: float | None = None,
    max_read_ms: float | None = None,
    auth_token: str = "",
) -> dict[str, Any]:
    global AUTH_TOKEN
    old_auth_token = AUTH_TOKEN
    AUTH_TOKEN = _load_auth_token(auth_token)
    report: dict[str, Any] = {
        "ok": False,
        "host": host,
        "port": port,
        "timeout_seconds": timeout,
        "ping_count": ping_count,
        "read_count": read_count,
        "require_native": require_native,
        "include_objects": include_objects,
        "phase": phase,
        "allow_write_fixture": allow_write_fixture,
        "stop_requested": stop,
        "max_ping_ms": max_ping_ms,
        "max_read_ms": max_read_ms,
        "stop_port_released": None,
        "checks": [],
        "failures": [],
    }

    if not _is_positive_int(ping_count):
        report["failures"].append("ping_count must be at least 1")
    if phase >= 1 and not _is_positive_int(read_count):
        report["failures"].append("read_count must be at least 1 for phase >= 1")
    if phase < 1 and allow_write_fixture:
        report["failures"].append("allow_write_fixture requires phase >= 1")
    if max_ping_ms is not None and max_ping_ms <= 0:
        report["failures"].append("max_ping_ms must be greater than 0")
    if max_read_ms is not None and max_read_ms <= 0:
        report["failures"].append("max_read_ms must be greater than 0")
    if report["failures"]:
        AUTH_TOKEN = old_auth_token
        return report

    stop_acknowledged = False
    phase_one_snapshots: dict[str, Any] = {}
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            for index in range(1, ping_count + 1):
                response = _record_call(
                    sock,
                    report,
                    "ping",
                    index,
                    latency_budget_ms=max_ping_ms,
                    latency_budget_label="ping latency",
                )
                if response is not None:
                    _validate_ping(report, response.get("result"), require_native=require_native, phase=phase)

            read_actions = []
            if phase >= 1:
                read_actions = list(PHASE_ONE_READ_ACTIONS)
            elif include_objects:
                read_actions.append("get_objects")

            for action in read_actions:
                for index in range(1, read_count + 1):
                    if action == "get_objects":
                        params = {"limit": 10}
                    elif action == "selection":
                        params = {"action": "get"}
                    else:
                        params = None
                    response = _record_call(
                        sock,
                        report,
                        action,
                        index,
                        params=params,
                        latency_budget_ms=max_read_ms,
                        latency_budget_label="read latency",
                    )
                    if response is not None:
                        result = response.get("result")
                        _validate_read_result(report, action, result, params=params)
                        if index == 1 and action not in phase_one_snapshots:
                            phase_one_snapshots[action] = result

            if phase_one_snapshots:
                _validate_phase_one_consistency(report, phase_one_snapshots)

            if phase >= 1 and allow_write_fixture:
                _run_phase_one_write_fixture(sock, report)
            if phase >= 2 and allow_write_fixture:
                _run_phase_two_write_fixture(sock, report)

            if stop:
                stop_acknowledged = _record_call(sock, report, "stop", 1) is not None
    except Exception as exc:
        report["failures"].append(str(exc))

    if stop:
        if stop_acknowledged:
            started = time.perf_counter()
            report["stop_port_released"] = _wait_for_port_closed(host, port, timeout)
            _record_stop_port_release(
                report,
                bool(report["stop_port_released"]),
                (time.perf_counter() - started) * 1000,
            )
        else:
            report["stop_port_released"] = False

    report["ok"] = len(report["failures"]) == 0
    AUTH_TOKEN = old_auth_token
    return report


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Smoke-test a Vectorworks native bridge over TCP.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9877)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--ping-count", type=int, default=10)
    parser.add_argument("--read-count", type=int, default=10)
    parser.add_argument("--allow-non-native", action="store_true")
    parser.add_argument("--include-objects", action="store_true")
    parser.add_argument("--phase", type=int, choices=(0, 1, 2), default=1)
    parser.add_argument("--allow-write-fixture", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--max-ping-ms", type=float, default=None)
    parser.add_argument("--max-read-ms", type=float, default=None)
    parser.add_argument("--auth-token", default="")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    report = run_smoke(
        host=args.host,
        port=args.port,
        timeout=args.timeout,
        ping_count=args.ping_count,
        read_count=args.read_count,
        require_native=not args.allow_non_native,
        include_objects=args.include_objects,
        phase=args.phase,
        allow_write_fixture=args.allow_write_fixture,
        stop=args.stop,
        max_ping_ms=args.max_ping_ms,
        max_read_ms=args.max_read_ms,
        auth_token=args.auth_token,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        status = "OK" if report["ok"] else "FAILED"
        print("Native bridge smoke test: {0}".format(status))
        print("Target: {0}:{1}".format(report["host"], report["port"]))
        print("Phase: {0}; write_fixture={1}".format(report["phase"], report["allow_write_fixture"]))
        print("Checks: {0}".format(len(report["checks"])))
        if report.get("last_ping"):
            ping = report["last_ping"]
            print(
                "Bridge: {0}; cad_api_safe={1}; native_bridge={2}; transport_only={3}; pump={4}; pump_ready={5}".format(
                    ping.get("bridge_kind", "unknown"),
                    ping.get("cad_api_safe"),
                    ping.get("native_bridge"),
                    ping.get("transport_only"),
                    ping.get("main_context_pump", "unknown"),
                    ping.get("main_context_pump_ready"),
                )
            )
        if report["stop_requested"]:
            print("Stop port released: {0}".format(report["stop_port_released"]))
        for failure in report["failures"]:
            print("ERROR: {0}".format(failure), file=sys.stderr)

    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
