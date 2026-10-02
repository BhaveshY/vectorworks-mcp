"""Read-only probes for scripts/doctor.ps1 (stdlib only). Prints one JSON object.

  startup --launcher CMD [--timeout S] [--runs N]
      Spawn the MCP launcher exactly like Codex/Claude do (cmd.exe /d /c CMD),
      send initialize + notifications/initialized + tools/list, and time
      spawn->initialize response and spawn->tools/list response.
  bridge --root DIR [--timeout S]
      Read instances/*.json discovery files (prune nothing), then send the native
      `ping` action (4-byte big-endian length + JSON) to each live instance and
      to 127.0.0.1:9877 when no discovery file exists.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

CMD_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")


def emit(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    return 0


def _reader(stream, sink: list, events: dict, started: float):
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        now = time.perf_counter() - started
        try:
            msg = json.loads(line)
        except ValueError:
            sink.append({"t": now, "garbage": line[:200]})
            continue
        sink.append({"t": now, "msg": msg})
        if isinstance(msg, dict) and "id" in msg:
            events.setdefault(msg["id"], now)


def startup_once(launcher: str, timeout: float) -> dict:
    env = dict(os.environ)
    started = time.perf_counter()
    proc = subprocess.Popen([CMD_EXE, "/d", "/c", launcher], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    frames: list = []
    events: dict = {}
    t = threading.Thread(target=_reader, args=(proc.stdout, frames, events, started), daemon=True)
    t.start()
    errbuf: list = []
    te = threading.Thread(target=lambda: errbuf.append(proc.stderr.read()), daemon=True)
    te.start()

    def send(obj):
        proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        proc.stdin.flush()

    result = {"ok": False}
    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "vectorworks-doctor", "version": "1"}}})
        deadline = started + timeout
        while 1 not in events and time.perf_counter() < deadline and proc.poll() is None:
            time.sleep(0.005)
        if 1 in events:
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            while 2 not in events and time.perf_counter() < deadline and proc.poll() is None:
                time.sleep(0.005)
        init = next((f["msg"] for f in frames if "msg" in f and f["msg"].get("id") == 1), None)
        tools = next((f["msg"] for f in frames if "msg" in f and f["msg"].get("id") == 2), None)
        result["initialize_ms"] = round(events[1] * 1000) if 1 in events else None
        result["tools_list_ms"] = round(events[2] * 1000) if 2 in events else None
        if init and "result" in init:
            info = init["result"].get("serverInfo") or {}
            result["server_name"] = info.get("name")
            result["server_version"] = info.get("version")
            result["protocol_version"] = init["result"].get("protocolVersion")
        if tools and "result" in tools:
            names = [t.get("name") for t in tools["result"].get("tools", [])]
            result["tools"] = names
            result["tools_list_bytes"] = len(json.dumps(tools["result"]))
        result["garbage_on_stdout"] = [f["garbage"] for f in frames if "garbage" in f]
        result["repair_mode"] = result.get("server_name") == "vectorworks-mcp-repair"
        result["ok"] = bool(init and tools and not result["garbage_on_stdout"])
        if not result["ok"] and proc.poll() is not None:
            result["exit_code"] = proc.returncode
    finally:
        try:
            proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
        te.join(timeout=2)
        err = (errbuf[0] if errbuf else b"").decode("utf-8", "replace").strip()
        if err:
            result["stderr_tail"] = err[-800:]
    return result


def cmd_startup(args) -> int:
    runs = [startup_once(args.launcher, args.timeout) for _ in range(max(1, args.runs))]
    best = min((r for r in runs if r.get("tools_list_ms") is not None), key=lambda r: r["tools_list_ms"], default=runs[-1])
    out = dict(best)
    out["runs"] = [{k: r.get(k) for k in ("initialize_ms", "tools_list_ms", "ok")} for r in runs]
    out["first_run"] = out["runs"][0]
    return emit(out)


def _frame(sock: socket.socket, payload: dict, timeout: float) -> dict:
    data = json.dumps(payload).encode("utf-8")
    sock.settimeout(timeout)
    sock.sendall(struct.pack(">I", len(data)) + data)
    head = b""
    while len(head) < 4:
        chunk = sock.recv(4 - len(head))
        if not chunk:
            raise ConnectionError("bridge closed the connection")
        head += chunk
    (size,) = struct.unpack(">I", head)
    if size > 16 * 1024 * 1024:
        raise ValueError("frame too large: %d" % size)
    body = b""
    while len(body) < size:
        chunk = sock.recv(min(65536, size - len(body)))
        if not chunk:
            raise ConnectionError("bridge closed mid-frame")
        body += chunk
    return json.loads(body.decode("utf-8"))


def _pid_alive(pid: int) -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"], capture_output=True, text=True, timeout=10)
        return str(pid) in out.stdout
    except Exception:
        return True


def cmd_bridge(args) -> int:
    root = Path(args.root)
    token = ""
    token_file = Path(os.environ.get("VW_MCP_AUTH_TOKEN_FILE") or root / "auth-token")
    try:
        token = token_file.read_text(encoding="utf-8-sig").strip().splitlines()[0]
    except (OSError, IndexError):
        pass
    report = {"ok": True, "token_present": bool(token), "instances": [], "pings": []}
    targets = []
    inst_dir = root / "instances"
    for f in sorted(inst_dir.glob("*.json")) if inst_dir.is_dir() else []:
        try:
            info = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            report["instances"].append({"file": str(f), "error": str(exc)})
            continue
        pid = int(info.get("pid") or 0)
        alive = _pid_alive(pid) if pid else False
        entry = {"file": f.name, "pid": pid, "port": info.get("port"), "alive": alive,
                 "capability_revision": info.get("capability_revision"), "bridge_version": info.get("bridge_version"),
                 "build_id": info.get("build_id"), "vectorworks_version": info.get("vectorworks_version")}
        report["instances"].append(entry)
        if alive and info.get("port"):
            targets.append(("discovery", int(info["port"])))
    if not targets:
        targets.append(("legacy-default", 9877))
    for source, port in targets:
        ping = {"source": source, "port": port}
        started = time.perf_counter()
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=args.timeout) as sock:
                resp = _frame(sock, {"id": "doctor-ping", "action": "ping", "auth_token": token, "params": {}}, args.timeout)
            ping["ms"] = round((time.perf_counter() - started) * 1000)
            ping["success"] = bool(resp.get("success"))
            res = resp.get("result") if isinstance(resp.get("result"), dict) else {}
            for key in ("protocol", "capability_revision", "bridge_version", "build_id", "pid", "ui_state",
                        "vectorworks_version", "open_document_count", "main_context_idle_ms", "native_phase"):
                if key in res:
                    ping[key] = res[key]
            if isinstance(res.get("active_document"), dict):
                ping["active_document"] = res["active_document"].get("name")
            if not resp.get("success"):
                ping["error"] = str(resp.get("error"))[:300]
                ping["error_code"] = resp.get("error_code")
        except Exception as exc:
            ping["success"] = False
            ping["error"] = "%s: %s" % (exc.__class__.__name__, exc)
        report["pings"].append(ping)
    return emit(report)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("startup")
    s.add_argument("--launcher", required=True)
    s.add_argument("--timeout", type=float, default=20)
    s.add_argument("--runs", type=int, default=2)
    b = sub.add_parser("bridge")
    b.add_argument("--root", required=True)
    b.add_argument("--timeout", type=float, default=3)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    return cmd_startup(args) if args.cmd == "startup" else cmd_bridge(args)


if __name__ == "__main__":
    raise SystemExit(main())
