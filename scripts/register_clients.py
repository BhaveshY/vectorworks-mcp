"""Client registration helper for Vectorworks MCP (stdlib only, Python >= 3.11).

Called by scripts/register-clients.ps1 and scripts/doctor.ps1. Every command
prints one JSON object to stdout.

  codex   --config FILE --launcher CMD [--marketplace-dir DIR] [--unregister] [--dry-run]
          Surgically edits Codex config.toml: replaces only [mcp_servers.vectorworks]
          (and its sub-tables), raises mcp_optional_startup_grace_ms to >= 5000,
          points the vectorworks-mcp marketplace at DIR and disables duplicate
          vectorworks plugin servers. Everything else (comments, order, quoting)
          is preserved byte for byte. The result is re-parsed and compared with
          the expected document before it is written; a backup is kept.
  claude  --config FILE --launcher CMD [--unregister] [--dry-run]
          Merges mcpServers.vectorworks into ~/.claude.json (JSON round trip with
          key order preserved; never ConvertTo-Json).
  inspect --codex-home DIR --claude-config FILE [--launcher CMD]
          Read-only analysis used by doctor: active vectorworks definitions per
          client, duplicates, stale plugins.
  codex-logs --codex-home DIR [--days N]
          Read-only summary of recent vectorworks MCP startup events in Codex logs.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    tomllib = None

SERVER = "vectorworks"
MARKETPLACE = "vectorworks-mcp"
PLUGIN_KEY = "vectorworks@vectorworks-mcp"
GRACE_KEY = "mcp_optional_startup_grace_ms"
GRACE_MS = 5000
STARTUP_TIMEOUT = 30
TOOL_TIMEOUT = 180
CMD_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
ENV_VARS = ["USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "SystemRoot", "PATH", "PATHEXT", "ComSpec"]


def out(obj) -> int:
    sys.stdout.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
    return 0 if obj.get("ok", True) else 1


def stamp() -> str:
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(p.replace("\\\\?\\", ""))) if p else ""


def server_points_at(defn: dict, launcher: str) -> bool:
    if not isinstance(defn, dict) or not launcher:
        return False
    parts = [str(defn.get("command", ""))] + [str(a) for a in (defn.get("args") or [])]
    return any(norm(p) == norm(launcher) for p in parts)


# --------------------------------------------------------------------------
# Minimal TOML line scanner (structure only; values are validated by tomllib)
# --------------------------------------------------------------------------

HEADER_RE = re.compile(r"^\s*(\[\[?)\s*(?P<key>[^\]\[#][^#]*?)\s*(\]\]?)\s*(#.*)?$")


def split_key(text: str) -> list[str]:
    """Split a dotted TOML key into its unquoted parts ([] when it is not a key)."""
    try:
        return _split_key(text)
    except (ValueError, IndexError):
        return []


def string_interior_lines(lines: list[str]) -> set[int]:
    """Indexes of lines that lie inside a multi-line string (after its opening line)."""
    inside: set[int] = set()
    in_ml = None
    for idx, line in enumerate(lines):
        if in_ml:
            inside.add(idx)
            if line.count(in_ml) % 2 == 1:
                in_ml = None
            continue
        code = line.split("#")[0]
        for delim in ('"""', "'''"):
            if code.count(delim) % 2 == 1:
                in_ml = delim
                break
    return inside


def _split_key(text: str) -> list[str]:
    parts, buf, i = [], "", 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            j = i + 1
            while j < len(text) and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            parts.append(json.loads(text[i : j + 1]))
            i = j + 1
            buf = None
        elif ch == "'":
            j = text.index("'", i + 1)
            parts.append(text[i + 1 : j])
            i = j + 1
            buf = None
        elif ch == ".":
            if buf:
                parts.append(buf.strip())
            buf = ""
            i += 1
        else:
            if buf is None:
                buf = ""
            buf += ch
            i += 1
    if buf and buf.strip():
        parts.append(buf.strip())
    return [p for p in parts if p != ""]


