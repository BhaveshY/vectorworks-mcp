#!/usr/bin/env python3
"""Live acceptance for native wire protocol v2 (capability_revision 6).

Run against an installed VectorworksMCPBridge in a running Vectorworks:

    python native_bridge/live_acceptance_v2.py [--port N] [--out report.json]
                                              [--keep-documents] [--skip-pdf]

Safety rules (enforced by this script):
  * It FIRST creates a new untitled document with `new_document`. If that is
    unsupported, or Vectorworks opened the template file itself instead of an
    untitled copy, it aborts and asks you to open a blank document manually
    (then re-run with --use-active-untitled).
  * Every write carries `expect_document_fingerprint` of a document this script
    created, so a write can never land in one of your own documents.
  * Created documents are closed with discard_changes at the end (unless
    --keep-documents). The originally active document is re-activated. The run
    never leaves Vectorworks with zero documents: before closing the last open
    document it opens a fresh blank one (closing the last document would bring
    up the modal Home screen).
  * If the Vectorworks Home screen (or the "update available" notice) blocks
    the bridge at start, it is closed with the `dismiss_home` action.
  * Python-engine-backed checks that could crash the engine (manage_classes
    set used to) run AFTER the run_script checks so a contained engine fault
    cannot mask other results.

Stdlib only. Prints a JSON report and exits 0 when every required check passed.
"""

from __future__ import annotations

import argparse
import glob
import itertools
import json
import os
import socket
import struct
import sys
import tempfile
import time
import zlib
from typing import Any, Optional

STATE_DIR = os.path.join(os.path.expanduser("~"), ".vectorworks-mcp")


class BridgeError(Exception):
    def __init__(self, response: dict):
        super().__init__(response.get("error"))
        self.response = response
        self.code = response.get("error_code")


class Client:
    def __init__(self, port: int, token: str, timeout: float = 60.0):
        self.port = port
        self.token = token
        self.timeout = timeout
        self.ids = itertools.count(1)
        self.sock: Optional[socket.socket] = None

    def connect(self) -> socket.socket:
        if self.sock is None:
            self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=self.timeout)
        return self.sock

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    @staticmethod
    def send_frame(sock: socket.socket, obj: dict) -> None:
        data = json.dumps(obj).encode("utf-8")
        sock.sendall(struct.pack(">I", len(data)) + data)

    @staticmethod
    def recv_frame(sock: socket.socket) -> Optional[dict]:
        header = b""
        while len(header) < 4:
            chunk = sock.recv(4 - len(header))
            if not chunk:
                return None
            header += chunk
        size = struct.unpack(">I", header)[0]
        payload = b""
        while len(payload) < size:
            chunk = sock.recv(min(1 << 20, size - len(payload)))
            if not chunk:
                return None
            payload += chunk
        return json.loads(payload.decode("utf-8"))

    def raw(self, action: str, params: Optional[dict] = None, timeout_ms: Optional[int] = None) -> dict:
        request = {"id": f"acc-{os.getpid()}-{next(self.ids)}", "action": action,
                   "auth_token": self.token, "params": params or {}}
        if timeout_ms is not None:
            request["timeout_ms"] = timeout_ms
        sock = self.connect()
        try:
            self.send_frame(sock, request)
            response = self.recv_frame(sock)
        except OSError:
            self.close()
            raise
        if response is None:
            self.close()
            raise ConnectionError(f"connection closed during {action}")
        return response

    def call(self, action: str, params: Optional[dict] = None, timeout_ms: Optional[int] = None) -> Any:
        response = self.raw(action, params, timeout_ms)
        if not response.get("success"):
            raise BridgeError(response)
        return response.get("result")


def read_token() -> str:
    token = os.environ.get("VW_MCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    path = os.environ.get("VW_MCP_AUTH_TOKEN_FILE") or os.path.join(STATE_DIR, "auth-token")
    with open(path, encoding="utf-8-sig") as handle:
        return handle.readline().strip()


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(handle)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def read_png(path: str) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Minimal stdlib PNG decoder (8-bit, non-interlaced; gray, RGB, palette,
    gray+alpha, RGBA). Returns (width, height, rgb pixels row-major)."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    pos, width, height, depth, ctype, interlace = 8, 0, 0, 0, 0, 0
    palette: list[tuple[int, int, int]] = []
    idat = b""
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        kind = data[pos + 4:pos + 8]
        chunk = data[pos + 8:pos + 8 + length]
        pos += 12 + length
        if kind == b"IHDR":
            width, height, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunk)
        elif kind == b"PLTE":
            palette = [tuple(chunk[i:i + 3]) for i in range(0, len(chunk), 3)]
        elif kind == b"IDAT":
            idat += chunk
        elif kind == b"IEND":
            break
    if depth != 8 or interlace:
        raise ValueError(f"unsupported PNG (depth {depth}, interlace {interlace})")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(idat)
    stride = width * channels
    rows: list[bytearray] = []
    prev = bytearray(stride)
    offset = 0
    for _ in range(height):
        ftype = raw[offset]
        line = bytearray(raw[offset + 1:offset + 1 + stride])
        offset += 1 + stride
        for i in range(stride):
            left = line[i - channels] if i >= channels else 0
            up = prev[i]
            upleft = prev[i - channels] if i >= channels else 0
            if ftype == 1:
                line[i] = (line[i] + left) & 0xFF
            elif ftype == 2:
                line[i] = (line[i] + up) & 0xFF
            elif ftype == 3:
                line[i] = (line[i] + ((left + up) >> 1)) & 0xFF
            elif ftype == 4:
                pa, pb, pc = abs(up - upleft), abs(left - upleft), abs(left + up - 2 * upleft)
                pred = left if pa <= pb and pa <= pc else (up if pb <= pc else upleft)
                line[i] = (line[i] + pred) & 0xFF
        rows.append(line)
        prev = line
    pixels: list[tuple[int, int, int]] = []
    for line in rows:
        for x in range(width):
            px = line[x * channels:(x + 1) * channels]
            if ctype in (0, 4):
                pixels.append((px[0], px[0], px[0]))
            elif ctype == 3:
                pixels.append(palette[px[0]] if px[0] < len(palette) else (0, 0, 0))
            else:
                pixels.append((px[0], px[1], px[2]))
    return width, height, pixels


