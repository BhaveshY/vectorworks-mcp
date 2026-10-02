"""Fixture tests for scripts/register_clients.py (stdlib only, Python >= 3.11).

Covers: surgical replacement of [mcp_servers.vectorworks] + sub-tables with
comments and unrelated content preserved, CRLF + BOM, inline-table forms,
multi-line strings that look like headers, grace-period raise-only rule,
marketplace switch, duplicate plugin server disabling, idempotency,
unregister, and the Claude JSON merge (key order + other keys preserved).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
HELPER = HERE / "register_clients.py"
LAUNCHER = r"C:\Users\Jane Doe\.vectorworks-mcp\bin\vectorworks-mcp.cmd"
MARKET = r"C:\Users\Jane Doe\.vectorworks-mcp\marketplace"
FAIL: list[str] = []


def check(cond: bool, label: str) -> None:
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        FAIL.append(label)


def run(*args: str) -> dict:
    p = subprocess.run([sys.executable, "-I", str(HELPER), *args], capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(p.stdout)
    except ValueError:
        return {"ok": False, "error": "no json: " + p.stdout + p.stderr}


BASE = '''# my codex config
model = "gpt-x"   # keep this comment
mcp_optional_startup_grace_ms = 1500

notes = """
[mcp_servers.vectorworks]
this is a string, not a table
"""

[marketplaces.vectorworks-mcp]
source_type = "git"
source = "https://github.com/BhaveshY/vectorworks-mcp.git"
ref = "main"

[plugins."vectorworks@vectorworks-mcp"]
enabled = true

[plugins."other@x"]
enabled = true

[mcp_servers.revit]
command = "cmd"
args = ["/c", 'C:\\x\\revit.cmd']

# comment before vectorworks
[mcp_servers.vectorworks]
command = "powershell.exe"
args = ["-File", "C:/Users/Jane/AppData/Local/VectorworksMCP/0.7.0/scripts/run-mcp-server.ps1"]

[mcp_servers.vectorworks.env]
VW_MCP_PORT = "9877"

[projects.'c:\\work']
trust_level = "trusted"
'''


def stale_plugin_cache(home: Path, key: str) -> None:
    name, _, market = key.partition("@")
    d = home / "plugins" / "cache" / market / name / "0.7.0"
    (d / ".codex-plugin").mkdir(parents=True)
    (d / ".codex-plugin" / "plugin.json").write_text(json.dumps({"name": name, "mcpServers": "./.mcp.json"}))
    (d / ".mcp.json").write_text(json.dumps({"mcpServers": {"vectorworks": {"command": "powershell.exe"}}}))


def codex_tests(tmp: Path) -> None:
    for variant in ("lf", "crlf_bom"):
        home = tmp / ("codex_" + variant)
        home.mkdir()
        cfg = home / "config.toml"
        text = BASE if variant == "lf" else BASE.replace("\n", "\r\n")
        cfg.write_bytes((b"\xef\xbb\xbf" if variant != "lf" else b"") + text.encode())
        stale_plugin_cache(home, "vectorworks@vectorworks-mcp")
        stale_plugin_cache(home, "other@x")  # an unrelated plugin that also defines vectorworks
        before = tomllib.loads(text)
        r = run("codex", "--config", str(cfg), "--launcher", LAUNCHER, "--marketplace-dir", MARKET)
        check(r.get("ok") is True and r.get("written") is True, f"[{variant}] edit ok + written")
        raw = cfg.read_bytes()
        check(raw.startswith(b"\xef\xbb\xbf") == (variant != "lf"), f"[{variant}] BOM preserved")
        new = raw.decode("utf-8-sig")
        check(("\r\n" in new) == (variant != "lf"), f"[{variant}] newline style preserved")
        doc = tomllib.loads(new)
        srv = doc["mcp_servers"]["vectorworks"]
        check(srv["args"] == ["/d", "/c", LAUNCHER] and srv["startup_timeout_sec"] == 30 and srv["tool_timeout_sec"] == 180,
              f"[{variant}] server entry correct")
        check("env" not in srv, f"[{variant}] old env sub-table removed (no VW_MCP_PORT)")
        check(doc["mcp_optional_startup_grace_ms"] == 5000, f"[{variant}] grace raised to 5000")
        check(doc["notes"] == before["notes"], f"[{variant}] multi-line string untouched")
        check(doc["mcp_servers"]["revit"] == before["mcp_servers"]["revit"], f"[{variant}] other servers untouched")
        check(doc["projects"] == before["projects"] and doc["model"] == "gpt-x", f"[{variant}] unrelated tables untouched")
        check("# keep this comment" in new and "# comment before vectorworks" in new, f"[{variant}] comments kept")
        check(doc["marketplaces"]["vectorworks-mcp"] == {"source_type": "local", "source": "\\\\?\\" + MARKET},
              f"[{variant}] marketplace -> local")
        check(doc["plugins"]["vectorworks@vectorworks-mcp"]["enabled"] is False, f"[{variant}] stale-cache plugin disabled")
        check(doc["plugins"]["other@x"]["enabled"] is False, f"[{variant}] duplicate-server plugin disabled")
        check(len(r.get("definitions_after", [])) == 1, f"[{variant}] exactly one active definition")
        backups = list(home.glob("config.toml.vectorworks-mcp-*.bak"))
        check(len(backups) == 1 and backups[0].read_bytes() == (b"\xef\xbb\xbf" if variant != "lf" else b"") + text.encode(),
              f"[{variant}] byte-exact backup")
        r2 = run("codex", "--config", str(cfg), "--launcher", LAUNCHER, "--marketplace-dir", MARKET)
        check(r2.get("written") is False and r2.get("changes") == ["already up to date"], f"[{variant}] idempotent")
        r3 = run("codex", "--config", str(cfg), "--launcher", LAUNCHER, "--unregister", "--marketplace-dir", MARKET)
        doc3 = tomllib.loads(cfg.read_bytes().decode("utf-8-sig"))
        check(r3.get("ok") and "vectorworks" not in doc3.get("mcp_servers", {}) and "revit" in doc3["mcp_servers"],
              f"[{variant}] unregister removes only ours")

    # grace never lowered; inline-table form replaced
    home = tmp / "codex_inline"
    home.mkdir()
    cfg = home / "config.toml"
    cfg.write_text('mcp_optional_startup_grace_ms = 9000\n\n[mcp_servers]\nvectorworks = { command = "x.exe" }\nother = { command = "y.exe" }\n',
                   encoding="utf-8")
    r = run("codex", "--config", str(cfg), "--launcher", LAUNCHER)
    doc = tomllib.loads(cfg.read_text(encoding="utf-8"))
    check(r.get("ok") and doc["mcp_optional_startup_grace_ms"] == 9000, "[inline] higher grace kept")
    check(doc["mcp_servers"]["vectorworks"]["command"].lower().endswith("cmd.exe") and doc["mcp_servers"]["other"]["command"] == "y.exe",
          "[inline] inline vectorworks replaced, other kept")

    # foreign launcher kept on unregister
    r = run("codex", "--config", str(cfg), "--launcher", r"C:\other\launcher.cmd", "--unregister")
    check("vectorworks" in tomllib.loads(cfg.read_text(encoding="utf-8"))["mcp_servers"], "[unregister] foreign entry kept")

    # invalid TOML is never touched
    bad = home / "bad.toml"
    bad.write_text("this is = = not toml", encoding="utf-8")
    r = run("codex", "--config", str(bad), "--launcher", LAUNCHER)
    check(r.get("ok") is False and bad.read_text(encoding="utf-8") == "this is = = not toml", "[invalid] refused, untouched")


def claude_tests(tmp: Path) -> None:
    cfg = tmp / ".claude.json"
    original = {"numStartups": 3, "projects": {"C:/p": {"mcpServers": {}}}, "mcpServers": {"other": {"command": "z"}},
                "oauthAccount": {"emailAddress": "x@example.com"}, "tipsHistory": {"a": 1}}
    cfg.write_text(json.dumps(original, indent=2), encoding="utf-8")
    r = run("claude", "--config", str(cfg), "--launcher", LAUNCHER)
    data = json.loads(cfg.read_text(encoding="utf-8"))
    check(r.get("ok") and data["mcpServers"]["vectorworks"]["args"] == ["/d", "/c", LAUNCHER], "[claude] entry added")
    check(list(data.keys()) == list(original.keys()) and data["oauthAccount"] == original["oauthAccount"]
          and data["mcpServers"]["other"] == {"command": "z"}, "[claude] other keys and order preserved")
    r2 = run("claude", "--config", str(cfg), "--launcher", LAUNCHER)
    check(r2.get("written") is False, "[claude] idempotent")
    r3 = run("claude", "--config", str(cfg), "--launcher", LAUNCHER, "--unregister")
    check(r3.get("ok") and "vectorworks" not in json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"], "[claude] unregister")


def main() -> int:
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        codex_tests(tmp)
        claude_tests(tmp)
    print("\n%s" % ("all register-clients checks passed" if not FAIL else "%d check(s) failed" % len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
