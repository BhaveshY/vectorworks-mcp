"""Vectorworks MCP repair-mode server (stdlib only).

Started by ``bin\\vectorworks-mcp.cmd`` when the installed runtime (venv or
code) is missing or broken, so MCP clients still get a working server instead
of "connector unavailable". It exposes exactly one tool, ``vw_status``, which
reports ``setup_required`` with diagnostics and the exact repair command.

Rules (docs/V1_CONTRACT.md section 3):
- No third-party imports; must run on any Python >= 3.8.
- Newline-delimited JSON-RPC 2.0 over stdio. Nothing but frames on stdout.
- Dual era: legacy ``initialize`` handshake and the 2026-07-28
  ``server/discover`` per-request envelope.

Run directly: ``python repair_server.py [--reason TEXT] [--root DIR]``.
"""

from __future__ import annotations

import json
import os
import platform
import sys
from pathlib import Path

SERVER_NAME = "vectorworks-mcp-repair"
SERVER_VERSION = "1.0.0"
HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_VERSIONS = ("2026-07-28",)
LATEST_HANDSHAKE = HANDSHAKE_VERSIONS[-1]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
UNSUPPORTED_PROTOCOL_VERSION = -32022

INSTRUCTIONS = (
    "Vectorworks MCP is running in REPAIR MODE: the runtime is not installed or is broken. "
    "Only vw_status is available. Call vw_status, show the user error.next_step "
    "(the exact repair command), and stop. Do not try other Vectorworks tools until "
    "the user has run it and restarted the MCP client."
)

TOOL = {
    "name": "vw_status",
    "title": "Vectorworks status (repair mode)",
    "description": (
        "Repair mode: reports why the Vectorworks MCP runtime cannot start and the exact "
        "command that fixes it. Example: vw_status({})."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "Ignored in repair mode."},
            "job_id": {"type": "string", "description": "Ignored in repair mode."},
            "idempotency_key": {"type": "string", "description": "Ignored in repair mode."},
        },
        "additionalProperties": True,
    },
    "outputSchema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "setup_required": {"type": "boolean"},
            "error": {"type": "object"},
            "repair_command": {"type": "string"},
            "diagnostics": {"type": "object"},
        },
        "required": ["ok", "error"],
    },
    "annotations": {
        "title": "Vectorworks status (repair mode)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
}