def dark_fraction(path: str, threshold: int = 200) -> Optional[float]:
    """Fraction of pixels whose r+g+b < threshold (None if unreadable)."""
    try:
        width, height, pixels = read_png(path)
    except Exception:  # noqa: BLE001
        return None
    if not pixels:
        return None
    step = max(1, len(pixels) // 200000)
    sample = pixels[::step]
    return sum(1 for r, g, b in sample if r + g + b < threshold) / len(sample)


def ops_params(ops: list[dict], key: str, **extra) -> dict:
    params = {"idempotency_key": f"{key}-{time.time_ns()}", "operation_count": len(ops)}
    for i, op in enumerate(ops, 1):
        params[f"operation_{i}_json"] = json.dumps(op)
    params.update(extra)
    return params


def wait_job(client: "Client", job_id: str, seconds: float) -> Optional[dict]:
    status = None
    deadline = time.time() + seconds
    while time.time() < deadline:
        status = client.call("job_status", {"job_id": job_id})
        if status["state"] in ("done", "failed", "cancelled"):
            return status
        time.sleep(0.5)
    return status


def discovery_records() -> list[dict]:
    records = []
    for path in glob.glob(os.path.join(STATE_DIR, "instances", "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                record = json.load(handle)
            record["_file"] = path
            record["_alive"] = pid_alive(int(record.get("pid", 0)))
            records.append(record)
        except (OSError, ValueError):
            continue
    return records


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []
        self.notes: list[str] = []
        self.started = time.time()

    def check(self, name: str, ok: bool, detail: Any = None, required: bool = True) -> bool:
        self.checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})
        mark = "PASS" if ok else ("FAIL" if required else "WARN")
        print(f"[{mark}] {name}" + (f" - {detail}" if detail not in (None, "") and not ok else ""), file=sys.stderr)
        return bool(ok)

    def run(self, name: str, func, required: bool = True) -> Any:
        try:
            return func()
        except BridgeError as error:
            self.check(name, False, {"error_code": error.code, "error": str(error)[:400],
                                     "details": error.response.get("details")}, required)
        except Exception as error:  # noqa: BLE001
            self.check(name, False, f"{type(error).__name__}: {error}"[:400], required)
        return None

    def summary(self) -> dict:
        failed = [c["name"] for c in self.checks if c["required"] and not c["ok"]]
        warned = [c["name"] for c in self.checks if not c["required"] and not c["ok"]]
        return {"ok": not failed, "failed": failed, "warnings": warned,
                "passed": sum(1 for c in self.checks if c["ok"]), "total": len(self.checks),
                "duration_s": round(time.time() - self.started, 2), "checks": self.checks, "notes": self.notes}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--out", default="")
    parser.add_argument("--keep-documents", action="store_true")
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-ifc", action="store_true", help="skip export_ifc / import_ifc (minutes on some documents)")
    parser.add_argument("--use-active-untitled", action="store_true",
                        help="use the active document instead of new_document, but only if it is untitled and empty")
    parser.add_argument("--template", default="", help="template_path for new_document")
    args = parser.parse_args()

    report = Report()
    work_dir = tempfile.mkdtemp(prefix="vw-mcp-acceptance-")
    report.notes.append(f"artifacts in {work_dir}")

    # ---------------------------------------------------------- discovery
    records = discovery_records()
    alive = [r for r in records if r.get("_alive")]
    port = args.port
    if not port:
        port = alive[0]["port"] if len(alive) == 1 else int(os.environ.get("VW_MCP_PORT", "9877"))
    disc = next((r for r in alive if r.get("port") == port), None)
    report.check("discovery_file_present", disc is not None, {"files": [r["_file"] for r in records]})
    if disc:
        report.check("discovery_file_fields",
                     all(k in disc for k in ("pid", "port", "protocol", "capability_revision", "bridge_version",
                                             "build_id", "vectorworks_version", "session_id", "started_at"))
                     and disc.get("protocol") == 2 and disc.get("capability_revision") == 6, disc)

    client = Client(port, read_token())
    created: list[str] = []
    original_active: Optional[str] = None

    try:
        # ------------------------------------------------- Home screen gate
        ping = client.call("ping")
        report.check("ping_modal_dialog_field", "modal_dialog" in ping and "auto_dismiss_dialogs" in ping,
                     {k: ping.get(k) for k in ("modal_dialog", "auto_dismiss_dialogs", "ui_state")})
        dialog = ping.get("modal_dialog") or {}
        if dialog.get("kind") in ("home", "update_available"):
            dis = report.run("dismiss_home_at_start", lambda: client.call("dismiss_home", {"wait_ms": 5000}))
            report.check("dismiss_home_at_start", bool(dis and not dis.get("still_shown")), dis)
            time.sleep(0.5)
            ping = client.call("ping")
        elif dialog:
            report.notes.append(f"a non-Home dialog is open in Vectorworks: {dialog}; close it and re-run")
        else:
            nop = report.run("dismiss_home_noop", lambda: client.call("dismiss_home", {"wait_ms": 0}))
            report.check("dismiss_home_noop", bool(nop and nop.get("dismissed") is False), nop)

        # ------------------------------------------------------------ ping
        needed = ["protocol", "capability_revision", "bridge_version", "build_id", "pid", "port", "session_id",
                  "vectorworks_version", "ui_state", "handler_running_ms", "queue_depth", "main_context_idle_ms",
                  "active_document", "open_document_count", "pong", "bridge_kind", "implemented_actions"]
        missing = [k for k in needed if k not in ping]
        report.check("ping_v2_fields", not missing and ping.get("protocol") == 2 and ping.get("capability_revision") == 6,
                     {"missing": missing, "protocol": ping.get("protocol"), "rev": ping.get("capability_revision")})
        report.check("ping_ui_state_valid", ping.get("ui_state") in ("idle", "modal", "tracking", "loading", "no_document"),
                     ping.get("ui_state"))
        report.check("ping_legacy_pump_value", ping.get("main_context_pump") == "win32_ui_timer", ping.get("main_context_pump"))
        if disc:
            report.check("discovery_matches_ping", disc.get("pid") == ping.get("pid") and disc.get("build_id") == ping.get("build_id"),
                         {"disc": disc.get("build_id"), "ping": ping.get("build_id")})
        caps = client.call("capabilities")
        names = {a.get("name") for a in caps.get("actions", [])}
        report.check("capabilities_actions",
                     {"list_documents", "new_document", "close_document", "run_script", "layers", "capture_view",
                      "job_status", "cancel_job", "dismiss_home"} <= names, sorted(names))
        report.check("ping_max_operations",
                     ping.get("max_operations") == 2000 and (caps.get("limits") or {}).get("max_operations") == 2000,
                     {"ping": ping.get("max_operations"), "limits": caps.get("limits")})
        if ping.get("active_document"):
            original_active = ping["active_document"].get("fingerprint")

        # -------------------------------------------- create test document
        doc_a = None
        if args.use_active_untitled:
            docs = client.call("list_documents")
            active = next((d for d in docs["documents"] if d.get("active")), None)
            info = client.call("get_document_info") if active else None
            if active and active.get("untitled") and info and info.get("total_objects") == 0:
                doc_a = active
                report.notes.append("using the active untitled empty document (--use-active-untitled)")
            else:
                print("ABORT: --use-active-untitled needs an active, untitled, empty document.", file=sys.stderr)
                return 3
        else:
            params = {"template_path": args.template} if args.template else {}
            try:
                res = client.call("new_document", params, timeout_ms=120000)
            except BridgeError as error:
                print(json.dumps({"ok": False, "abort": "new_document failed", "error_code": error.code,
                                  "error": str(error), "details": error.response.get("details"),
                                  "next_step": "Open a blank untitled document in Vectorworks manually and re-run with "
                                               "--use-active-untitled (or pass --template <path to a .sta>)."}, indent=2))
                return 3
            doc_a = res["document"]
            if not res.get("untitled"):
                print(json.dumps({"ok": False, "abort": "new_document opened the template file itself, not an untitled copy",
                                  "document": doc_a,
                                  "next_step": "Close that document without saving, open a blank untitled document manually "
                                               "and re-run with --use-active-untitled."}, indent=2))
                return 3
            created.append(doc_a["fingerprint"])
        fp_a = doc_a["fingerprint"]
        report.check("new_document_untitled", doc_a.get("untitled") is True, doc_a)

        def w(params: dict, fp: str = None) -> dict:
            out = dict(params)
            out["expect_document_fingerprint"] = fp or fp_a
            out["activate_document"] = True
            return out

        ping2 = client.call("ping")
        report.check("ping_active_document_is_new", (ping2.get("active_document") or {}).get("fingerprint") == fp_a,
                     ping2.get("active_document"))

        # ---------------------------------------- two documents + activate
        if not args.use_active_untitled:
            res_b = report.run("new_document_second", lambda: client.call("new_document", {}, timeout_ms=120000))
            if res_b:
                fp_b = res_b["document"]["fingerprint"]
                created.append(fp_b)
                docs = client.call("list_documents")
                fps = [d["fingerprint"] for d in docs["documents"]]
                report.check("list_documents_two_distinct", fp_a in fps and fp_b in fps and fp_a != fp_b, fps)
                act = client.call("activate_document", {"fingerprint": fp_a})
                report.check("activate_document", act["active_document"]["fingerprint"] == fp_a, act)
                # document_changed guard
                client.call("activate_document", {"fingerprint": fp_b})
                try:
                    client.call("get_view", {"expect_document_fingerprint": fp_a})
                    report.check("document_changed_guard", False, "no error")
                except BridgeError as error:
                    report.check("document_changed_guard", error.code == "document_changed", error.code)
                view = report.run("activate_via_expect", lambda: client.call("get_view", w({})))
                report.check("activate_via_expect", view is not None and
                             client.call("ping")["active_document"]["fingerprint"] == fp_a)

        # --------------------------------------------- create/edit/delete
        ops = [
            {"op": "create", "local_ref": "r1", "object_type": "rect", "x1": 0, "y1": 0, "x2": 1000, "y2": 500,
             "name": "ACC_RECT_1"},
            {"op": "create", "local_ref": "w1", "object_type": "wall", "x1": 0, "y1": 2000, "x2": 4000, "y2": 2000,
             "thickness": 200, "height": 2800},
        ]
        params = {"idempotency_key": f"acc-{time.time_ns()}", "operation_count": len(ops)}
        for i, op in enumerate(ops, 1):
            params[f"operation_{i}_json"] = json.dumps(op)
        tx = report.run("apply_operations_create", lambda: client.call("apply_operations", w(params)))
        rect_uuid = rect_handle = None
        if tx:
            receipts = tx["transaction"]["operations"]
            rect_uuid = receipts[0].get("uuid")
            rect_handle = receipts[0].get("handle")
            report.check("create_receipts_have_uuid", all(r.get("uuid") for r in receipts), receipts)
            report.check("handle_is_opaque_id", isinstance(rect_handle, str) and rect_handle.startswith("h"), rect_handle)
            prop = report.run("set_property_by_handle", lambda: client.call(
                "set_property", w({"handle": rect_handle, "property_name": "name", "value": "ACC_RECT_RENAMED"})))
            report.check("set_property_by_handle", bool(prop and prop.get("changed")))
            info_before = client.call("get_document_info", w({}))
            dparams = {"idempotency_key": f"acc-del-{time.time_ns()}", "operation_count": 1,
                       "operation_1_json": json.dumps({"op": "object.delete", "target": f"uuid:{rect_uuid}",
                                                       "confirm": "DELETE_OBJECT"})}
            dres = report.run("delete_object", lambda: client.call("apply_operations", w(dparams)))
            report.check("delete_object", bool(dres and dres.get("ok")))
            try:
                client.call("set_property", w({"handle": rect_handle, "property_name": "name", "value": "x"}))
                report.check("stale_reference_after_delete", False, "no error")
            except BridgeError as error:
                report.check("stale_reference_after_delete", error.code == "stale_reference", error.code)
            info_after = client.call("get_document_info", w({}))
            report.check("delete_reduced_object_count",
                         info_after["total_objects"] == info_before["total_objects"] - 1,
                         {"before": info_before["total_objects"], "after": info_after["total_objects"]})
            # Undo check (best effort: Undo through the universal menu command,
            # outside any bridge undo event: transaction False). The delete
            # registers exactly one undo record (DeleteObject(h, true), no
            # AddBeforeSwapObject), so ONE Undo must bring back exactly ONE
            # object with the same uuid.
            undo = report.run("undo_delete_via_menu", lambda: client.call("run_script", w({
                "language": "python", "transaction": False, "undo_label": "acceptance undo",
                "code": "vs.DoMenuTextByName('Undo', 0)\nresult = True\n"})), required=False)
            if undo and undo.get("ok"):
                info_undo = client.call("get_document_info", w({}))
                restored = client.call("query_objects", w({"uuids": [rect_uuid]}))
                detail = {"before": info_before["total_objects"], "after_delete": info_after["total_objects"],
                          "after_undo": info_undo["total_objects"], "uuid_found": len(restored.get("items", [])),
                          "missing": restored.get("missing_uuids")}
                if info_undo["total_objects"] == info_after["total_objects"]:
                    detail["hint"] = ("nothing changed: Undo invoked from inside a script may be ignored by "
                                      "Vectorworks; verify manually with Edit > Undo")
                report.check("undo_restores_deleted_object_once",
                             info_undo["total_objects"] == info_before["total_objects"]
                             and len(restored.get("items", [])) == 1,
                             detail, required=False)

        # ------------------------------------------- doors in a local wall
        schema = report.run("describe_door_schema",
                            lambda: client.call("describe_parametric_schema", w({"plugin_name": "Door"})), required=False)
        if schema and schema.get("descriptor_fingerprint"):
            ops2 = [
                {"op": "create", "local_ref": "w2", "object_type": "wall", "x1": 0, "y1": 6000, "x2": 5000, "y2": 6000,
                 "thickness": 200, "height": 2800},
                {"op": "create", "object_type": "door", "x1": 2500, "y1": 6000, "width": 900, "height": 2100,
                 "wall_uuid": "$w2", "require_wall_host": True,
                 "descriptor_fingerprint": schema["descriptor_fingerprint"]},
            ]
            p2 = {"idempotency_key": f"acc-door-{time.time_ns()}", "operation_count": 2}
            for i, op in enumerate(ops2, 1):
                p2[f"operation_{i}_json"] = json.dumps(op)
            dres = report.run("door_in_local_wall_one_transaction", lambda: client.call("apply_operations", w(p2)),
                              required=False)
            report.check("door_in_local_wall_one_transaction", bool(dres and dres.get("ok")), required=False)
            if dres and dres.get("ok"):
                door_uuid = dres["transaction"]["operations"][1].get("uuid")
                wall_uuid = dres["transaction"]["operations"][0].get("uuid")
                doors = client.call("query_objects", w({"object_type": "door", "limit": 100}))["items"]
                hit = next((d for d in doors if d.get("uuid") == door_uuid), None)
                report.check("door_in_wall_listed_by_query", bool(hit and hit.get("parent_uuid") == wall_uuid
                                                                  and hit.get("host") == "wall"), hit)
                by_uuid = client.call("query_objects", w({"uuids": [door_uuid]}))
                report.check("door_in_wall_resolvable_by_uuid", len(by_uuid.get("items", [])) == 1, by_uuid)

        # ------------------- wall inserts survive wall edits (2026-10-14 data loss)
        # Native doors/windows used to be only parented into the wall (no
        # kSymbolBreak); Vectorworks drops such orphans when it rebuilds the
        # wall. They must survive class change, move, reset and Undo.
        wschema = report.run("describe_window_schema",
                             lambda: client.call("describe_parametric_schema", w({"plugin_name": "Window"})),
                             required=False)
        if schema and schema.get("descriptor_fingerprint") and wschema and wschema.get("descriptor_fingerprint"):
            ins_ops = [
                {"op": "create", "local_ref": "wi", "object_type": "wall", "x1": 0, "y1": 80000, "x2": 6000,
                 "y2": 80000, "thickness": 200, "height": 2800},
                {"op": "create", "object_type": "door", "x1": 1500, "y1": 80000, "width": 900, "height": 2100,
                 "wall_uuid": "$wi", "require_wall_host": True, "descriptor_fingerprint": schema["descriptor_fingerprint"]},
                {"op": "create", "object_type": "window", "x1": 4200, "y1": 80000, "width": 1200, "height": 1300,
                 "sill_height": 900, "wall_uuid": "$wi", "require_wall_host": True,
                 "descriptor_fingerprint": wschema["descriptor_fingerprint"]},
            ]
            ins_res = report.run("wall_inserts_create", lambda: client.call("apply_operations", w(ops_params(ins_ops, "acc-ins"))))
            if ins_res and ins_res.get("ok"):
                wall_u, door_u, win_u = [o.get("uuid") for o in ins_res["transaction"]["operations"]]
                probe = (
                    "w = vs.GetObjectByUuid(%r)\n"
                    "out = {'wall': w is not None, 'items': {}}\n"
                    "for key, u in (('door', %r), ('window', %r)):\n"
                    "    h = vs.GetObjectByUuid(u)\n"
                    "    rec = 'Door' if key == 'door' else 'Window'\n"
                    "    out['items'][key] = None if h is None else {\n"
                    "        'parent_is_wall': w is not None and vs.GetParent(h) == w,\n"
                    "        'break': bool(vs.GetObjectWallOffset(h, w)[0]) if w is not None else False,\n"
                    "        'width': vs.GetRField(h, rec, 'Width'),\n"
                    "        'set_at': vs.GetRField(h, rec, 'ElevationSetAt') if key == 'window' else None}\n"
                    "result = out\n") % (wall_u, door_u, win_u)

                def inserts_state() -> dict:
                    res = client.call("run_script", w({"language": "python", "transaction": False, "code": probe}))
                    return (res or {}).get("result") or {}

                def inserts_ok(state: dict) -> bool:
                    items = state.get("items") or {}
                    return bool(state.get("wall")) and all(
                        items.get(k) and items[k]["parent_is_wall"] and items[k]["break"] for k in ("door", "window"))

                st0 = inserts_state()
                report.check("wall_inserts_have_break_records", inserts_ok(st0), st0)
                report.check("window_elevation_set_at_sill",
                             ((st0.get("items") or {}).get("window") or {}).get("set_at") == "Sill of Window",
                             st0, required=False)
                widths0 = {k: (v or {}).get("width") for k, v in (st0.get("items") or {}).items()}
                cls = f"ACC-Walls-{int(time.time())}"
                steps = [
                    ("class", lambda: client.call("apply_operations", w(ops_params([
                        {"op": "set_property", "target": f"uuid:{wall_u}", "property_name": "class", "value": cls}],
                        "acc-ins-class")))),
                    ("move", lambda: client.call("apply_operations", w(ops_params([
                        {"op": "object.transform", "target": f"uuid:{wall_u}", "delta_x": 0, "delta_y": 1000}],
                        "acc-ins-move")))),
                    ("reset", lambda: client.call("run_script", w({
                        "language": "python", "transaction": False,
                        "code": "vs.ResetObject(vs.GetObjectByUuid(%r))\nresult = True\n" % wall_u}))),
                    ("undo", lambda: client.call("run_script", w({
                        "language": "python", "transaction": False,
                        "code": "vs.DoMenuTextByName('Undo', 0)\nresult = True\n"}))),
                ]
                for step, func in steps:
                    report.run(f"wall_inserts_{step}", func, required=(step != "undo"))
                    st = inserts_state()
                    widths = {k: (v or {}).get("width") for k, v in (st.get("items") or {}).items()}
                    report.check(f"wall_inserts_survive_{step}", inserts_ok(st) and widths == widths0,
                                 {"state": st, "widths_before": widths0}, required=(step != "undo"))

        # ------------------------------------------------ automatic wall joins
        jops = [
            {"op": "create", "local_ref": "j1", "object_type": "wall", "x1": 10000, "y1": 0, "x2": 14000, "y2": 0,
             "thickness": 200, "height": 2800},
            {"op": "create", "local_ref": "j2", "object_type": "wall", "x1": 14000, "y1": 0, "x2": 14000, "y2": 3000,
             "thickness": 200, "height": 2800},
            {"op": "create", "local_ref": "j3", "object_type": "wall", "x1": 12000, "y1": 0, "x2": 12000, "y2": 2000,
             "thickness": 200, "height": 2800},
        ]
        jp = {"idempotency_key": f"acc-join-{time.time_ns()}", "operation_count": len(jops)}
        for i, op in enumerate(jops, 1):
            jp[f"operation_{i}_json"] = json.dumps(op)
        jres = report.run("wall_joins_apply", lambda: client.call("apply_operations", w(jp)), required=False)
        if jres:
            kinds = sorted(j.get("kind") for j in jres.get("wall_joins", []) if j.get("joined"))
            report.check("wall_joins_L_and_T", kinds == ["L", "T"], jres.get("wall_joins"), required=False)

        # ------------------------------------------------------------- view
        v0 = report.run("get_view", lambda: client.call("get_view", w({})))
        if v0:
            report.check("get_view_fields", all(k in v0 for k in ("standard_view", "projection", "render_mode", "zoom",
                                                                    "center", "active_layer", "active_layer_kind",
                                                                    "screen_px", "dpi")), sorted(v0))
            sv = report.run("set_view_fit_drawing", lambda: client.call("set_view", w({"fit": "drawing", "margin_pct": 10})))
            report.check("set_view_fit_applied", bool(sv and sv.get("fit", {}).get("applied")), sv and sv.get("fit"))
            report.check("set_view_previous_current", bool(sv and "previous" in sv and "current" in sv))

        # ---------------------------------------------- capture + restore
        # Select one object so the selection restore is meaningful.
        client.call("selection", w({"action": "select", "criteria": "((N='ACC_RECT_RENAMED'))"}))
        if rect_uuid is None:
            pass
        sel_before = client.call("selection", w({"action": "get"}))
        view_before = client.call("get_view", w({}))
        png = os.path.join(work_dir, "capture_fit.png")
        cap = report.run("capture_view_png", lambda: client.call("capture_view", w({
            "path": png, "format": "png", "fit": "drawing", "standard_view": "right_iso", "render_mode": "opengl",
            "restore": True})))
        if cap:
            report.check("capture_file_written", os.path.isfile(png) and os.path.getsize(png) == cap.get("bytes"),
                         {"bytes": cap.get("bytes"), "width": cap.get("width"), "height": cap.get("height")})
            report.check("capture_dimensions", (cap.get("width") or 0) > 0 and (cap.get("height") or 0) > 0)
            view_after = client.call("get_view", w({}))
            sel_after = client.call("selection", w({"action": "get"}))
            same_view = all(view_after.get(k) == view_before.get(k)
                            for k in ("standard_view", "projection", "render_mode", "active_layer"))
            close_center = all(abs(a - b) < 1e-3 for a, b in zip(view_after["center"], view_before["center"]))
            report.check("capture_restored_view", cap.get("restored") and same_view and close_center,
                         {"before": view_before, "after": view_after, "restore_verified": cap.get("restore_verified")})
            report.check("capture_iso_restored_verified",
                         cap.get("restored") is True and not cap.get("restore_mismatch")
                         and view_after.get("view_matrix") == view_before.get("view_matrix")
                         and view_after.get("zoom") == view_before.get("zoom"),
                         {"restored": cap.get("restored"), "mismatch": cap.get("restore_mismatch"),
                          "before": {k: view_before.get(k) for k in ("standard_view_name", "projection_name",
                                                                      "render_mode_name", "zoom")},
                          "after": {k: view_after.get(k) for k in ("standard_view_name", "projection_name",
                                                                    "render_mode_name", "zoom")}})
            report.check("capture_restored_selection",
                         sorted(o.get("uuid") for o in sel_after) == sorted(o.get("uuid") for o in sel_before),
                         {"before": len(sel_before), "after": len(sel_after)})
        plan_png = os.path.join(work_dir, "capture_plan.png")
        cap2 = report.run("capture_view_plan_objects", lambda: client.call("capture_view", w({
            "path": plan_png, "fit": "drawing", "standard_view": "top", "projection": "plan"})))
        report.check("capture_plan_cropped", bool(cap2 and cap2.get("export_rect")), cap2 and cap2.get("fit"), required=False)

        # ------------------------- 1.0.x findings: layers, isolation, order
        ts = int(time.time())
        layer_a, layer_b = f"ACC Iso A {ts}", f"ACC Iso B {ts}"
        for lname_iso in (layer_a, layer_b):
            report.run(f"layers_create_{lname_iso}", lambda n=lname_iso: client.call(
                "layers", w({"action": "create", "name": n, "kind": "design"})))
        active_before_ops = client.call("get_view", w({}))["active_layer"]
        iso_ops = [
            {"op": "create", "local_ref": "ia", "object_type": "rect", "x1": 50000, "y1": 0, "x2": 52000, "y2": 1000,
             "layer": layer_a, "name": f"ACC_ISO_A_{ts}"},
            {"op": "create", "local_ref": "ib", "object_type": "rect", "x1": 49000, "y1": -1000, "x2": 53000,
             "y2": 2000, "layer": layer_b, "name": f"ACC_ISO_B_{ts}"},
            {"op": "set_property", "target": "$ib", "property_name": "fillColor", "value": "0,0,0"},
            {"op": "set_property", "target": "$ib", "property_name": "fillPattern", "value": "1"},
        ]
        iso_res = report.run("per_op_layer_create", lambda: client.call("apply_operations", w(ops_params(iso_ops, "acc-iso"))))
        if iso_res:
            receipts = iso_res["transaction"]["operations"]
            report.check("per_op_layer_receipts", receipts[0].get("layer") == layer_a and receipts[1].get("layer") == layer_b,
                         [r.get("layer") for r in receipts[:2]])
            on_b = client.call("query_objects", w({"layer": layer_b, "limit": 100}))["items"]
            report.check("per_op_layer_object_on_layer", any(o.get("uuid") == receipts[1].get("uuid") for o in on_b),
                         [o.get("uuid") for o in on_b])
            report.check("per_op_layer_active_layer_restored",
                         client.call("get_view", w({}))["active_layer"] == active_before_ops, active_before_ops)
        try:
            client.call("apply_operations", w(ops_params(
                [{"op": "create", "object_type": "rect", "x1": 0, "y1": 0, "x2": 10, "y2": 10,
                  "layer": f"ACC no such layer {ts}"}], "acc-nolayer")))
            report.check("per_op_layer_not_found", False, "no error")
        except BridgeError as error:
            report.check("per_op_layer_not_found", error.code == "layer_not_found"
                         and error.response.get("commit_state") == "not_started", error.response)

        # capture isolation (#11/#12/#22) + plan default (#10)
        if iso_res:
            iso_png = os.path.join(work_dir, "capture_layer_isolated.png")
            ctl_png = os.path.join(work_dir, "capture_layer_not_isolated.png")
            iso_cap = report.run("capture_layer_isolated", lambda: client.call("capture_view", w({
                "path": iso_png, "layer": layer_a, "fit": "layer", "restore": True})))
            # The control must SHOW the other layers: with Layer Options "Gray
            # Others" (6 = Gray/Snap Others, the 2024 Blank template default)
            # ExportImage draws non-active layers as light outlines without
            # fills, so isolated and control captures were byte-identical
            # (2026-10-14). Not an isolation bug; force "Show/Snap/Modify Others".
            opts = client.call("run_script", w({"language": "python", "transaction": False,
                                                 "code": "result = vs.GetLayerOptions()\nvs.SetLayerOptions(5)\n"}))
            ctl_cap = report.run("capture_layer_not_isolated", lambda: client.call("capture_view", w({
                "path": ctl_png, "layer": layer_a, "fit": "layer", "isolate": False, "restore": True})),
                required=False)
            if (opts or {}).get("ok") and isinstance(opts.get("result"), int):
                client.call("run_script", w({"language": "python", "transaction": False,
                                             "code": "vs.SetLayerOptions(%d)\nresult = True\n" % opts["result"]}))
            if iso_cap:
                used = iso_cap.get("view_used") or {}
                report.check("capture_layer_plan_default",
                             iso_cap.get("plan_default_applied") is True and used.get("projection_name") == "plan"
                             and used.get("standard_view") == 7,
                             {k: used.get(k) for k in ("standard_view_name", "projection_name")})
                report.check("capture_layer_fit_applied",
                             iso_cap.get("fit_to_objects_applied") is True and (iso_cap.get("fit") or {}).get("applied"),
                             iso_cap.get("fit"))
                report.check("capture_layer_isolation_applied",
                             (iso_cap.get("isolation") or {}).get("applied") is True
                             and (iso_cap.get("isolation") or {}).get("layers") == [layer_a],
                             iso_cap.get("isolation"))
                report.check("capture_layer_restored", iso_cap.get("restored") is True, iso_cap.get("restore_mismatch"))
                report.check("capture_png_really_png", iso_cap.get("encoded_format") == "png",
                             {k: iso_cap.get(k) for k in ("encoded_format", "transcoded_from", "transcode_error")})
                area = iso_cap.get("drawing_area_px") or [0, 0]
                long_edge = max(iso_cap.get("width") or 0, iso_cap.get("height") or 0)
                report.check("capture_layer_crisp",
                             long_edge >= min(1200, int(0.8 * max(area))),
                             {"image": [iso_cap.get("width"), iso_cap.get("height")], "drawing_area_px": area,
                              "fill": (iso_cap.get("fit") or {}).get("fill")}, required=False)
                iso_dark = dark_fraction(iso_png)
                ctl_dark = dark_fraction(ctl_png) if ctl_cap else None
                report.check("capture_layer_contains_no_other_layer",
                             iso_dark is not None and iso_dark < 0.25,
                             {"isolated_dark_fraction": iso_dark, "not_isolated_dark_fraction": ctl_dark})
                # Layer B's black rect covers A's crop; even if B is stacked
                # below A's (white-filled) rect, the 5 % margin band is black.
                report.check("capture_isolation_differs_from_control",
                             iso_dark is not None and ctl_dark is not None and ctl_dark > iso_dark + 0.1,
                             {"isolated": iso_dark, "control": ctl_dark}, required=False)
                # Capture resolution (2026-10-14): ExportImage renders at 72 dpi of the
                # layer's PAGE size, whatever the zoom; the bridge boosts the layer scale.
                boost = iso_cap.get("scale_boost") or {}
                report.check("capture_resolution_boost_plan",
                             long_edge >= 1200 and boost.get("applied") is True and (boost.get("factor") or 0) > 1,
                             {"image": [iso_cap.get("width"), iso_cap.get("height")], "scale_boost": boost})
                report.check("capture_scale_boost_restored",
                             boost.get("scales_restored") is True and iso_cap.get("restored") is True,
                             {"scale_boost": boost, "restore_mismatch": iso_cap.get("restore_mismatch")})
                report.check("capture_world_bounds_plan",
                             isinstance(iso_cap.get("world_bounds"), dict) and (iso_cap.get("px_per_mm") or 0) > 0,
                             {k: iso_cap.get(k) for k in ("world_bounds", "px_per_mm", "px_per_mm_xy")})

            # (b) the layer scale reads back unchanged and the capture leaves no
            # undo entry: a sentinel created right before the capture must be the
            # object removed by ONE Undo (best effort: Undo from a script).
            scale_code = "result = vs.GetLScale(vs.GetLayerByName(%r))\n" % layer_a
            scale_before = (client.call("run_script", w({"language": "python", "transaction": False,
                                                         "code": scale_code})) or {}).get("result")
            sentinel = report.run("capture_undo_sentinel", lambda: client.call("apply_operations", w(ops_params([
                {"op": "create", "object_type": "rect", "x1": 50500, "y1": 200, "x2": 50600, "y2": 300,
                 "layer": layer_a, "name": f"ACC_SENTINEL_{ts}"}], "acc-sentinel"))), required=False)
            ping_dirty_before = (client.call("ping").get("active_document") or {}).get("dirty")
            boost_png = os.path.join(work_dir, "capture_boost_undo.png")
            bcap = report.run("capture_boost_for_undo", lambda: client.call("capture_view", w({
                "path": boost_png, "layer": layer_a, "fit": "layer"})))
            scale_after = (client.call("run_script", w({"language": "python", "transaction": False,
                                                        "code": scale_code})) or {}).get("result")
            ping_dirty_after = (client.call("ping").get("active_document") or {}).get("dirty")
            report.check("capture_layer_scale_unchanged",
                         scale_before is not None and scale_before == scale_after,
                         {"before": scale_before, "after": scale_after, "scale_boost": (bcap or {}).get("scale_boost")})
            report.check("capture_dirty_state_unchanged",
                         ping_dirty_before is True or ping_dirty_before == ping_dirty_after,
                         {"before": ping_dirty_before, "after": ping_dirty_after,
                          "note": "untitled documents are always dirty; SetLayerScaleN marks a clean document "
                                  "dirty (scale_boost.marked_dirty)"}, required=False)
            if sentinel and sentinel.get("ok"):
                sentinel_uuid = sentinel["transaction"]["operations"][0].get("uuid")
                client.call("run_script", w({"language": "python", "transaction": False,
                                             "code": "vs.DoMenuTextByName('Undo', 0)\nresult = True\n"}))
                gone = client.call("query_objects", w({"uuids": [sentinel_uuid]}))
                scale_undo = (client.call("run_script", w({"language": "python", "transaction": False,
                                                           "code": scale_code})) or {}).get("result")
                report.check("capture_leaves_no_undo_entry",
                             not gone.get("items") and scale_undo == scale_before,
                             {"sentinel_still_there": bool(gone.get("items")), "scale_after_undo": scale_undo,
                              "scale_before": scale_before}, required=False)

            # (c) 3D capture: right_iso from Top/Plan must really be 3D and large.
            iso3d_png = os.path.join(work_dir, "capture_layer_3d.png")
            cap3d = report.run("capture_3d_boost", lambda: client.call("capture_view", w({
                "path": iso3d_png, "layer": layer_a, "fit": "layer", "standard_view": "right_iso",
                "render_mode": "opengl"})))
            if cap3d:
                used3d = cap3d.get("view_used") or {}
                report.check("capture_3d_is_3d", used3d.get("projection_name") != "plan"
                             and used3d.get("standard_view_name") == "right_iso",
                             {k: used3d.get(k) for k in ("standard_view_name", "projection_name")})
                report.check("capture_resolution_boost_3d",
                             max(cap3d.get("width") or 0, cap3d.get("height") or 0) >= 1200,
                             {"image": [cap3d.get("width"), cap3d.get("height")],
                              "scale_boost": cap3d.get("scale_boost")})
                report.check("capture_3d_no_world_bounds", cap3d.get("world_bounds") is None,
                             cap3d.get("world_bounds"), required=False)

        # (d) image pixel -> world mapping: two black squares far apart on their own
        # layer; their pixel centroids mapped through world_bounds/px_per_mm must
        # land on their world centres within 1 % of the extent.
        layer_m = f"ACC Map {ts}"
        report.run("layers_create_map", lambda: client.call("layers", w({"action": "create", "name": layer_m,
                                                                           "kind": "design"})))
        centres = {"P": (200400.0, 100400.0), "Q": (212400.0, 106400.0)}
        map_ops = []
        for key, (cx, cy) in centres.items():
            map_ops.append({"op": "create", "local_ref": key.lower(), "object_type": "rect", "x1": cx - 400,
                            "y1": cy - 400, "x2": cx + 400, "y2": cy + 400, "layer": layer_m,
                            "name": f"ACC_MAP_{key}_{ts}"})
            map_ops.append({"op": "set_property", "target": "$" + key.lower(), "property_name": "fillColor",
                            "value": "0,0,0"})
            map_ops.append({"op": "set_property", "target": "$" + key.lower(), "property_name": "fillPattern",
                            "value": "1"})
        map_res = report.run("map_rects_create", lambda: client.call("apply_operations", w(ops_params(map_ops, "acc-map"))))
        if map_res and map_res.get("ok"):
            map_uuids = [o.get("uuid") for o in map_res["transaction"]["operations"] if o.get("op") == "create"]
            map_png = os.path.join(work_dir, "capture_map.png")
            mcap = report.run("capture_map", lambda: client.call("capture_view", w({
                "path": map_png, "fit": "objects", "object_uuids": map_uuids})))
            wb = (mcap or {}).get("world_bounds") or {}
            ppm = (mcap or {}).get("px_per_mm") or 0
            detail: dict = {"world_bounds": wb, "px_per_mm": ppm,
                            "image": [(mcap or {}).get("width"), (mcap or {}).get("height")]}
            ok = False
            try:
                width, height, pixels = read_png(map_png)
                dark = [(i % width, i // width) for i, (r, g, b) in enumerate(pixels) if r + g + b < 150]
                halves = {"P": [q for q in dark if q[0] < width / 2], "Q": [q for q in dark if q[0] >= width / 2]}
                extent = max(wb["right"] - wb["left"], wb["top"] - wb["bottom"])
                errors = {}
                for key, pts in halves.items():
                    u = sum(q[0] for q in pts) / len(pts) + 0.5
                    v = sum(q[1] for q in pts) / len(pts) + 0.5
                    x = wb["left"] + u / ppm
                    y = wb["top"] - v / ppm
                    errors[key] = {"pixel": [round(u, 1), round(v, 1)], "world": [round(x, 1), round(y, 1)],
                                   "expected": centres[key],
                                   "error_pct": round(100 * max(abs(x - centres[key][0]),
                                                                abs(y - centres[key][1])) / extent, 3)}
                detail["mapping"] = errors
                ok = all(e["error_pct"] < 1.0 for e in errors.values())
            except Exception as error:  # noqa: BLE001
                detail["error"] = f"{type(error).__name__}: {error}"
            report.check("capture_world_mapping_accurate", ok, detail)

        # spaces behind walls (#1)
        sp_ops = [
            {"op": "create", "local_ref": "sw", "object_type": "wall", "x1": 60000, "y1": 0, "x2": 64000, "y2": 0,
             "thickness": 200, "height": 2800},
            {"op": "create", "local_ref": "ss", "object_type": "space", "height": 2800, "name": f"ACC Room {ts}",
             "points": [[60000, -100], [64000, -100], [64000, 3000], [60000, 3000]]},
        ]
        sp_res = report.run("space_with_wall_create", lambda: client.call("apply_operations", w(ops_params(sp_ops, "acc-space"))))
        if sp_res:
            wall_uuid = sp_res["transaction"]["operations"][0].get("uuid")
            space_uuid = sp_res["transaction"]["operations"][1].get("uuid")
            items = client.call("query_objects", w({"layer": active_before_ops, "limit": 1000}))["items"]
            order = [o.get("uuid") for o in items]
            report.check("space_sent_to_back",
                         (sp_res.get("sent_to_back") or 0) >= 1 and space_uuid in order and wall_uuid in order
                         and order.index(space_uuid) < order.index(wall_uuid),
                         {"sent_to_back": sp_res.get("sent_to_back"),
                          "space_index": order.index(space_uuid) if space_uuid in order else None,
                          "wall_index": order.index(wall_uuid) if wall_uuid in order else None})

        # order op
        o_res = report.run("order_setup", lambda: client.call("apply_operations", w(ops_params([
            {"op": "create", "local_ref": "o1", "object_type": "rect", "x1": 70000, "y1": 0, "x2": 71000, "y2": 1000},
            {"op": "create", "local_ref": "o2", "object_type": "rect", "x1": 70500, "y1": 500, "x2": 71500, "y2": 1500},
        ], "acc-order-setup"))))
        if o_res:
            o1 = o_res["transaction"]["operations"][0]["uuid"]
            front = report.run("order_front", lambda: client.call("apply_operations", w(ops_params(
                [{"op": "order", "target": f"uuid:{o1}", "position": "front"}], "acc-order-front"))))
            rec = front and front["transaction"]["operations"][0]
            report.check("order_front", bool(rec and rec.get("changed") and
                                             rec.get("stack_index_after") == rec.get("stack_count", 0) - 1), rec)
            back = report.run("order_backward", lambda: client.call("apply_operations", w(ops_params(
                [{"op": "order", "target": f"uuid:{o1}", "position": "backward"}], "acc-order-backward"))))
            rec2 = back and back["transaction"]["operations"][0]
            report.check("order_backward", bool(rec2 and rec2.get("changed") and
                                                rec2.get("stack_index_after") == rec2.get("stack_index_before", 0) - 1), rec2)

        # operation cap (#19): 2001 rejected before anything runs; 1000 creates timed
        too_many = [{"op": "create", "object_type": "rect", "x1": 0, "y1": 0, "x2": 1, "y2": 1}] * 2001
        try:
            client.call("apply_operations", w(ops_params(too_many, "acc-cap")))
            report.check("operation_cap_2000", False, "no error")
        except BridgeError as error:
            report.check("operation_cap_2000", error.code == "invalid_request", error.code)
        bulk_layer = f"ACC Bulk {ts}"
        report.run("layers_create_bulk", lambda: client.call("layers", w({"action": "create", "name": bulk_layer})))
        bulk = [{"op": "create", "object_type": "rect", "x1": 100000 + (i % 40) * 300, "y1": (i // 40) * 300,
                 "x2": 100000 + (i % 40) * 300 + 200, "y2": (i // 40) * 300 + 200, "layer": bulk_layer}
                for i in range(1000)]
        started = time.time()
        bulk_res = report.run("apply_operations_1000_rects", lambda: client.call(
            "apply_operations", w(ops_params(bulk, "acc-bulk")), timeout_ms=300000))
        elapsed = round(time.time() - started, 2)
        if bulk_res:
            report.check("apply_operations_1000_rects", bulk_res.get("ok") is True and
                         len(bulk_res["transaction"]["operations"]) == 1000,
                         {"elapsed_s": elapsed, "timing": bulk_res.get("timing")})
            report.check("apply_operations_1000_rects_fast", elapsed < 30,
                         {"elapsed_s": elapsed, "timing": bulk_res.get("timing")}, required=False)
            report.notes.append(f"1000 rect creates in one transaction: {elapsed} s "
                                f"(handler_ms {(bulk_res.get('timing') or {}).get('handler_ms')})")

        # --------------------------------------------- layers and classes
        lname = f"ACC Layer {int(time.time())}"
        lc = report.run("layers_create", lambda: client.call("layers", w({"action": "create", "name": lname, "kind": "design"})))
        report.check("layers_create", bool(lc and lc.get("created")))
        ls = report.run("layers_set", lambda: client.call("layers", w({"action": "set", "target": lname,
                                                                       "visibility": "gray", "opacity": 60})))
        report.check("layers_set_visibility", bool(ls and ls["layer"]["visible"] == "gray"), ls and ls.get("layer"))
        report.check("layers_set_opacity", bool(ls and ls["layer"].get("opacity") is not None and
                                                abs(ls["layer"]["opacity"] - 60) < 1), ls and ls.get("layer"),
                     required=False)
        ll = report.run("layers_list", lambda: client.call("layers", w({"action": "list"})))
        report.check("layers_list_contains_new", bool(ll and any(l["name"] == lname for l in ll["layers"])))
        svn = f"ACC View {int(time.time())}"
        report.run("saved_views_create", lambda: client.call("saved_views", w({"action": "create", "name": svn})),
                   required=False)
        svl = report.run("saved_views_list", lambda: client.call("saved_views", w({"action": "list"})))
        report.check("saved_views_roundtrip", bool(svl and any(v["name"] == svn for v in svl["saved_views"])),
                     required=False)
        report.run("saved_views_delete", lambda: client.call("saved_views", w({"action": "delete", "name": svn})),
                   required=False)

        # ------------------------------------------------------ run_script
        rs = report.run("run_script_result", lambda: client.call("run_script", w({
            "language": "python", "undo_label": "acceptance script",
            "code": "def area(w, h):\n    return w * h\nprint('hello')\nresult = {'area': area(3, 4), 'layer': vs.GetLName(vs.ActLayer())}\n"})))
        report.check("run_script_result", bool(rs and rs.get("ok") and rs["result"]["area"] == 12 and "hello" in rs["stdout"]),
                     rs)
        info_before = client.call("get_document_info", w({}))
        rf = report.run("run_script_failure", lambda: client.call("run_script", w({
            "language": "python", "undo_label": "acceptance failing script",
            "code": "vs.Rect(0, 0, 100, 100)\nvs.Rect(200, 0, 300, 100)\nraise RuntimeError('intentional failure')\n"})))
        info_after = client.call("get_document_info", w({}))
        report.check("run_script_failure_reported", bool(rf and rf.get("ok") is False and "intentional failure" in rf.get("error", "")), rf)
        report.check("run_script_failure_rolled_back",
                     bool(rf and rf.get("rolled_back")) and info_after["total_objects"] == info_before["total_objects"],
                     {"before": info_before["total_objects"], "after": info_after["total_objects"],
                      "rollback_verified": rf and rf.get("rollback_verified")})
        vsr = report.run("run_script_vectorscript", lambda: client.call("run_script", w({
            "language": "vectorscript", "code": "PROCEDURE T; BEGIN END; RUN(T);"})), required=False)
        report.check("run_script_vectorscript", bool(vsr and vsr.get("ok")), vsr, required=False)

        # -------------------------------------------- timeouts and jobs
        slow = report.run("still_running_conversion", lambda: client.raw("run_script", w({
            "language": "python", "transaction": False, "code": "import time\ntime.sleep(3)\nresult = 'slept'\n"}),
            timeout_ms=800))
        if slow:
            report.check("still_running_conversion", slow.get("error_code") == "still_running" and
                         "job_id" in (slow.get("details") or {}), slow)
            job_id = (slow.get("details") or {}).get("job_id")
            if job_id:
                status = None
                for _ in range(40):
                    status = client.call("job_status", {"job_id": job_id})
                    if status["state"] in ("done", "failed", "cancelled"):
                        break
                    time.sleep(0.25)
                report.check("job_status_done", bool(status and status["state"] == "done" and status.get("result", {}).get("result") == "slept"), status)
        # timeout_not_started: occupy the main thread, then send a short-timeout request
        busy = client.call("run_script", w({"language": "python", "transaction": False, "async": True,
                                            "code": "import time\ntime.sleep(2)\nresult = 1\n"}))
        quick = client.raw("get_view", w({}), timeout_ms=300)
        report.check("timeout_not_started", quick.get("error_code") == "timeout_not_started" and
                     quick.get("commit_state") == "not_started", quick)
        for _ in range(20):
            if client.call("job_status", {"job_id": busy["job_id"]})["state"] == "done":
                break
            time.sleep(0.25)

        # ----------------------------------------------------------- classes
        # Runs after run_script on purpose (see module docstring). manage_classes
        # set is native ISDK now (no Python engine).
        cname = f"ACC-Class-{int(time.time())}"
        report.run("classes_create", lambda: client.call("manage_classes", w({"action": "create", "class_name": cname})))
        cs = report.run("classes_set", lambda: client.call("manage_classes", w({
            "action": "set", "name": cname, "visibility": "hide", "fill_color": [255, 0, 0], "line_weight": 10})))
        report.check("classes_set", bool(cs and cs["class"]["visible"] == "hide"), cs)
        report.check("classes_set_readback", bool(cs and not cs.get("readback_mismatch")
                                                  and cs["class"].get("line_weight") == 10
                                                  and cs["class"].get("fill_color") == [255, 0, 0]),
                     cs and cs.get("class"), required=False)
        cl = report.run("classes_list_detailed", lambda: client.call("manage_classes", w({"action": "list", "detailed": True})))
        report.check("classes_list_detailed", bool(cl and any(c["name"] == cname and c["visible"] == "hide"
                                                              for c in cl["classes"])))
        legacy = report.run("classes_list_legacy_array", lambda: client.call("manage_classes", w({"action": "list"})))
        report.check("classes_list_legacy_array", isinstance(legacy, list) and cname in legacy)
        ping_engine = client.call("ping")
        report.check("script_engine_ok_after_classes", ping_engine.get("script_engine") == "ok",
                     ping_engine.get("script_engine"))

        if not args.skip_pdf:
            pdf = os.path.join(work_dir, "acceptance.pdf")
            job = report.run("export_pdf_async", lambda: client.call("export_pdf", w({
                "file_path": pdf, "range": "current_view", "dpi": 150, "async": True})))
            if job:
                report.check("export_pdf_async_queued", job.get("state") == "queued" and bool(job.get("job_id")), job)
                status = None
                for _ in range(240):
                    status = client.call("job_status", {"job_id": job["job_id"]})
                    if status["state"] in ("done", "failed", "cancelled"):
                        break
                    time.sleep(0.5)
                report.check("export_pdf_job_done", bool(status and status["state"] == "done" and os.path.isfile(pdf)), status)

        # ------------------------------------------------ IFC (#17)
        if not args.skip_ifc:
            ifc_ops = [
                {"op": "create", "local_ref": "iw1", "object_type": "wall", "x1": 0, "y1": 30000, "x2": 5000, "y2": 30000,
                 "thickness": 200, "height": 2800},
                {"op": "create", "local_ref": "iw2", "object_type": "wall", "x1": 5000, "y1": 30000, "x2": 5000,
                 "y2": 34000, "thickness": 200, "height": 2800},
                {"op": "create", "object_type": "space", "height": 2800, "name": "ACC IFC Room",
                 "points": [[0, 30000], [5000, 30000], [5000, 34000], [0, 34000]]},
            ]
            if schema and schema.get("descriptor_fingerprint"):
                ifc_ops.insert(2, {"op": "create", "object_type": "door", "x1": 2500, "y1": 30000, "width": 900,
                                   "height": 2100, "wall_uuid": "$iw1", "require_wall_host": True,
                                   "descriptor_fingerprint": schema["descriptor_fingerprint"]})
            report.run("ifc_model_create", lambda: client.call("apply_operations", w(ops_params(ifc_ops, "acc-ifc"))))
            ifc_path = os.path.join(work_dir, "acceptance.ifc")
            job = report.run("export_ifc_async", lambda: client.call("export_ifc", w({"file_path": ifc_path, "async": True})))
            status = wait_job(client, job["job_id"], 900) if job else None
            head = b""
            if os.path.isfile(ifc_path):
                with open(ifc_path, "rb") as handle:
                    head = handle.read(64)
            result = (status or {}).get("result") or {}
            report.check("export_ifc_file", bool(status and status["state"] == "done" and os.path.isfile(ifc_path)
                                                 and os.path.getsize(ifc_path) > 0
                                                 and (head.lstrip(b"\xef\xbb\xbf").lstrip().startswith(b"ISO-10303-21")
                                                      or head.startswith(b"PK"))),
                         {"state": (status or {}).get("state"), "error": (status or {}).get("error"),
                          "method": result.get("method"), "attempts": result.get("attempts"),
                          "elapsed_ms": (status or {}).get("elapsed_ms")})
            dialogs = result.get("dismissed_dialogs") or ((status or {}).get("details") or {}).get("dismissed_dialogs") or []
            reports = [d for d in dialogs if d.get("kind") in ("handler_report", "handler_message")]
            if reports:
                report.notes.append(f"IFC dialogs acknowledged: {json.dumps(reports)[:1500]}")
            grid_reports = [d for d in reports if d.get("grid")]
            if grid_reports:
                # The IFC Export Report grid rows are read (MSAA) before OK is pressed.
                report.check("ifc_report_rows_captured",
                             all(d.get("grid_rows_captured") is True and d.get("rows") for d in grid_reports),
                             [{k: d.get(k) for k in ("title", "grid_rows_captured", "grid_read", "rows")}
                              for d in grid_reports], required=False)
            ping_after_ifc = client.call("ping")
            report.check("ifc_report_dialog_auto_closed", not ping_after_ifc.get("modal_dialog"),
                         {"modal_dialog": ping_after_ifc.get("modal_dialog"), "acknowledged": reports})
            if status and status["state"] == "done" and os.path.isfile(ifc_path):
                ijob = report.run("import_ifc_async", lambda: client.call("import_ifc", w({"file_path": ifc_path, "async": True})),
                                  required=False)
                istatus = wait_job(client, ijob["job_id"], 900) if ijob else None
                iresult = (istatus or {}).get("result") or {}
                mutation = iresult.get("document_mutation") or {}
                report.check("import_ifc_compact_receipt",
                             bool(istatus and istatus["state"] == "done" and mutation.get("detail") is False
                                  and "created_object_uuids" not in mutation
                                  and (mutation.get("created_object_count") or 0) > 0),
                             {"state": (istatus or {}).get("state"), "error": (istatus or {}).get("error"),
                              "method": iresult.get("method"), "mutation": mutation}, required=False)

        # ------------------------------------------- stop is per connection
        side = Client(port, client.token)
        stop = side.raw("stop")
        closed = False
        try:
            closed = side.recv_frame(side.connect()) is None
        except OSError:
            closed = True
        side.close()
        report.check("stop_closes_only_connection",
                     stop.get("result") == {"connection_closed": True, "bridge_running": True} and closed
                     and client.call("ping").get("pong") is True, stop)

        # --------------------------------------------- unicode round trip
        uname = "ACC äöü € \U0001F600"
        up = {"idempotency_key": f"acc-uni-{time.time_ns()}", "operation_count": 1,
              "operation_1_json": json.dumps({"op": "create", "object_type": "text", "x1": 0, "y1": -2000,
                                              "text": uname}, ensure_ascii=True)}
        ur = report.run("unicode_text_roundtrip", lambda: client.call("apply_operations", w(up)), required=False)
        if ur:
            uuid = ur["transaction"]["operations"][0]["uuid"]
            items = client.call("query_objects", w({"mode": "criteria", "criteria": "ALL", "limit": 1000}))["items"]
            found = next((o for o in items if o.get("uuid") == uuid), None)
            report.check("unicode_text_roundtrip", bool(found and found.get("text") == uname),
                         found and found.get("text"), required=False)

        final_ping = client.call("ping")
        report.check("bridge_still_healthy", final_ping.get("pong") is True and final_ping.get("queue_depth") == 0,
                     {k: final_ping.get(k) for k in ("ui_state", "queue_depth", "handler_running_ms")})
    except (ConnectionError, OSError) as error:
        report.check("connection", False, f"{type(error).__name__}: {error}")
    finally:
        if not args.keep_documents:
            for fp in reversed(created):
                try:
                    open_count = len(client.call("list_documents").get("documents", []))
                    if open_count <= 1:
                        # Closing the last document would show the modal Home
                        # screen; open a blank one first so one document stays.
                        try:
                            keep = client.call("new_document", {}, timeout_ms=120000)
                            report.notes.append(f"opened {keep['document'].get('name')} so a document stays open")
                        except Exception as error:  # noqa: BLE001
                            report.notes.append(f"kept {fp} open (could not open a blank document: {error})")
                            continue
                    res = client.call("close_document", {"fingerprint": fp, "discard_changes": "discard"},
                                      timeout_ms=60000)
                    report.check(f"close_document_{fp}", bool(res.get("closed")), res)
                except Exception as error:  # noqa: BLE001
                    report.check(f"close_document_{fp}", False, str(error)[:300])
            try:
                final = client.call("ping")
                report.check("documents_remain_open", (final.get("open_document_count") or 0) >= 1
                             and not final.get("modal_dialog"),
                             {k: final.get(k) for k in ("open_document_count", "modal_dialog", "ui_state")})
            except Exception as error:  # noqa: BLE001
                report.check("documents_remain_open", False, str(error)[:300])
        if original_active:
            try:
                client.call("activate_document", {"fingerprint": original_active})
            except Exception as error:  # noqa: BLE001
                report.notes.append(f"could not re-activate the original document: {error}")
        client.close()

    summary = report.summary()
    text = json.dumps(summary, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    print(text)
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
