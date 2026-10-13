"""Minimal, dependency-free MCP stdio server core.

Why not FastMCP: importing fastmcp + mcp + pydantic costs ~1.8 s on a warm
Windows machine, and Codex drops servers that are not ready ~1 s after thread
start (``mcp_optional_startup_grace_ms``). This core answers ``initialize`` /
``server/discover`` / ``tools/list`` from static data using only the standard
library.

Supports both protocol eras on one stdio connection:
- handshake era (2024-11-05 .. 2025-11-25): initialize, notifications/initialized, ping;
- 2026-07-28: server/discover plus per-request ``_meta`` protocol version,
  ``resultType`` on results, ``ttlMs``/``cacheScope`` on cacheable results,
  -32022 for unsupported versions.

Rules: newline-delimited JSON on stdin/stdout, stdout carries only MCP frames
(``sys.stdout`` is redirected to stderr), the process exits only on stdin EOF,
and no handler exception can escape.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("vectorworks_mcp.core")

HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_VERSIONS = ("2026-07-28",)
LATEST_HANDSHAKE = HANDSHAKE_VERSIONS[-1]
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022
RESOURCE_NOT_FOUND = -32002

_CACHEABLE = {"server/discover", "tools/list", "resources/list", "resources/templates/list",
              "resources/read", "prompts/list"}


@dataclass
class ToolSpec:
    name: str
    title: str
    description: str
    input_schema: dict
    output_schema: Optional[dict]
    annotations: dict
    handler: Callable[[dict, "CallContext"], Any]

    def wire(self, version: str) -> dict:
        tool = {"name": self.name, "description": self.description, "inputSchema": self.input_schema,
                "annotations": dict(self.annotations, title=self.title)}
        if _at_least(version, "2025-06-18"):
            tool["title"] = self.title
            if self.output_schema is not None:
                tool["outputSchema"] = self.output_schema
        return tool


@dataclass
class ResourceSpec:
    uri: str
    name: str
    description: str
    mime_type: str
    reader: Callable[[str], Any]  # returns str (text) or bytes (blob)
    template: bool = False


@dataclass
class PromptSpec:
    name: str
    description: str
    arguments: list[dict]
    render: Callable[[dict], list[dict]]


class ToolOutput:
    """Structured result plus optional extra content blocks (images, links)."""

    def __init__(self, structured: dict, extra: Optional[list[dict]] = None) -> None:
        self.structured = structured
        self.extra = extra or []


@dataclass
class CallContext:
    request_id: Any
    progress_token: Any
    protocol_version: str
    server: "MCPServer"
    cancelled: threading.Event = field(default_factory=threading.Event)
    _last_progress: float = 0.0

    def report_progress(self, progress: float, total: Optional[float] = None, message: str = "") -> None:
        if self.progress_token is None or self.cancelled.is_set():
            return
        params: dict[str, Any] = {"progressToken": self.progress_token, "progress": progress}
        if total is not None:
            params["total"] = total
        if message and _at_least(self.protocol_version, "2025-03-26"):
            params["message"] = message
        self.server.send({"jsonrpc": "2.0", "method": "notifications/progress", "params": params})


def _at_least(version: str, minimum: str) -> bool:
    order = HANDSHAKE_VERSIONS + MODERN_VERSIONS
    if version not in order:
        return True
    return order.index(version) >= order.index(minimum)


class _RPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class MCPServer:
    def __init__(self, name: str, version: str, title: str = "", instructions: str = "",
                 max_workers: int = 8) -> None:
        self.name = name
        self.version = version
        self.title = title or name
        self.instructions = instructions
        self.tools: dict[str, ToolSpec] = {}
        self.resources: dict[str, ResourceSpec] = {}
        self.prompts: dict[str, PromptSpec] = {}
        self.session_version = LATEST_HANDSHAKE
        self.client_info: dict = {}
        self._out = None
        self._write_lock = threading.Lock()
        self._inflight: dict[Any, CallContext] = {}
        self._inflight_lock = threading.Lock()
        self._jobs: "queue.Queue[Callable[[], None]]" = queue.Queue()
        self._workers = 0
        self._idle_workers = 0
        self._max_workers = max_workers
        self._worker_lock = threading.Lock()
        self.on_shutdown: list[Callable[[], None]] = []
        self.first_list_at: Optional[float] = None

    # ----- registration ------------------------------------------------
    def add_tool(self, spec: ToolSpec) -> None:
        self.tools[spec.name] = spec

    def add_resource(self, spec: ResourceSpec) -> None:
        self.resources[spec.uri] = spec

    def add_prompt(self, spec: PromptSpec) -> None:
        self.prompts[spec.name] = spec

    # ----- output ------------------------------------------------------
    def send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode("utf-8")
        with self._write_lock:
            try:
                self._out.write(data)
                self._out.flush()
            except (OSError, ValueError):
                pass  # client gone; the read loop sees EOF and exits

    def _reply(self, request_id: Any, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _error(self, request_id: Any, code: int, message: str, data: Any = None) -> None:
        err: dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            err["data"] = data
        self.send({"jsonrpc": "2.0", "id": request_id, "error": err})

    # ----- workers -----------------------------------------------------
    def _submit(self, fn: Callable[[], None]) -> None:
        with self._worker_lock:
            spawn = self._idle_workers == 0 and self._workers < self._max_workers
            if spawn:
                self._workers += 1
            self._pending = getattr(self, "_pending", 0) + 1
        self._jobs.put(fn)
        if spawn:
            threading.Thread(target=self._worker_loop, name=f"mcp-worker-{self._workers}", daemon=True).start()

    def _worker_loop(self) -> None:
        while True:
            with self._worker_lock:
                self._idle_workers += 1
            fn = self._jobs.get()
            with self._worker_lock:
                self._idle_workers -= 1
            try:
                fn()
            except BaseException:  # noqa: BLE001 - a worker must never die
                log.error("worker crashed: %s", traceback.format_exc())
            finally:
                with self._worker_lock:
                    self._pending = max(0, getattr(self, "_pending", 1) - 1)

    def _drain(self, timeout_s: float) -> None:
        """After stdin EOF, let in-flight requests finish and send their replies
        (one-shot clients close stdin right after their last request)."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            with self._worker_lock:
                if not getattr(self, "_pending", 0):
                    return
            time.sleep(0.02)

    # ----- main loop ---------------------------------------------------
    def run_stdio(self) -> int:
        stdin = sys.stdin.buffer
        self._out = sys.stdout.buffer
        sys.stdout = sys.stderr  # stray prints can never corrupt the MCP stream
        for sig in ("SIGINT", "SIGBREAK"):
            if hasattr(signal, sig):
                try:
                    signal.signal(getattr(signal, sig), signal.SIG_IGN)
                except (ValueError, OSError):
                    pass
        while True:
            try:
                line = stdin.readline()
            except KeyboardInterrupt:
                continue
            except (OSError, ValueError):
                break
            if not line:
                break  # EOF: the only way out
            line = line.strip()
            if not line:
                continue
            try:
                self._handle_line(line)
            except BaseException:  # noqa: BLE001
                log.error("dispatch failure: %s", traceback.format_exc())
        self._drain(float(os.environ.get("VW_MCP_DRAIN_S", "190") or 190))
        for hook in self.on_shutdown:
            try:
                hook()
            except Exception:  # noqa: BLE001
                pass
        return 0

    def _handle_line(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode("utf-8-sig", errors="replace"))
        except ValueError:
            self._error(None, PARSE_ERROR, "Parse error")
            return
        if isinstance(message, list):  # JSON-RPC batch (2025-03-26)
            for item in message:
                self._handle_message(item)
            return
        self._handle_message(message)

    def _handle_message(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            self._error(None, INVALID_REQUEST, "Invalid Request")
            return
        method = msg.get("method")
        has_id = "id" in msg and msg.get("id") is not None
        if method is None:
            return  # a response to something we never sent; ignore
        if not isinstance(method, str):
            if has_id:
                self._error(msg.get("id"), INVALID_REQUEST, "method must be a string")
            return
        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            if has_id:
                self._error(msg["id"], INVALID_PARAMS, "params must be an object")
            return
        if not has_id:
            self._notification(method, params)
            return
        request_id = msg["id"]
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        modern_version = meta.get(PROTOCOL_VERSION_META_KEY) if meta else None
        if modern_version is not None:
            if not isinstance(modern_version, str):
                self._error(request_id, INVALID_PARAMS, "the protocol-version envelope value must be a string")
                return
            if modern_version not in MODERN_VERSIONS:
                self._error(request_id, UNSUPPORTED_PROTOCOL_VERSION, "Unsupported protocol version",
                            {"supported": list(MODERN_VERSIONS), "requested": modern_version})
                return
            version = modern_version
        else:
            version = self.session_version
        try:
            if method == "tools/call" or method == "resources/read":
                ctx = CallContext(request_id=request_id, progress_token=meta.get("progressToken") if meta else None,
                                  protocol_version=version, server=self)
                with self._inflight_lock:
                    self._inflight[request_id] = ctx
                self._submit(lambda: self._run_async(method, params, ctx, modern_version is not None))
                return
            result = self._dispatch_sync(method, params, version)
        except _RPCError as exc:
            self._error(request_id, exc.code, exc.message, exc.data)
            return
        except Exception as exc:  # noqa: BLE001
            log.error("handler %s failed: %s", method, traceback.format_exc())
            self._error(request_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}")
            return
        self._reply(request_id, self._stamp(method, result, modern_version is not None))

    def _stamp(self, method: str, result: dict, modern: bool) -> dict:
        if not modern:
            return result
        result = dict(result)
        result.setdefault("resultType", "complete")
        if method in _CACHEABLE:
            result.setdefault("ttlMs", 0)
            result.setdefault("cacheScope", "private")
        meta = dict(result.get("_meta") or {})
        meta[SERVER_INFO_META_KEY] = {"name": self.name, "version": self.version}
        result["_meta"] = meta
        return result

    def _notification(self, method: str, params: dict) -> None:
        if method == "notifications/cancelled":
            rid = params.get("requestId")
            with self._inflight_lock:
                ctx = self._inflight.get(rid)
            if ctx is not None:
                ctx.cancelled.set()
        # notifications/initialized, progress, roots/list_changed: nothing to do

    def _capabilities(self) -> dict:
        return {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
            "prompts": {"listChanged": False},
            "logging": {},
        }

    def _dispatch_sync(self, method: str, params: dict, version: str) -> dict:
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.session_version = requested if requested in HANDSHAKE_VERSIONS else LATEST_HANDSHAKE
            self.client_info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
            result = {
                "protocolVersion": self.session_version,
                "capabilities": self._capabilities(),
                "serverInfo": {"name": self.name, "title": self.title, "version": self.version},
            }
            if self.instructions:
                result["instructions"] = self.instructions
            return result
        if method == "server/discover":
            result = {"supportedVersions": list(MODERN_VERSIONS) + list(reversed(HANDSHAKE_VERSIONS)),
                      "capabilities": self._capabilities(),
                      "serverInfo": {"name": self.name, "title": self.title, "version": self.version}}
            if self.instructions:
                result["instructions"] = self.instructions
            return result
        if method == "ping":
            return {}
        if method == "tools/list":
            if self.first_list_at is None:
                self.first_list_at = time.perf_counter()
            return {"tools": [t.wire(version) for t in self.tools.values()]}
        if method == "resources/list":
            return {"resources": [
                {"uri": r.uri, "name": r.name, "description": r.description, "mimeType": r.mime_type}
                for r in self.resources.values() if not r.template]}
        if method == "resources/templates/list":
            return {"resourceTemplates": [
                {"uriTemplate": r.uri, "name": r.name, "description": r.description, "mimeType": r.mime_type}
                for r in self.resources.values() if r.template]}
        if method == "prompts/list":
            return {"prompts": [{"name": p.name, "description": p.description, "arguments": p.arguments}
                                for p in self.prompts.values()]}
        if method == "prompts/get":
            name = params.get("name")
            prompt = self.prompts.get(name)
            if prompt is None:
                raise _RPCError(INVALID_PARAMS, f"Unknown prompt: {name}")
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            return {"description": prompt.description, "messages": prompt.render(args)}
        if method == "logging/setLevel":
            level = str(params.get("level", "info")).upper()
            logging.getLogger("vectorworks_mcp").setLevel(getattr(logging, level, logging.INFO)
                                                          if level in {"DEBUG", "INFO", "WARNING", "ERROR"} else logging.INFO)
            return {}
        if method == "completion/complete":
            return {"completion": {"values": [], "hasMore": False}}
        raise _RPCError(METHOD_NOT_FOUND, f"Method not found: {method}")

    # ----- tools/call and resources/read (worker threads) ---------------
    def _run_async(self, method: str, params: dict, ctx: CallContext, modern: bool) -> None:
        try:
            if method == "tools/call":
                result = self._call_tool(params, ctx)
            else:
                result = self._read_resource(params)
            if not ctx.cancelled.is_set():
                self._reply(ctx.request_id, self._stamp(method, result, modern))
        except _RPCError as exc:
            if not ctx.cancelled.is_set():
                self._error(ctx.request_id, exc.code, exc.message, exc.data)
        except BaseException as exc:  # noqa: BLE001
            log.error("%s failed: %s", method, traceback.format_exc())
            if not ctx.cancelled.is_set():
                self._error(ctx.request_id, INTERNAL_ERROR, f"Internal error: {type(exc).__name__}")
        finally:
            with self._inflight_lock:
                self._inflight.pop(ctx.request_id, None)

    def _call_tool(self, params: dict, ctx: CallContext) -> dict:
        name = params.get("name")
        tool = self.tools.get(name) if isinstance(name, str) else None
        if tool is None:
            raise _RPCError(INVALID_PARAMS, f"Unknown tool: {name}. Available: {', '.join(self.tools)}")
        args = params.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        if not isinstance(args, dict):
            args = {}
        try:
            output = tool.handler(args, ctx)
        except Exception as exc:  # noqa: BLE001 - handlers return envelopes; this is a bug guard
            log.error("tool %s crashed: %s", name, traceback.format_exc())
            output = ToolOutput({"ok": False, "error": {
                "code": "internal_error", "message": f"{type(exc).__name__}: {exc}"[:500], "retryable": False,
                "next_step": "The MCP host hit an unexpected error (logged under ~/.vectorworks-mcp/logs). Try a simpler request."}})
        if not isinstance(output, ToolOutput):
            output = ToolOutput(output if isinstance(output, dict) else {"ok": True, "result": output})
        structured = output.structured
        text = json.dumps(structured, ensure_ascii=False, separators=(",", ":"), default=str)
        content: list[dict] = []
        modern_content = _at_least(ctx.protocol_version, "2025-06-18")
        for block in output.extra:
            if block.get("type") == "resource_link" and not modern_content:
                continue
            content.append(block)
        content.append({"type": "text", "text": text})
        result: dict[str, Any] = {"content": content, "isError": structured.get("ok") is False}
        if modern_content:
            result["structuredContent"] = structured
        return result

    def _read_resource(self, params: dict) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise _RPCError(INVALID_PARAMS, "uri is required")
        spec = self.resources.get(uri)
        if spec is None:
            for candidate in self.resources.values():
                if candidate.template and uri.startswith(candidate.uri.split("{", 1)[0]):
                    spec = candidate
                    break
        if spec is None:
            raise _RPCError(RESOURCE_NOT_FOUND, f"Resource not found: {uri}", {"uri": uri})
        data = spec.reader(uri)
        if data is None:
            raise _RPCError(RESOURCE_NOT_FOUND, f"Resource not found: {uri}", {"uri": uri})
        if isinstance(data, tuple):  # (bytes, mime)
            import base64
            blob, mime = data
            return {"contents": [{"uri": uri, "mimeType": mime, "blob": base64.b64encode(blob).decode("ascii")}]}
        return {"contents": [{"uri": uri, "mimeType": spec.mime_type, "text": data}]}


def environment_summary() -> dict:
    return {"python": sys.version.split()[0], "executable": sys.executable, "pid": os.getpid()}