def _state_root(cli_root: str | None) -> Path:
    if cli_root:
        return Path(cli_root)
    override = os.environ.get("VW_MCP_STATE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("USERPROFILE") or str(Path.home())) / ".vectorworks-mcp"


def _read_text(path: Path, limit: int = 4096) -> str | None:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return handle.read(limit).strip()
    except OSError:
        return None


def _read_json(path: Path) -> dict | None:
    text = _read_text(path, 1024 * 1024)
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _quote(path: str) -> str:
    return '"' + path + '"' if (" " in path and not path.startswith('"')) else path


def _repair_command(receipt: dict | None) -> str:
    source = (receipt or {}).get("source_dir") or ""
    if source and Path(source, "install.ps1").is_file():
        return (
            "powershell -NoProfile -ExecutionPolicy Bypass -File "
            + _quote(str(Path(source, "install.ps1")))
        )
    return (
        "Clone https://github.com/BhaveshY/vectorworks-mcp, then run in that folder: "
        "powershell -NoProfile -ExecutionPolicy Bypass -File .\\install.ps1 -WaitForVectorworksExit "
        "(AI agents: follow AGENT_INSTALL.md in that folder)"
    )


def build_status(reason: str, cli_root: str | None) -> dict:
    root = _state_root(cli_root)
    version = _read_text(root / "current.txt", 128)
    receipt = _read_json(root / "install-receipt.json")
    runtime = root / "runtime" / version if version else None
    venv_python = runtime / "venv" / "Scripts" / "python.exe" if runtime else None
    checks = {
        "state_dir": str(root),
        "state_dir_exists": root.is_dir(),
        "current_version": version,
        "runtime_dir": str(runtime) if runtime else None,
        "runtime_dir_exists": bool(runtime and runtime.is_dir()),
        "venv_python": str(venv_python) if venv_python else None,
        "venv_python_exists": bool(venv_python and venv_python.is_file()),
        "package_present": bool(runtime and (runtime / "app" / "vectorworks_mcp" / "__init__.py").is_file()),
        "receipt_present": receipt is not None,
        "receipt_version": (receipt or {}).get("version"),
        "base_python_recorded": (receipt or {}).get("python", {}).get("base_executable")
        if isinstance((receipt or {}).get("python"), dict)
        else None,
        "auth_token_present": (root / "auth-token").is_file(),
        "repair_python": sys.executable,
        "repair_python_version": platform.python_version(),
        "launcher_reason": reason or "unspecified",
    }
    if not checks["state_dir_exists"] or not version:
        message = "Vectorworks MCP is not installed for this Windows user."
    elif not checks["venv_python_exists"]:
        message = "The Vectorworks MCP runtime %s is incomplete: its Python environment is missing." % version
    elif not checks["package_present"]:
        message = "The Vectorworks MCP runtime %s is incomplete: the server code is missing." % version
    else:
        message = "The Vectorworks MCP runtime %s failed to start (%s)." % (version, reason or "unknown reason")
    command = _repair_command(receipt)
    next_step = (
        "Ask the user to run this in PowerShell, then restart the MCP client (Codex / Claude Code): "
        + command
    )
    return {
        "ok": False,
        "setup_required": True,
        "mode": "repair",
        "error": {
            "code": "setup_required",
            "message": message,
            "retryable": False,
            "next_step": next_step,
        },
        "repair_command": command,
        "doctor_command": "powershell -NoProfile -ExecutionPolicy Bypass -File "
        + _quote(str(root / "bin" / "doctor.ps1"))
        if (root / "bin" / "doctor.ps1").is_file()
        else None,
        "diagnostics": checks,
    }


class RepairServer:
    def __init__(self, reason: str = "", root: str | None = None) -> None:
        self.reason = reason
        self.root = root
        self.era: str | None = None  # "legacy" | "modern"
        self.protocol_version = LATEST_HANDSHAKE

    # -- helpers -------------------------------------------------------
    def _server_info(self) -> dict:
        return {"name": SERVER_NAME, "title": "Vectorworks MCP (repair mode)", "version": SERVER_VERSION}

    def _stamp(self, result: dict, list_result: bool = False) -> dict:
        if self.era == "modern":
            result.setdefault("resultType", "complete")
            meta = result.setdefault("_meta", {})
            meta["io.modelcontextprotocol/serverInfo"] = self._server_info()
            if list_result:
                result.setdefault("cacheScope", "private")
                result.setdefault("ttlMs", 0)
        return result

    @staticmethod
    def _error(msg_id, code: int, message: str, data=None) -> dict:
        err = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        return {"jsonrpc": "2.0", "id": msg_id, "error": err}

    def _capabilities(self) -> dict:
        return {"tools": {"listChanged": False}}

    # -- dispatch ------------------------------------------------------
    def handle(self, message):
        """Return a response dict, or None for notifications."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return self._error(None, INVALID_REQUEST, "Invalid JSON-RPC 2.0 message")
        method = message.get("method")
        msg_id = message.get("id")
        is_request = "id" in message and msg_id is not None
        if not isinstance(method, str):
            # A response from the client (we never send requests) - ignore.
            return None
        if not is_request:
            return None  # notifications/initialized, notifications/cancelled, ...
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return self._error(msg_id, INVALID_PARAMS, "params must be an object")

        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        requested_modern = meta.get("io.modelcontextprotocol/protocolVersion") if meta else None

        if method == "initialize":
            self.era = "legacy"
            wanted = params.get("protocolVersion")
            self.protocol_version = wanted if wanted in HANDSHAKE_VERSIONS else LATEST_HANDSHAKE
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": self._capabilities(),
                    "serverInfo": self._server_info(),
                    "instructions": INSTRUCTIONS,
                },
            }
        if method == "server/discover":
            if requested_modern and requested_modern not in MODERN_VERSIONS:
                return self._error(
                    msg_id,
                    UNSUPPORTED_PROTOCOL_VERSION,
                    "Unsupported protocol version",
                    {"supported": list(MODERN_VERSIONS + HANDSHAKE_VERSIONS), "requested": requested_modern},
                )
            self.era = "modern"
            self.protocol_version = MODERN_VERSIONS[-1]
            result = {
                "supportedVersions": list(MODERN_VERSIONS),
                "capabilities": self._capabilities(),
                "instructions": INSTRUCTIONS,
            }
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._stamp(result, list_result=True)}
        if self.era is None and requested_modern:
            self.era = "modern"
        if method == "ping":
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._stamp({})}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._stamp({"tools": [TOOL]}, list_result=True)}
        if method == "tools/call":
            name = params.get("name")
            if name != "vw_status":
                status = build_status(self.reason, self.root)
                status["error"]["message"] = (
                    "Tool %r is unavailable in repair mode. " % name
                ) + status["error"]["message"]
            else:
                status = build_status(self.reason, self.root)
            text = json.dumps(status, ensure_ascii=False, indent=1)
            result = {
                "content": [{"type": "text", "text": text}],
                "structuredContent": status,
                "isError": True,
            }
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._stamp(result)}
        if method in ("resources/list", "resources/templates/list", "prompts/list"):
            key = {
                "resources/list": "resources",
                "resources/templates/list": "resourceTemplates",
                "prompts/list": "prompts",
            }[method]
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._stamp({key: []}, list_result=True)}
        if method == "logging/setLevel":
            return {"jsonrpc": "2.0", "id": msg_id, "result": self._stamp({})}
        return self._error(msg_id, METHOD_NOT_FOUND, "Method not found: %s" % method)


def _write(stdout, payload) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    stdout.flush()


def serve(stdin=None, stdout=None, reason: str = "", root: str | None = None) -> int:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    server = RepairServer(reason=reason, root=root)
    sys.stderr.write("vectorworks-mcp: repair mode (%s)\n" % (reason or "runtime missing"))
    sys.stderr.flush()
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            _write(stdout, server._error(None, PARSE_ERROR, "Parse error"))
            continue
        if isinstance(message, list):  # batch (legacy 2024-11-05 clients)
            responses = [r for r in (server.handle(m) for m in message) if r is not None]
            if responses:
                _write(stdout, responses)
            continue
        response = server.handle(message)
        if response is not None:
            _write(stdout, response)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    reason = os.environ.get("VW_MCP_REPAIR_REASON", "")
    root = None
    while argv:
        arg = argv.pop(0)
        if arg == "--reason" and argv:
            reason = argv.pop(0)
        elif arg == "--root" and argv:
            root = argv.pop(0)
        elif arg == "--print-status":
            print(json.dumps(build_status(reason, root), indent=2))
            return 0
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass
    try:
        return serve(reason=reason, root=root)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
