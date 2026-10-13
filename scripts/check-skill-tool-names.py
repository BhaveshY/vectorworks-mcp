"""Fail when skills/docs mention a vw_* tool (or an argument) the server does not have.

Sources of truth:
  * docs/V1_CONTRACT.md section 2.3 table (always), and optionally
  * the live tools/list (--live [--launcher CMD]) or a saved one (--tools-json FILE).

Checked files: plugins/**/*.md, top-level *.md, docs/*.md (except the contract,
docs/PARITY.md). Text between
<!-- tool-check: legacy-begin --> and <!-- tool-check: legacy-end --> may name
removed 0.7 tools (old -> new maps).

With a tools/list, example calls such as  vw_read(what="query", type="wall")
or  vw_create({"objects": [...]})  are parsed and every top-level argument
name must exist in that tool's inputSchema.properties.

Exit code 0 = clean, 1 = problems. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TOKEN = re.compile(r"(?<![A-Za-z0-9_])vw_[a-z][a-z0-9_]*")
CALL = re.compile(r"(?<![A-Za-z0-9_])(vw_[a-z][a-z0-9_]*)\(")
LEGACY_BEGIN = "<!-- tool-check: legacy-begin -->"
LEGACY_END = "<!-- tool-check: legacy-end -->"


def contract_tools() -> list[str]:
    text = (REPO / "docs" / "V1_CONTRACT.md").read_text(encoding="utf-8")
    m = re.search(r"### 2\.3 .*?\n(.*?)\n### ", text, re.S)
    if not m:
        raise SystemExit("cannot find section 2.3 in docs/V1_CONTRACT.md")
    names = re.findall(r"^\|\s*`(vw_[a-z_]+)`\s*\|", m.group(1), re.M)
    if not names:
        raise SystemExit("no tools found in the section 2.3 table")
    return names


def files_to_check(extra: list[str]) -> list[Path]:
    out = sorted((REPO / "plugins").rglob("*.md"))
    out += sorted(p for p in REPO.glob("*.md"))
    out += sorted(p for p in (REPO / "docs").glob("*.md") if p.name not in {"V1_CONTRACT.md", "PARITY.md"})
    out += [Path(e) for e in extra]
    return [p for p in out if p.is_file()]


def strip_legacy(text: str) -> str:
    result, pos = [], 0
    while True:
        b = text.find(LEGACY_BEGIN, pos)
        if b < 0:
            result.append(text[pos:])
            break
        result.append(text[pos:b])
        e = text.find(LEGACY_END, b)
        if e < 0:
            break
        # keep line count stable for line numbers
        result.append("\n" * text[b:e].count("\n"))
        pos = e + len(LEGACY_END)
    return "".join(result)


def call_args(text: str, start: int) -> tuple[list[str], int] | None:
    """Top-level argument names of the call whose '(' is at text[start]."""
    depth, i, n = 0, start, len(text)
    quote = None
    args_text_start = start + 1
    while i < n:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                break
        elif ch == "\n" and depth == 1 and text[i + 1:i + 2] == "\n":
            return None  # prose, not a call
        i += 1
    if depth != 0:
        return None
    body = text[args_text_start:i].strip()
    if not body:
        return [], i
    if body.startswith("{"):
        try:
            obj = json.loads(body)
        except ValueError:
            return None
        return (list(obj.keys()) if isinstance(obj, dict) else None), i
    names, d, q, j = [], 0, None, 0
    while j < len(body):
        ch = body[j]
        if q:
            if ch == "\\":
                j += 2
                continue
            if ch == q:
                q = None
        elif ch in "\"'":
            q = ch
        elif ch in "([{":
            d += 1
        elif ch in ")]}":
            d -= 1
        elif d == 0:
            m = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*=(?!=)", body[j:])
            if m and (j == 0 or body[j - 1] in " ,\n\t("):
                names.append(m.group(1))
                j += m.end()
                continue
        j += 1
    return names, i


def fetch_live(launcher: str | None) -> list[dict]:
    if launcher:
        cmd = [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"), "/d", "/c", launcher]
        env = dict(os.environ)
    else:
        boot = "import sys,runpy; sys.path.insert(0, %r); runpy.run_module('vectorworks_mcp', run_name='__main__')" % str(REPO)
        cmd = [sys.executable, "-X", "utf8", "-u", "-c", boot]
        env = dict(os.environ, PYTHONUTF8="1")
    msgs = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                                        "clientInfo": {"name": "tool-check", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    proc = subprocess.run(cmd, input="".join(json.dumps(m) + "\n" for m in msgs).encode(), capture_output=True,
                          timeout=60, env=env)
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if msg.get("id") == 2 and "result" in msg:
            return msg["result"]["tools"]
    raise SystemExit("could not read tools/list from the server:\n" + proc.stderr.decode("utf-8", "replace")[-2000:])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="start the server and check against its tools/list")
    ap.add_argument("--launcher", help="with --live: use this vectorworks-mcp.cmd instead of the checkout")
    ap.add_argument("--tools-json", help="a saved tools/list result (or a list of tools)")
    ap.add_argument("files", nargs="*")
    args = ap.parse_args()

    allowed = set(contract_tools())
    problems: list[str] = []
    schemas: dict[str, dict] = {}
    tools = None
    if args.tools_json:
        data = json.loads(Path(args.tools_json).read_text(encoding="utf-8"))
        tools = data.get("tools", data.get("result", {}).get("tools")) if isinstance(data, dict) else data
    elif args.live or args.launcher:
        tools = fetch_live(args.launcher)
    if tools is not None:
        live = {t["name"] for t in tools}
        schemas = {t["name"]: t.get("inputSchema") or {} for t in tools}
        if live != allowed:
            problems.append("tools/list %s differs from the contract %s (missing: %s, extra: %s)" % (
                sorted(live), sorted(allowed), sorted(allowed - live), sorted(live - allowed)))
        allowed &= live

    checked = 0
    for path in files_to_check(args.files):
        raw = path.read_text(encoding="utf-8", errors="replace")
        text = strip_legacy(raw)
        rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
        for m in TOKEN.finditer(text):
            name = m.group(0)
            after = text[m.end():m.end() + 4]
            if after.startswith((".py", ".cmd", ".ps1")) or name in allowed:
                continue
            line = text.count("\n", 0, m.start()) + 1
            problems.append("%s:%d: unknown tool %s" % (rel, line, name))
        if schemas:
            for m in CALL.finditer(text):
                name = m.group(1)
                if name not in schemas:
                    continue
                parsed = call_args(text, m.end() - 1)
                if parsed is None:
                    continue
                props = set((schemas[name].get("properties") or {}).keys())
                for arg in parsed[0]:
                    if arg not in props:
                        line = text.count("\n", 0, m.start()) + 1
                        problems.append("%s:%d: %s has no argument %r (schema: %s)" % (rel, line, name, arg, ", ".join(sorted(props))))
        checked += 1

    for p in problems:
        print("FAIL " + p)
    print("%d file(s) checked against %d tool(s)%s: %s" % (
        checked, len(allowed), " + live schemas" if schemas else "", "OK" if not problems else "%d problem(s)" % len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
