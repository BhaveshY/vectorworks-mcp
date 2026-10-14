"""Drive the installed Vectorworks MCP server like an agent, from a shell.

One long-lived MCP session (the installed launcher, exactly as Codex starts it)
is held by a small daemon; each `call` sends one tools/call over it, prints the
structured result (compact) and saves any image content to disk so it can be
inspected. Document pinning and connection reuse therefore behave as in a real
client session.

    py -3.12 scripts\\mcp_drive.py start                       # start the session daemon
    py -3.12 scripts\\mcp_drive.py call vw_status '{}'
    py -3.12 scripts\\mcp_drive.py call vw_create @args.json    # args from a file
    py -3.12 scripts\\mcp_drive.py tools                        # tools/list summary
    py -3.12 scripts\\mcp_drive.py stop

Images are written to %TEMP%\\vw-drive\\NNN-<tool>.<ext>; a call log with
timings goes to %TEMP%\\vw-drive\\calls.jsonl.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PORT = int(os.environ.get("VW_DRIVE_PORT", "8765"))
OUT = Path(os.environ.get("TEMP", ".")) / "vw-drive"
LAUNCHER = Path(os.environ["USERPROFILE"]) / ".vectorworks-mcp" / "bin" / "vectorworks-mcp.cmd"


def _recv_line(conn: socket.socket) -> bytes:
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = conn.recv(65536)
        if not chunk:
            break
        buf += chunk
    return buf


def daemon() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["cmd.exe", "/d", "/c", str(LAUNCHER)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=open(OUT / "server-stderr.log", "ab"))
    lock = threading.Lock()
    state = {"id": 0, "n": 0}

    def rpc(method: str, params: dict) -> dict:
        with lock:
            state["id"] += 1
            rid = state["id"]
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("MCP server exited")
                msg = json.loads(line)
                if msg.get("id") == rid:
                    return msg

    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "mcp-drive", "version": "1"}})
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.flush()
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    srv.bind(("127.0.0.1", PORT))
    srv.listen(4)
    (OUT / "daemon.json").write_text(json.dumps({"pid": os.getpid(), "server": init.get("result", {}).get("serverInfo")}))
    while True:
        conn, _ = srv.accept()
        with conn:
            req = json.loads(_recv_line(conn) or b"{}")
            if req.get("op") == "stop":
                conn.sendall(b'{"stopped":true}\n')
                proc.stdin.close()
                return
            t0 = time.perf_counter()
            try:
                if req.get("op") == "tools":
                    reply = rpc("tools/list", {})
                else:
                    reply = rpc("tools/call", {"name": req["tool"], "arguments": req.get("args") or {}})
            except Exception as exc:  # noqa: BLE001
                reply = {"error": {"message": str(exc)}}
            ms = round((time.perf_counter() - t0) * 1000)
            state["n"] += 1
            images = []
            result = reply.get("result") or {}
            for idx, block in enumerate(result.get("content") or []):
                if block.get("type") == "image":
                    ext = "jpg" if "jpeg" in block.get("mimeType", "") else "png"
                    path = OUT / f"{state['n']:03d}-{req.get('tool', 'x')}-{idx}.{ext}"
                    path.write_bytes(base64.b64decode(block["data"]))
                    images.append(str(path))
            text_bytes = sum(len(b.get("text", "")) for b in result.get("content") or [] if b.get("type") == "text")
            with open(OUT / "calls.jsonl", "a", encoding="utf-8") as log:
                log.write(json.dumps({"n": state["n"], "tool": req.get("tool"), "ms": ms, "text_bytes": text_bytes,
                                      "is_error": result.get("isError"), "args": req.get("args")}) + "\n")
            conn.sendall((json.dumps({"ms": ms, "images": images, "text_bytes": text_bytes, "reply": reply}) + "\n").encode())


def send(payload: dict, timeout: float = 400) -> dict:
    with socket.create_connection(("127.0.0.1", PORT), timeout=timeout) as conn:
        conn.sendall((json.dumps(payload) + "\n").encode())
        return json.loads(_recv_line(conn))


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "start":
        subprocess.Popen([sys.executable, __file__, "_daemon"], creationflags=0x00000008 | 0x00000200,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            time.sleep(0.2)
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=1):
                    print("session daemon up on", PORT)
                    return 0
            except OSError:
                pass
        print("daemon did not start")
        return 1
    if cmd == "_daemon":
        daemon()
        return 0
    if cmd == "stop":
        print(send({"op": "stop"}))
        return 0
    if cmd == "tools":
        r = send({"op": "tools"})
        tools = r["reply"]["result"]["tools"]
        print(len(tools), "tools,", len(json.dumps(tools)), "bytes")
        for t in tools:
            print(f"- {t['name']}: {t.get('description', '')[:110]}")
        return 0
    if cmd == "call":
        tool = argv[1]
        raw = argv[2] if len(argv) > 2 else "{}"
        args = json.loads(Path(raw[1:]).read_text(encoding="utf-8")) if raw.startswith("@") else json.loads(raw)
        limit = int(os.environ.get("VW_DRIVE_LIMIT", "3500"))
        r = send({"op": "call", "tool": tool, "args": args})
        res = r["reply"].get("result") or {}
        sc = res.get("structuredContent")
        body = json.dumps(sc if sc is not None else r["reply"], ensure_ascii=False)
        print(f"[{tool}] {r['ms']} ms, isError={res.get('isError')}, text={r['text_bytes']} B, images={r['images']}")
        print(body[:limit] + (" ...(truncated)" if len(body) > limit else ""))
        return 0
    print("unknown command", cmd)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
