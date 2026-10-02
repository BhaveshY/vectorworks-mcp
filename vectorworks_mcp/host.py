"""Process-wide host state shared by the tools (bridge client, document
session, caches) and small helpers used by several tools."""

from __future__ import annotations

import logging
import threading
import time
import traceback
from typing import Any, Callable, Optional

from . import config
from .bridge import BridgeClient, Instance
from .documents import Session, Target
from .errors import VWError, error_envelope
from .mcp_core import CallContext, ToolOutput

log = logging.getLogger("vectorworks_mcp.host")

_host: Optional["Host"] = None
_host_lock = threading.Lock()


class Host:
    def __init__(self) -> None:
        self.bridge = BridgeClient()
        self.session = Session(self.bridge)
        self.started = time.time()
        self.log_path = None
        self.schema_cache: dict[tuple, dict] = {}

    # ----- native helpers ---------------------------------------------
    def call(self, target: Target, action: str, params: Optional[dict] = None, *, kind: str = "read",
             ctx: Optional[CallContext] = None, idempotency_key: Optional[str] = None, budget_s: Optional[float] = None) -> Any:
        merged = dict(params or {})
        merged.update(target.params)
        progress = ctx.report_progress if ctx is not None else None
        cancelled = ctx.cancelled if ctx is not None else None
        return self.bridge.call(target.inst, action, merged, kind=kind, idempotency_key=idempotency_key,
                                budget_s=budget_s, progress=progress, cancelled=cancelled)

    def can_script(self, inst: Instance) -> bool:
        return inst.supports("run_script")

    def script(self, target: Target, snippet: str, args: Any, *, label: str, ctx: Optional[CallContext] = None,
               transaction: bool = True, kind: str = "write") -> Any:
        from . import scripts_lib
        if not self.can_script(target.inst):
            raise self.bridge.missing_action(target.inst, "run_script")
        code = scripts_lib.build(snippet, args)
        reply = self.call(target, "run_script", {"language": "python", "code": code, "undo_label": label,
                                                  "transaction": transaction}, kind=kind, ctx=ctx)
        return script_result(reply)

    def schema(self, inst: Instance, plugin: str) -> dict:
        session_id = inst.status.get("session_id") or inst.status.get("bridge_session_id") or ""
        key = (inst.key, session_id, plugin)
        cached = self.schema_cache.get(key)
        if cached is None:
            cached = self.bridge.call(inst, "describe_parametric_schema", {"plugin_name": plugin}, kind="read")
            if not isinstance(cached, dict):
                cached = {}
            self.schema_cache[key] = cached
            if len(self.schema_cache) > 64:
                self.schema_cache.pop(next(iter(self.schema_cache)))
        return cached


def script_result(reply: Any) -> Any:
    if not isinstance(reply, dict):
        return reply
    if reply.get("ok") is False or reply.get("error"):
        message = str(reply.get("error") or "script failed")
        code = "script_error"
        for prefix in ("stale_reference", "not_found"):
            if message.startswith(prefix + ":") or (prefix + ":") in message:
                code = prefix
        raise VWError(code, message[:1500], commit_state="rolled_back" if reply.get("rolled_back", True) else "unknown",
                      details={"stderr": str(reply.get("stderr") or "")[-1500:]} if reply.get("stderr") else None)
    return reply.get("result")


def get_host() -> Host:
    global _host
    with _host_lock:
        if _host is None:
            _host = Host()
        return _host


def doc_brief(doc: Optional[dict], path: bool = False) -> Optional[dict]:
    if not doc:
        return None
    out = {"name": doc.get("name"), "fingerprint": doc.get("fingerprint")}
    if path and doc.get("path"):
        out["path"] = doc["path"]
    if isinstance(doc.get("dirty"), bool):
        out["dirty"] = doc["dirty"]
    return out


def tool_entry(fn: Callable[[dict, CallContext, list], Any]) -> Callable[[dict, CallContext], ToolOutput]:
    """Wrap a tool body: VWError -> envelope, warnings merged, never raises."""

    def run(args: dict, ctx: CallContext) -> ToolOutput:
        warnings: list[str] = []
        started = time.perf_counter()
        try:
            result = fn(args, ctx, warnings)
        except VWError as exc:
            result = exc.envelope()
        except Exception as exc:  # noqa: BLE001
            log.error("tool failure: %s", traceback.format_exc())
            result = error_envelope("internal_error", f"{type(exc).__name__}: {exc}")
        output = result if isinstance(result, ToolOutput) else ToolOutput(result)
        structured = output.structured
        if warnings:
            structured.setdefault("warnings", [])
            structured["warnings"] = list(dict.fromkeys(structured["warnings"] + warnings))
        if config.trace_enabled():
            structured["_trace"] = {"ms": round((time.perf_counter() - started) * 1000, 1)}
        return output

    return run
