"""Process-wide host state shared by the tools (bridge client, document
session, caches) and small helpers used by several tools."""

from __future__ import annotations

import logging
import os
import sys
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
_TIMING = os.environ.get("VW_MCP_TIMING") == "1"  # per bridge call timings on stderr (profiling)

_host: Optional["Host"] = None
_host_lock = threading.Lock()
_notices = threading.local()  # per tool call: notes merged into the result's warnings by tool_entry


_PRUNE_KEYS = {"warnings", "annotations_created", "gray_layers", "hidden_classes", "via", "undo"}


def _prune_empty(value: Any, depth: int = 0) -> None:
    """Token hygiene for every result: drop empty lists/dicts and internal markers (via, undo text) - also nested
    under "result" (organize/document receipts)."""
    if not isinstance(value, dict) or depth > 3:
        return
    for key in list(value):
        item = value[key]
        if key in _PRUNE_KEYS and (item in ([], {}, None, "") or key in {"via", "undo"}):
            value.pop(key)
        elif isinstance(item, dict):
            _prune_empty(item, depth + 1)


def notice(message: str) -> None:
    items = getattr(_notices, "items", None)
    if items is not None and message not in items:
        items.append(message)


class Host:
    def __init__(self) -> None:
        self.bridge = BridgeClient()
        self.session = Session(self.bridge)
        self.started = time.time()
        self.log_path = None
        self.schema_cache: dict[tuple, dict] = {}
        self.writes = 0  # count of non-read bridge calls by this server (cache generation)

    # ----- native helpers ---------------------------------------------
    def call(self, target: Target, action: str, params: Optional[dict] = None, *, kind: str = "read",
             ctx: Optional[CallContext] = None, idempotency_key: Optional[str] = None, budget_s: Optional[float] = None) -> Any:
        merged = dict(params or {})
        merged.update(target.params)
        if action in {"capture_view", "export_image"}:
            # Bridge builds before 7f22b11 default to undo-and-remove for the capture scale boost, which crashed
            # Vectorworks right after doors/windows were created (live 2026-10-14): always ask for the explicit restore.
            merged.setdefault("scale_boost_undo", "off")
        progress = ctx.report_progress if ctx is not None else None
        cancelled = ctx.cancelled if ctx is not None else None
        started = time.perf_counter()
        if kind != "read":
            self.writes += 1  # invalidates read caches (room labels)
        result = self.bridge.call(target.inst, action, merged, kind=kind, idempotency_key=idempotency_key,
                                  budget_s=budget_s, progress=progress, cancelled=cancelled)
        if _TIMING:
            timing = (result.get("timing") or {}) if isinstance(result, dict) else {}
            native, queue = timing.get("handler_ms"), timing.get("queue_wait_ms")
            label = merged.get("undo_label") or ""
            sys.stderr.write(f"[timing] {action} {label} {(time.perf_counter() - started) * 1000:.0f} ms"
                             f"{f' (native {native:.0f} ms' if isinstance(native, (int, float)) else ' ('}"
                             f"{f', queue {queue:.0f} ms' if isinstance(queue, (int, float)) else ''}, "
                             f"request {len(str(merged))} B)\n")
            sys.stderr.flush()
        if isinstance(result, dict) and (result.get("home_dismissed") or result.get("dismissed_dialogs")):
            dialogs = result.get("dismissed_dialogs") if isinstance(result.get("dismissed_dialogs"), list) else []
            home = [d for d in dialogs if isinstance(d, dict) and d.get("kind") in (None, "home", "update_available",
                                                                                      "backup_prompt")]
            other = [d for d in dialogs if isinstance(d, dict) and d not in home]
            if result.get("home_dismissed") or home:
                titles = ", ".join(f"'{d.get('title')}'" for d in home if d.get("title"))
                notice(f"The bridge closed the Vectorworks start-up dialog{' (' + titles + ')' if titles else ''} "
                       f"before {action}; nothing else was affected.")
            for d in other:
                rows = "; ".join(str(r) for r in (d.get("rows") or [])[:5] if str(r).strip(" ,|"))
                text = d.get("message") or rows
                notice(f"Vectorworks showed '{d.get('title')}' during {action}" + (f": {text[:300]}" if text else "")
                       + f" - closed with '{d.get('pressed') or 'OK'}'.")
        return result

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
            try:
                cached = self.bridge.call(inst, "describe_parametric_schema", {"plugin_name": plugin}, kind="read")
            except VWError as exc:
                # e.g. Column2: installed, but without an SDK schema provider -> parametric-record fallback
                # (scripts_lib/_pio.py) when run_script is available.
                if "schema provider" not in exc.message or not self.can_script(inst):
                    raise
                cached = {"script_only": True, "plugin_name": plugin}
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
        details = {"stderr": str(reply.get("stderr") or "")[-1500:]} if reply.get("stderr") else None
        # Vetted snippets raise ValueError("<prefix>: ...") -> that error code; the document was rolled back.
        for prefix, mapped in (("stale_reference", "stale_reference"), ("not_found", "not_found"),
                               ("validation", "native_rejected"), ("native_rejected", "native_rejected"),
                               ("readback_mismatch", "native_rejected"), ("already_exists", "native_rejected")):
            line = next((ln for ln in reversed(message.splitlines()) if (prefix + ":") in ln), None)
            if line is not None:
                code = mapped
                if "Traceback" in message:
                    details = dict(details or {}, traceback=message[-1500:])
                message = line[line.index(prefix + ":"):]
                break
        next_step = None
        if code == "not_found" and "did you mean" in message:
            next_step = ("Nothing was changed. Use one of the names suggested in error.message (or list them as the "
                         "message says) and send the call again.")
        raise VWError(code, message[:1500], commit_state="rolled_back" if reply.get("rolled_back", True) else "unknown",
                      details=details, next_step=next_step)
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
        _notices.items = []
        try:
            result = fn(args, ctx, warnings)
        except VWError as exc:
            result = exc.envelope()
        except Exception as exc:  # noqa: BLE001
            log.error("tool failure: %s", traceback.format_exc())
            result = error_envelope("internal_error", f"{type(exc).__name__}: {exc}")
        finally:
            warnings.extend(_notices.items)
            _notices.items = None
        output = result if isinstance(result, ToolOutput) else ToolOutput(result)
        structured = output.structured
        if warnings:
            structured.setdefault("warnings", [])
            structured["warnings"] = list(dict.fromkeys(structured["warnings"] + warnings))
        _prune_empty(structured)
        if config.trace_enabled():
            structured["_trace"] = {"ms": round((time.perf_counter() - started) * 1000, 1)}
        return output

    return run