def scan_blocks(lines: list[str]):
    """Return [(start, end, key_parts|None, is_array)]; block 0 = top level."""
    blocks = []
    start, key, arr = 0, None, False
    in_ml = None
    for idx, line in enumerate(lines):
        if in_ml:
            if line.count(in_ml) % 2 == 1:
                in_ml = None
            continue
        m = HEADER_RE.match(line)
        if m and not line.lstrip().startswith(("'", '"')) and "=" not in line.split("#")[0].split("]")[0]:
            blocks.append((start, idx, key, arr))
            start, key, arr = idx, split_key(m.group("key")), m.group(1) == "[["
            continue
        code = line.split("#")[0]
        for delim in ('"""', "'''"):
            if code.count(delim) % 2 == 1:
                in_ml = delim
                break
    blocks.append((start, len(lines), key, arr))
    return blocks


def toml_literal(s: str) -> str:
    if "'" not in s and "\n" not in s:
        return "'" + s + "'"
    return json.dumps(s)


def toml_key(part: str) -> str:
    return part if re.fullmatch(r"[A-Za-z0-9_-]+", part) else json.dumps(part)


def server_block(launcher: str, nl: str) -> list[str]:
    args = ", ".join(toml_literal(a) for a in ["/d", "/c", launcher])
    env_vars = ", ".join(toml_literal(v) for v in ENV_VARS)
    return [
        "[mcp_servers.vectorworks]",
        "# Managed by Vectorworks MCP install.ps1 (scripts/register-clients.ps1).",
        "command = " + toml_literal(CMD_EXE),
        "args = [" + args + "]",
        "env_vars = [" + env_vars + "]",
        "startup_timeout_sec = %d" % STARTUP_TIMEOUT,
        "tool_timeout_sec = %d" % TOOL_TIMEOUT,
        "enabled = true",
        "",
    ]


def expected_server(launcher: str) -> dict:
    return {
        "command": CMD_EXE,
        "args": ["/d", "/c", launcher],
        "env_vars": list(ENV_VARS),
        "startup_timeout_sec": STARTUP_TIMEOUT,
        "tool_timeout_sec": TOOL_TIMEOUT,
        "enabled": True,
    }


def marketplace_source(path: str) -> str:
    full = os.path.abspath(path)
    return full if full.startswith("\\\\?\\") else "\\\\?\\" + full


# --------------------------------------------------------------------------
# Codex
# --------------------------------------------------------------------------

