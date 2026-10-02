"""vw_status: health, instances, documents, ui_state, setup problems, jobs,
transactions. Never fails: problems are reported, not raised."""

from __future__ import annotations

import os
import sys

from .. import __version__, config
from .. import coerce as C
from ..documents import normalise_doc
from ..errors import INSTALL_COMMAND, NEXT_STEPS, VWError
from ..host import doc_brief, get_host

SUMMARY_SCAN_LIMIT = 100_000


def _problem(code: str, message: str, next_step: str = "") -> dict:
    return {"code": code, "message": message, "next_step": next_step or NEXT_STEPS.get(code, (False, ""))[1]}


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    host = get_host()
    bridge = host.bridge
    out: dict = {"ok": True, "ready": False}
    problems: list[dict] = list(config.config_problems)
    if a.get("disconnect") and C.boolean(a.get("disconnect"), "disconnect"):
        bridge.forget()
        out["disconnected"] = True
    out["host"] = {"version": __version__, "pid": os.getpid(), "python": sys.version.split()[0],
                   "profile": config.tool_profile()}
    if not config.auth_token_candidates() and not config.insecure_no_auth():
        problems.append(_problem("setup_required", f"Auth token file {config.auth_token_file()} is missing.",
                                 "Ask the user to run the installer, then restart Vectorworks: " + INSTALL_COMMAND))
    if a.get("instance"):
        try:
            bridge.pin(str(a["instance"]))
        except VWError as exc:
            problems.append(_problem(exc.code, exc.message))

    instances = bridge.discover(force=True)
    reachable = []
    rows = []
    for inst in instances:
        try:
            status = bridge.ping(inst)
            row = inst.summary()
            ad = normalise_doc(status.get("active_document"))
            if ad:
                row["active_document"] = doc_brief(ad)
            if isinstance(status.get("open_document_count"), int):
                row["open_documents"] = status["open_document_count"]
            if status.get("handler_running_ms"):
                row["busy_ms"] = status["handler_running_ms"]
            idle = status.get("main_context_idle_ms")
            if not inst.v2 and isinstance(idle, (int, float)) and idle >= bridge.budgets.busy_idle_ms > 0:
                row["ui_state"] = "busy"
            rows.append(row)
            reachable.append(inst)
        except VWError as exc:
            if inst.source != "probe" or exc.code != "bridge_unreachable":
                rows.append({"instance": inst.key, "port": inst.port, "error": exc.code, "message": exc.message})
            if exc.code == "bridge_unreachable":
                problems.append(_problem("bridge_unreachable",
                                         "Vectorworks is not running, or the MCP bridge plug-in is not loaded "
                                         f"(nothing answered on port {inst.port})."))
            else:
                problems.append(_problem(exc.code, exc.message, exc.next_step or ""))
    out["instances"] = rows
    chosen = None
    if len(reachable) == 1:
        chosen = reachable[0]
    elif len(reachable) > 1:
        pinned = bridge._pinned_key or bridge._last_used_key
        chosen = next((i for i in reachable if i.key == pinned), None)
        if chosen is None:
            problems.append(_problem("multiple_instances", f"{len(reachable)} Vectorworks instances are running; none is pinned."))
    if chosen is not None:
        out["instance"] = chosen.key
        if not chosen.v2:
            problems.append(_problem("upgrade_recommended",
                                     f"Vectorworks runs the pre-1.0 bridge (capability revision {chosen.revision}). Reads, "
                                     "creates and edits work; multi-document, capture fitting, scripting, layers/stories "
                                     "management and async jobs need the 1.0 bridge.",
                                     "Ask the user to run: " + INSTALL_COMMAND))
        ui_state = chosen.status.get("ui_state")
        chosen_row = next((r for r in rows if r.get("instance") == chosen.key), {})
        busy = ui_state in {"modal", "tracking", "loading"} or chosen_row.get("ui_state") == "busy"
        if busy:
            problems.append(_problem("vectorworks_busy", f"Vectorworks is busy (ui_state={ui_state or 'busy'})."))
        else:
            try:
                docs = host.session.list_documents(chosen)
                out["documents"] = [doc_brief(d, True) | ({"active": True} if d.get("active") else {}) for d in docs]
                active = next((d for d in docs if d.get("active")), None) or host.session.active_document(chosen)
                if active:
                    out["active_document"] = doc_brief(active, True)
                    host.session.observe(chosen, active)
                elif not docs:
                    problems.append(_problem("no_active_document", "No document is open in Vectorworks."))
            except VWError as exc:
                problems.append(_problem(exc.code, exc.message))
        if a.get("detail") and C.boolean(a.get("detail"), "detail") and "documents" in out:
            try:
                summary = bridge.call(chosen, "drawing_summary", {"limit": 5, "scan_limit": SUMMARY_SCAN_LIMIT,
                                                                   "include_examples": False, "example_limit": 0})
                if isinstance(summary, dict):
                    summary.pop("timing", None)
                out["summary"] = summary
            except VWError as exc:
                problems.append(_problem(exc.code, exc.message))
        out["ready"] = not any(p["code"] in {"vectorworks_busy", "no_active_document", "bridge_unreachable", "auth_failed",
                                             "multiple_instances", "setup_required"} for p in problems)

    if a.get("job_id"):
        out["job"] = _job(bridge, chosen, str(a["job_id"]))
    if a.get("idempotency_key"):
        out["transaction"] = _transaction(bridge, chosen, str(a["idempotency_key"]))
    pin = host.session.pinned
    if pin:
        out["pinned_document"] = {"name": pin.get("name"), "fingerprint": pin.get("fingerprint")}
    if problems:
        out["problems"] = problems
        out["next_step"] = problems[0]["next_step"]
    return out


def _job(bridge, inst, job_id: str) -> dict:
    if inst is None:
        return {"job_id": job_id, "state": "unknown", "error": "no reachable Vectorworks instance"}
    try:
        state = bridge.job_status(inst, job_id)
    except VWError as exc:
        return {"job_id": job_id, "state": "unknown", "error": exc.envelope()["error"]}
    state = {k: v for k, v in state.items() if k != "timing"}
    if state.get("state") in {"queued", "running"}:
        state["next_step"] = "Still running; call vw_status(job_id) again in a few seconds."
    return state


def _transaction(bridge, inst, key: str) -> dict:
    if inst is None:
        return {"idempotency_key": key, "state": "unknown", "error": "no reachable Vectorworks instance"}
    try:
        data = bridge.call(inst, "transaction_status", {"idempotency_key": key})
    except VWError as exc:
        return {"idempotency_key": key, "state": "unknown", "error": exc.envelope()["error"]}
    if not isinstance(data, dict):
        return {"idempotency_key": key, "state": "unknown"}
    out = {"idempotency_key": key, "state": data.get("state", "unknown")}
    if out["state"] == "committed":
        recorded = data.get("recorded_result")
        if isinstance(recorded, dict):
            recorded = {k: v for k, v in recorded.items() if k != "timing"}
        out["recorded_result"] = recorded
        out["next_step"] = "It was applied; do not resend."
    else:
        out["next_step"] = ("No receipt is retained for this key; this is not proof nothing happened. Inspect with vw_read "
                            "before deciding to resend.")
    return out
