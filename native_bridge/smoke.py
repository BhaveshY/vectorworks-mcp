import argparse
import json
import re
import socket
import struct
import sys
import time
from typing import Any


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


def _call(sock: socket.socket, action: str, params: dict[str, Any] | None, request_id: str) -> dict[str, Any]:
    _write_frame(sock, {"id": request_id, "action": action, "params": params or {}})
    response = _read_frame(sock)
    if response.get("id") != request_id:
        raise RuntimeError("bridge response id mismatch for {0}".format(action))
    return response


def _record_call(
    sock: socket.socket,
    report: dict[str, Any],
    action: str,
    iteration: int | str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    started = time.perf_counter()
    request_id = "{0}-{1}".format(action, iteration)
    try:
        response = _call(sock, action, params, request_id)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        ok = bool(response.get("success"))
        check = {
            "action": action,
            "iteration": iteration,
            "ok": ok,
            "elapsed_ms": elapsed_ms,
        }
        if not ok:
            check["error"] = str(response.get("error", "unknown bridge error"))
            report["failures"].append(check["error"])
        report["checks"].append(check)
        return response if ok else None
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


def _validate_ping(report: dict[str, Any], result: Any, require_native: bool) -> None:
    if not isinstance(result, dict):
        report["failures"].append("ping result was not an object")
        return

    report["last_ping"] = result
    if result.get("pong") is not True:
        report["failures"].append("ping did not return pong=true")
    if result.get("cad_api_safe") is not True:
        report["failures"].append("bridge did not report cad_api_safe=true")
    if result.get("transport_only") is True:
        report["failures"].append("bridge reported transport_only=true")
    if require_native and result.get("native_bridge") is not True:
        report["failures"].append("bridge did not report native_bridge=true")


def _validate_read_result(report: dict[str, Any], action: str, result: Any) -> None:
    if action == "get_document_info" and not isinstance(result, dict):
        report["failures"].append("get_document_info result was not an object")
    if action in ("get_layers", "get_objects") and not isinstance(result, list):
        report["failures"].append("{0} result was not a list".format(action))


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
) -> None:
    if not isinstance(result, list):
        report["failures"].append("fixture object check did not return a list")
        return
    if not any(_object_matches_fixture(obj, fixture_name, fixture_handle) for obj in result):
        report["failures"].append("created fixture object was not visible in get_objects")


def _validate_fixture_absent(
    report: dict[str, Any],
    result: Any,
    fixture_name: str,
    fixture_handle: str | None = None,
) -> None:
    if not isinstance(result, list):
        report["failures"].append("fixture cleanup check did not return a list")
        return
    if any(_object_matches_fixture(obj, fixture_name, fixture_handle) for obj in result):
        report["failures"].append("created fixture object remained after cleanup")


def _validate_fixture_selected(
    report: dict[str, Any],
    result: Any,
    fixture_name: str,
    fixture_handle: str | None = None,
) -> None:
    if not isinstance(result, list):
        report["failures"].append("selection get did not return a list")
        return
    if not any(_object_matches_fixture(obj, fixture_name, fixture_handle) for obj in result):
        report["failures"].append("fixture object was not selected")


def _extract_created_handle(result: Any) -> str | None:
    if isinstance(result, dict):
        handle = result.get("handle")
        return str(handle) if handle else None
    if isinstance(result, str):
        match = re.search(r"handle:\s*([^\s,;]+)", result)
        if match:
            return match.group(1)
    return None


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

    objects_response = _record_call(
        sock,
        report,
        "get_objects",
        "fixture-present",
        params={"limit": 200, "object_type": "rect"},
    )
    if objects_response is not None:
        _validate_fixture_present(report, objects_response.get("result"), fixture_name, fixture_handle)

    _record_call(sock, report, "selection", "fixture-clear", params={"action": "clear"})
    _record_call(
        sock,
        report,
        "selection",
        "fixture-select",
        params={"action": "select", "criteria": "((N='{0}'))".format(fixture_name)},
    )
    selection_response = _record_call(sock, report, "selection", "fixture-get", params={"action": "get"})
    if selection_response is not None:
        _validate_fixture_selected(report, selection_response.get("result"), fixture_name, fixture_handle)
    _record_call(sock, report, "selection", "fixture-delete", params={"action": "delete"})

    cleanup_response = _record_call(
        sock,
        report,
        "get_objects",
        "fixture-cleanup",
        params={"limit": 200, "object_type": "rect"},
    )
    if cleanup_response is not None:
        _validate_fixture_absent(report, cleanup_response.get("result"), fixture_name, fixture_handle)


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
) -> dict[str, Any]:
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
        "checks": [],
        "failures": [],
    }

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            for index in range(1, ping_count + 1):
                response = _record_call(sock, report, "ping", index)
                if response is not None:
                    _validate_ping(report, response.get("result"), require_native=require_native)

            read_actions = []
            if phase >= 1:
                read_actions = ["get_document_info", "get_layers", "get_objects"]
            elif include_objects:
                read_actions.append("get_objects")

            for action in read_actions:
                for index in range(1, read_count + 1):
                    params = {"limit": 10} if action == "get_objects" else None
                    response = _record_call(sock, report, action, index, params=params)
                    if response is not None:
                        _validate_read_result(report, action, response.get("result"))

            if phase >= 1 and allow_write_fixture:
                _run_phase_one_write_fixture(sock, report)

            if stop:
                _record_call(sock, report, "stop", 1)
    except Exception as exc:
        report["failures"].append(str(exc))

    report["ok"] = len(report["failures"]) == 0
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
    parser.add_argument("--phase", type=int, choices=(0, 1), default=1)
    parser.add_argument("--allow-write-fixture", action="store_true")
    parser.add_argument("--stop", action="store_true")
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
                "Bridge: {0}; cad_api_safe={1}; native_bridge={2}; transport_only={3}".format(
                    ping.get("bridge_kind", "unknown"),
                    ping.get("cad_api_safe"),
                    ping.get("native_bridge"),
                    ping.get("transport_only"),
                )
            )
        for failure in report["failures"]:
            print("ERROR: {0}".format(failure), file=sys.stderr)

    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