def plugin_cache_servers(codex_home: Path, plugin_key: str) -> list[dict]:
    """MCP servers defined by the newest cached copy of an installed plugin."""
    name, _, market = plugin_key.partition("@")
    base = codex_home / "plugins" / "cache" / market / name
    found = []
    if not base.is_dir():
        return found
    versions = sorted([d for d in base.iterdir() if d.is_dir()], key=lambda d: d.stat().st_mtime, reverse=True)
    for vdir in versions[:1]:
        manifest = vdir / ".codex-plugin" / "plugin.json"
        try:
            data = json.loads(manifest.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        ref = data.get("mcpServers")
        servers = {}
        if isinstance(ref, dict):
            servers = ref.get("mcpServers", ref)
        elif isinstance(ref, str):
            try:
                servers = json.loads((vdir / ref).read_text(encoding="utf-8-sig")).get("mcpServers", {})
            except (OSError, ValueError):
                servers = {}
        for sname, sdef in (servers or {}).items():
            found.append({"plugin": plugin_key, "version": vdir.name, "server": sname,
                          "command": (sdef or {}).get("command"), "path": str(vdir)})
    return found


def codex_vectorworks_definitions(doc: dict, codex_home: Path) -> list[dict]:
    defs = []
    srv = (doc.get("mcp_servers") or {}).get(SERVER)
    if isinstance(srv, dict) and srv.get("enabled", True) is not False:
        defs.append({"source": "config.toml [mcp_servers.vectorworks]", "command": srv.get("command"),
                     "args": srv.get("args", [])})
    for key, val in (doc.get("plugins") or {}).items():
        if not isinstance(val, dict) or val.get("enabled", False) is not True:
            continue
        for s in plugin_cache_servers(codex_home, key):
            if "vectorworks" in s["server"].lower() or "vectorworks" in key.lower():
                if "vectorworks" in s["server"].lower():
                    defs.append({"source": "plugin %s %s (%s)" % (key, s["version"], s["path"]),
                                 "command": s["command"], "server": s["server"]})
    return defs


def cmd_codex(args) -> int:
    path = Path(args.config)
    launcher = args.launcher
    result = {"ok": True, "client": "codex", "config": str(path), "changes": [], "dry_run": args.dry_run}
    if tomllib is None:
        return out({**result, "ok": False, "error": "Python 3.11+ is required (tomllib)."})
    raw = path.read_bytes() if path.exists() else b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    nl = "\r\n" if "\r\n" in text else "\n"
    try:
        before = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return out({**result, "ok": False, "error": "config.toml is not valid TOML; refusing to edit: %s" % exc})
    expected = copy.deepcopy(before)
    lines = text.split(nl)
    trailing_nl = text.endswith(nl)
    if trailing_nl:
        lines = lines[:-1]

    blocks = scan_blocks(lines)
    remove: set[int] = set()
    insert_at = None
    for start, end, key, _arr in blocks:
        if key and key[:2] == ["mcp_servers", SERVER]:
            remove.update(range(start, end))
            if insert_at is None:
                insert_at = start
    # Inline forms: [mcp_servers] vectorworks = {...} / top-level mcp_servers.vectorworks.x = ...
    interior = string_interior_lines(lines)
    for start, end, key, _arr in blocks:
        for i in range(start if key is None else start + 1, end):
            if i in interior:
                continue
            kline = lines[i].split("=")[0].strip()
            if key == ["mcp_servers"] and split_key(kline)[:1] == [SERVER] and "=" in lines[i]:
                remove.add(i)
                insert_at = insert_at if insert_at is not None else None
            if key is None and split_key(kline)[:2] == ["mcp_servers", SERVER] and "=" in lines[i]:
                remove.add(i)

    if args.unregister:
        srv = (before.get("mcp_servers") or {}).get(SERVER)
        if srv is None:
            result["changes"].append("no [mcp_servers.vectorworks] to remove")
        elif args.launcher and not server_points_at(srv, launcher) and not args.force:
            result["changes"].append("kept [mcp_servers.vectorworks]: it does not point at %s" % launcher)
            remove = set()
        else:
            expected.get("mcp_servers", {}).pop(SERVER, None)
            if not expected.get("mcp_servers"):
                expected.pop("mcp_servers", None)
            result["changes"].append("removed [mcp_servers.vectorworks]")
        new_lines = [l for i, l in enumerate(lines) if i not in remove]
        # Remove our marketplace entry if it points at our directory.
        mk = (before.get("marketplaces") or {}).get(MARKETPLACE)
        if args.marketplace_dir and isinstance(mk, dict) and norm(str(mk.get("source", ""))) == norm(args.marketplace_dir):
            new_lines, _ = replace_table(new_lines, ["marketplaces", MARKETPLACE], None)
            expected["marketplaces"].pop(MARKETPLACE, None)
            if not expected["marketplaces"]:
                expected.pop("marketplaces")
            result["changes"].append("removed local marketplace %s" % MARKETPLACE)
            if PLUGIN_KEY in (before.get("plugins") or {}):
                new_lines, _ = replace_table(new_lines, ["plugins", PLUGIN_KEY], None)
                expected["plugins"].pop(PLUGIN_KEY, None)
                if not expected["plugins"]:
                    expected.pop("plugins")
                result["changes"].append("removed plugin entry %s" % PLUGIN_KEY)
    else:
        new_block = server_block(launcher, nl)
        expected.setdefault("mcp_servers", {})[SERVER] = expected_server(launcher)
        old = (before.get("mcp_servers") or {}).get(SERVER)
        if old != expected_server(launcher):
            result["changes"].append("set [mcp_servers.vectorworks] -> %s" % launcher if old is None else
                                     "replaced [mcp_servers.vectorworks] (was: %s %s)" % (old.get("command"), " ".join(map(str, old.get("args", [])))))
        new_lines = []
        inserted = False
        for i, l in enumerate(lines):
            if insert_at is not None and i == insert_at and not inserted:
                new_lines.extend(new_block)
                inserted = True
            if i in remove:
                continue
            new_lines.append(l)
        if not inserted:
            # Append after the last mcp_servers.* table, else at the end.
            last = None
            for start, end, key, _arr in scan_blocks(new_lines):
                if key and key[:1] == ["mcp_servers"]:
                    last = end
            if last is None:
                if new_lines and new_lines[-1].strip():
                    new_lines.append("")
                new_lines.extend(new_block)
            else:
                pos = last
                chunk = ([""] if new_lines[pos - 1].strip() else []) + new_block
                new_lines[pos:pos] = chunk
        # Top-level grace period: only raise it.
        grace = before.get(GRACE_KEY)
        if not isinstance(grace, int) or grace < GRACE_MS:
            new_lines = set_top_level(new_lines, GRACE_KEY, str(GRACE_MS))
            expected[GRACE_KEY] = GRACE_MS
            result["changes"].append("%s %s -> %d" % (GRACE_KEY, grace if grace is not None else "(unset)", GRACE_MS))
        # Marketplace -> local stable directory (skills-only plugin).
        if args.marketplace_dir:
            src = marketplace_source(args.marketplace_dir)
            mk = (before.get("marketplaces") or {}).get(MARKETPLACE)
            want = {"source_type": "local", "source": src}
            if mk != want:
                body = ['[marketplaces.%s]' % MARKETPLACE, 'source_type = "local"', "source = " + toml_literal(src)]
                new_lines, _ = replace_table(new_lines, ["marketplaces", MARKETPLACE], body)
                expected.setdefault("marketplaces", {})[MARKETPLACE] = want
                result["changes"].append("marketplace %s -> local %s (was %s)" % (
                    MARKETPLACE, src, (mk or {}).get("source", "unset")))
            plug = (before.get("plugins") or {}).get(PLUGIN_KEY)
            stale = any("vectorworks" in s["server"].lower() for s in plugin_cache_servers(path.parent, PLUGIN_KEY))
            want_enabled = not stale
            if not isinstance(plug, dict) or plug.get("enabled") is not want_enabled:
                if isinstance(plug, dict):
                    new_lines, _ = set_table_key(new_lines, ["plugins", PLUGIN_KEY], "enabled", "true" if want_enabled else "false")
                    expected["plugins"][PLUGIN_KEY]["enabled"] = want_enabled
                else:
                    new_lines, _ = replace_table(new_lines, ["plugins", PLUGIN_KEY],
                                                 ['[plugins.%s]' % toml_key(PLUGIN_KEY), "enabled = %s" % ("true" if want_enabled else "false")])
                    expected.setdefault("plugins", {})[PLUGIN_KEY] = {"enabled": want_enabled}
                result["changes"].append(
                    ("enabled plugin %s (skills only)" % PLUGIN_KEY) if want_enabled else
                    ("disabled plugin %s: its cached copy still defines a vectorworks server; "
                     "`codex plugin add %s` refreshes it to the skills-only 1.0 plugin" % (PLUGIN_KEY, PLUGIN_KEY)))
            result["plugin_cache_stale"] = stale
        # Other enabled plugins that still define a vectorworks server -> disable.
        codex_home = path.parent
        for key, val in (before.get("plugins") or {}).items():
            if key == PLUGIN_KEY and args.marketplace_dir:
                continue  # handled above
            if not isinstance(val, dict) or val.get("enabled") is not True:
                continue
            if any("vectorworks" in s["server"].lower() for s in plugin_cache_servers(codex_home, key)):
                new_lines, ok = set_table_key(new_lines, ["plugins", key], "enabled", "false")
                if ok:
                    expected["plugins"][key]["enabled"] = False
                    result["changes"].append("disabled plugin %s (it defines a duplicate vectorworks server)" % key)

    new_text = nl.join(new_lines) + (nl if trailing_nl or not text else "")
    try:
        after = tomllib.loads(new_text)
    except tomllib.TOMLDecodeError as exc:
        return out({**result, "ok": False, "error": "internal edit produced invalid TOML; nothing written: %s" % exc})
    if after != expected:
        diff = sorted(set(json.dumps(after, sort_keys=True, default=str)) ^ set(json.dumps(expected, sort_keys=True, default=str)))
        return out({**result, "ok": False, "error": "edit verification failed (parsed result differs from the expected document); nothing written",
                    "keys_after": sorted(after.keys()), "keys_expected": sorted(expected.keys()), "diff_chars": len(diff)})
    if new_text == text:
        result["changes"] = result["changes"] or ["already up to date"]
        result["written"] = False
        return out(result)
    result["written"] = False
    if not args.dry_run:
        if path.exists():
            backup = path.with_name(path.name + ".vectorworks-mcp-%s.bak" % stamp())
            shutil.copy2(path, backup)
            result["backup"] = str(backup)
        # Re-check that nobody rewrote the file while we worked.
        if path.exists() and path.read_bytes() != raw:
            return out({**result, "ok": False, "error": "config.toml changed while editing; rerun"})
        tmp = path.with_name(path.name + ".tmp-vwmcp")
        tmp.write_bytes((b"\xef\xbb\xbf" if bom else b"") + new_text.encode("utf-8"))
        os.replace(tmp, path)
        result["written"] = True
    result["definitions_after"] = codex_vectorworks_definitions(after, path.parent)
    return out(result)


def replace_table(lines: list[str], key: list[str], body: list[str] | None):
    """Replace (or append, or with body=None remove) the table `key`."""
    blocks = scan_blocks(lines)
    for start, end, k, _arr in blocks:
        if k == key:
            # keep trailing blank lines of the block
            stop = end
            while stop > start + 1 and not lines[stop - 1].strip():
                stop -= 1
            replacement = body if body is not None else []
            new = lines[:start] + replacement + lines[stop:]
            if body is None:
                # drop one leftover blank line
                if start < len(new) and start > 0 and not new[start].strip() and not new[start - 1].strip():
                    del new[start]
            return new, True
    if body is None:
        return lines, False
    # Append next to the last table with the same first key part.
    last = None
    for start, end, k, _arr in blocks:
        if k and k[:1] == key[:1]:
            last = end
    if last is None:
        new = lines + ([""] if lines and lines[-1].strip() else []) + body
    else:
        stop = last
        while stop > 0 and not lines[stop - 1].strip():
            stop -= 1
        new = lines[:stop] + [""] + body + lines[stop:]
    return new, True


def set_table_key(lines: list[str], key: list[str], name: str, value: str):
    interior = string_interior_lines(lines)
    for start, end, k, _arr in scan_blocks(lines):
        if k == key:
            for i in range(start + 1, end):
                if i in interior:
                    continue
                if split_key(lines[i].split("=")[0].strip()) == [name] and "=" in lines[i]:
                    lines = lines[:i] + ["%s = %s" % (name, value)] + lines[i + 1:]
                    return lines, True
            return lines[: start + 1] + ["%s = %s" % (name, value)] + lines[start + 1:], True
    return lines, False


def set_top_level(lines: list[str], name: str, value: str) -> list[str]:
    blocks = scan_blocks(lines)
    start, end, _k, _a = blocks[0]
    interior = string_interior_lines(lines)
    for i in range(start, end):
        if i in interior:
            continue
        if "=" in lines[i] and split_key(lines[i].split("=")[0].strip()) == [name]:
            return lines[:i] + ["%s = %s" % (name, value)] + lines[i + 1:]
    # insert after the last non-blank top-level line
    pos = end
    while pos > 0 and not lines[pos - 1].strip():
        pos -= 1
    return lines[:pos] + ["%s = %s" % (name, value)] + lines[pos:]


# --------------------------------------------------------------------------
# Claude Code
# --------------------------------------------------------------------------

def claude_entry(launcher: str) -> dict:
    return {"type": "stdio", "command": CMD_EXE, "args": ["/d", "/c", launcher], "env": {}}


def cmd_claude(args) -> int:
    path = Path(args.config)
    result = {"ok": True, "client": "claude-code", "config": str(path), "changes": [], "dry_run": args.dry_run}
    raw = path.read_bytes() if path.exists() else b"{}"
    try:
        data = json.loads(raw.decode("utf-8-sig") or "{}")
    except ValueError as exc:
        return out({**result, "ok": False, "error": "%s is not valid JSON; refusing to edit: %s" % (path, exc)})
    if not isinstance(data, dict):
        return out({**result, "ok": False, "error": "unexpected JSON root"})
    servers = data.get("mcpServers")
    if args.unregister:
        if isinstance(servers, dict) and SERVER in servers and (args.force or server_points_at(servers[SERVER], args.launcher)):
            del servers[SERVER]
            result["changes"].append("removed mcpServers.vectorworks")
        else:
            result["changes"].append("nothing to remove")
    else:
        want = claude_entry(args.launcher)
        if not isinstance(servers, dict):
            data["mcpServers"] = servers = {}
        if servers.get(SERVER) != want:
            result["changes"].append("set mcpServers.vectorworks -> %s" % args.launcher)
            servers[SERVER] = want
    if not result["changes"] or result["changes"] == ["nothing to remove"]:
        result["written"] = False
        return out(result)
    result["written"] = False
    if not args.dry_run:
        if path.exists():
            backup = path.with_name(path.name + ".vectorworks-mcp-%s.bak" % stamp())
            shutil.copy2(path, backup)
            result["backup"] = str(backup)
            if path.read_bytes() != raw:
                return out({**result, "ok": False, "error": "%s changed while editing; rerun" % path})
        tmp = path.with_name(path.name + ".tmp-vwmcp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
        json.loads(tmp.read_text(encoding="utf-8"))
        os.replace(tmp, path)
        result["written"] = True
    return out(result)


# --------------------------------------------------------------------------
# Inspect (doctor)
# --------------------------------------------------------------------------

def inspect_claude(config: Path, launcher: str) -> dict:
    info = {"config": str(config), "exists": config.exists(), "user": None, "project_overrides": [], "problems": []}
    if not config.exists():
        return info
    try:
        data = json.loads(config.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        info["problems"].append("cannot parse: %s" % exc)
        return info
    user = (data.get("mcpServers") or {}).get(SERVER)
    if user is not None:
        info["user"] = {"command": user.get("command"), "args": user.get("args", []),
                        "points_at_launcher": server_points_at(user, launcher)}
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    # ~/.claude.json pairs with ~/.claude/settings.json; CLAUDE_CONFIG_DIR keeps both in one folder.
    settings = home / ".claude" / "settings.json" if norm(str(config.parent)) == norm(str(home)) else config.parent / "settings.json"
    try:
        enabled = (json.loads(settings.read_text(encoding="utf-8-sig")).get("enabledPlugins") or {})
        info["skills_plugin_enabled"] = bool(enabled.get("vectorworks@vectorworks-mcp"))
        info["other_vectorworks_plugins"] = sorted(k for k, v in enabled.items() if k.startswith("vectorworks@") and k != "vectorworks@vectorworks-mcp" and v)
    except (OSError, ValueError):
        info["skills_plugin_enabled"] = None
    for proj, pdata in (data.get("projects") or {}).items():
        srv = ((pdata or {}).get("mcpServers") or {}).get(SERVER)
        if srv is not None:
            info["project_overrides"].append({"project": proj, "command": srv.get("command"),
                                              "points_at_launcher": server_points_at(srv, launcher)})
    return info


def find_stale_claude_plugins() -> list[dict]:
    found = []
    appdata = os.environ.get("APPDATA", "")
    sessions = Path(appdata) / "Claude" / "local-agent-mode-sessions"
    if sessions.is_dir():
        for manifest in sessions.glob("*/*/rpm/*/.claude-plugin/plugin.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if data.get("name") == "vectorworks":
                root = manifest.parent.parent
                has_mcp = (root / ".mcp.json").exists() or bool(data.get("mcpServers"))
                found.append({"kind": "claude-desktop-org-plugin", "version": data.get("version"), "path": str(root),
                              "defines_mcp_server": has_mcp})
    home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    cache = home / ".claude" / "plugins" / "cache"
    if cache.is_dir():
        for manifest in cache.glob("*/vectorworks/*/.claude-plugin/plugin.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            found.append({"kind": "claude-code-plugin-cache", "version": data.get("version"),
                          "path": str(manifest.parent.parent)})
    return found


def cmd_inspect(args) -> int:
    codex_home = Path(args.codex_home)
    launcher = args.launcher or ""
    report = {"ok": True, "codex": {}, "claude": {}, "stale": []}
    cfg = codex_home / "config.toml"
    cx = {"config": str(cfg), "exists": cfg.exists(), "definitions": [], "problems": []}
    if cfg.exists() and tomllib is not None:
        try:
            doc = tomllib.loads(cfg.read_text(encoding="utf-8-sig"))
        except tomllib.TOMLDecodeError as exc:
            doc = {}
            cx["problems"].append("config.toml does not parse: %s" % exc)
        cx["definitions"] = codex_vectorworks_definitions(doc, codex_home)
        srv = (doc.get("mcp_servers") or {}).get(SERVER)
        if isinstance(srv, dict):
            cx["server"] = {k: srv.get(k) for k in ("command", "args", "startup_timeout_sec", "tool_timeout_sec", "enabled", "cwd")}
            cx["server"]["points_at_launcher"] = server_points_at(srv, launcher)
        cx[GRACE_KEY] = doc.get(GRACE_KEY)
        mk = (doc.get("marketplaces") or {}).get(MARKETPLACE)
        cx["marketplace"] = mk
        cx["plugins"] = {k: v for k, v in (doc.get("plugins") or {}).items() if "vectorworks" in k}
    report["codex"] = cx
    report["claude"] = inspect_claude(Path(args.claude_config), launcher)
    report["stale"] = find_stale_claude_plugins()
    clone = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "repos" / "vectorworks-mcp"
    if clone.is_dir():
        report["stale"].append({"kind": "old-git-clone", "path": str(clone)})
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    for p in list((local / "Packages").glob("*/LocalCache/Local/VectorworksMCP")) + [local / "VectorworksMCP"]:
        if p.is_dir():
            report["stale"].append({"kind": "msix-localcache-runtime" if "Packages" in str(p) else "old-localappdata-runtime",
                                    "path": str(p), "versions": sorted(d.name for d in p.iterdir() if d.is_dir())})
    # Earlier registrations under another name (e.g. 0.x "vectorworks-mcp", "vectorworks_legacy").
    codex_servers: dict = {}
    if cfg.exists() and tomllib is not None:
        try:
            codex_servers = tomllib.loads(cfg.read_text(encoding="utf-8-sig")).get("mcp_servers") or {}
        except (OSError, tomllib.TOMLDecodeError):
            pass
    claude_servers: dict = {}
    try:
        claude_servers = json.loads(Path(args.claude_config).read_text(encoding="utf-8-sig")).get("mcpServers") or {}
    except (OSError, ValueError):
        pass
    for kind, servers in (("codex-old-server", codex_servers), ("claude-old-server", claude_servers)):
        for name, srv in servers.items():
            if name == SERVER or not isinstance(srv, dict):
                continue
            blob = " ".join([name, str(srv.get("command") or ""), " ".join(str(a) for a in srv.get("args") or [])])
            low = blob.lower()
            if not re.search(r"vectorworks|vw[-_]?mcp", low):
                continue
            # "ours": clearly an earlier Vectorworks MCP (safe for doctor -Fix to remove); else report only.
            ours = bool(re.search(r"vectorworks[-_ ]?mcp|vw[-_]?mcp", low)) or ("vectorworks" in low and "server.py" in low)
            report["stale"].append({"kind": kind, "name": name, "command": srv.get("command"),
                                    "args": srv.get("args", []), "ours": ours})
    return out(report)


# --------------------------------------------------------------------------
# Codex logs (read-only)
# --------------------------------------------------------------------------

LOG_PATTERNS = {
    "initialized": "Service initialized as client",
    "omitted_pending": "omitting pending optional MCP server",
    "cached_catalog_without_wait": "using cached MCP catalog without waiting for startup server",
    "startup_timeout": "timed out",
    "start_failed": "failed to start",
    "powershell_launcher": "MCP server stderr (powershell.exe)",
    "repair_mode": "repair mode",
}


def cmd_codex_logs(args) -> int:
    import sqlite3

    home = Path(args.codex_home)
    report = {"ok": True, "databases": [], "since_days": args.days, "counts": {}, "last_seen": {}, "errors": 0}
    since = int(time.time()) - args.days * 86400
    for db in sorted(home.glob("logs_*.sqlite")):
        report["databases"].append(str(db))
        try:
            con = sqlite3.connect("file:%s?mode=ro" % db.as_posix(), uri=True, timeout=2)
            rows = con.execute(
                "select ts, level, target, feedback_log_body from logs where ts > ? and "
                "feedback_log_body like '%vectorworks%' and target not like 'codex_core::stream_events%' "
                "and target not like 'codex_core::session%' order by ts desc limit 20000",
                (since,),
            ).fetchall()
            con.close()
        except Exception as exc:  # locked / schema change: report, never fail doctor
            report.setdefault("read_errors", []).append("%s: %s" % (db.name, exc))
            continue
        for ts, level, target, body in rows:
            body = body or ""
            if "server_name=vectorworks" not in body and "vectorworks" not in target and "(powershell.exe)" not in body \
                    and "MCP server stderr" not in body:
                continue
            for name, needle in LOG_PATTERNS.items():
                if needle.lower() in body.lower():
                    report["counts"][name] = report["counts"].get(name, 0) + 1
                    report["last_seen"].setdefault(name, _dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds"))
            if level in ("ERROR", "WARN") and "server_name=vectorworks" in body:
                report["errors"] += 1
    return out(report)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("codex")
    c.add_argument("--config", required=True)
    c.add_argument("--launcher", default="")
    c.add_argument("--marketplace-dir", default="")
    c.add_argument("--unregister", action="store_true")
    c.add_argument("--force", action="store_true")
    c.add_argument("--dry-run", action="store_true")
    j = sub.add_parser("claude")
    j.add_argument("--config", required=True)
    j.add_argument("--launcher", default="")
    j.add_argument("--unregister", action="store_true")
    j.add_argument("--force", action="store_true")
    j.add_argument("--dry-run", action="store_true")
    i = sub.add_parser("inspect")
    i.add_argument("--codex-home", required=True)
    i.add_argument("--claude-config", required=True)
    i.add_argument("--launcher", default="")
    lg = sub.add_parser("codex-logs")
    lg.add_argument("--codex-home", required=True)
    lg.add_argument("--days", type=int, default=7)
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if args.cmd == "codex":
        if not args.unregister and not args.launcher:
            return out({"ok": False, "error": "--launcher is required"})
        return cmd_codex(args)
    if args.cmd == "claude":
        if not args.unregister and not args.launcher:
            return out({"ok": False, "error": "--launcher is required"})
        return cmd_claude(args)
    if args.cmd == "inspect":
        return cmd_inspect(args)
    return cmd_codex_logs(args)


if __name__ == "__main__":
    raise SystemExit(main())
