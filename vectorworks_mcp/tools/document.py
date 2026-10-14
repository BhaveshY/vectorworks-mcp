"""vw_document: list/info/activate/new/open/save/save_as/close/export/import."""

from __future__ import annotations

import ntpath
import os
import time
from pathlib import Path

from .. import coerce as C
from ..documents import normalise_doc
from ..errors import VWError, validation
from ..host import doc_brief, get_host

ACTIONS = ["list", "info", "activate", "new", "open", "save", "save_as", "close", "export", "import", "dismiss_home"]
ACTION_SYN = {"documents": "list", "ls": "list", "get": "info", "document_info": "info", "switch": "activate",
              "focus": "activate", "create": "new", "load": "open", "saveas": "save_as", "save_copy": "save_as",
              "write": "export", "print": "export", "publish": "export", "pdf": "export", "insert": "import",
              "close_home": "dismiss_home", "home": "dismiss_home", "dismiss": "dismiss_home",
              "dismiss_dialog": "dismiss_home", "close_home_screen": "dismiss_home"}
EXPORT_FORMATS = {"pdf": "export_pdf", "dwg": "export_dwg", "dxf": "export_dwg", "ifc": "export_ifc",
                  "png": "export_image", "jpg": "export_image", "tif": "export_image", "bmp": "export_image",
                  "vwx": "export_vectorworks_document"}
IMPORT_FORMATS = {"dwg": "import_dwg", "dxf": "import_dwg", "ifc": "import_ifc", "png": "image", "jpg": "image",
                  "tif": "image", "bmp": "image"}
FORMAT_SYN = {"jpeg": "jpg", "tiff": "tif", "image": "png", "autocad": "dwg"}


def _discard(a: dict) -> bool:
    """Explicit consent to drop unsaved changes: discard_changes='discard' (or true), or discard=true."""
    value = a.get("discard_changes", a.get("discard"))
    return value is True or str(value or "").strip().lower() in ("discard", "true", "yes", "1")


def _fmt(a: dict, path: str, allowed: dict) -> str:
    fmt = a.get("format")
    if fmt in (None, ""):
        ext = Path(path).suffix.lower().lstrip(".")
        fmt = FORMAT_SYN.get(ext, ext)
    return C.choice(fmt, "format", list(allowed), FORMAT_SYN)


def _abs_path(value, field: str = "path") -> str:
    path = C.text(value, field, required=True).strip().strip('"')
    if not ntpath.isabs(path):
        raise VWError("path_error", f"{field} must be an absolute Windows path (got {path!r})", field=field,
                      example="C:\\Users\\me\\Desktop\\plan.pdf")
    return path


def _output_path(path: str, replace: bool) -> None:
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        raise VWError("path_error", f"The folder {parent!r} does not exist.", field="path")
    if os.path.exists(path) and not replace:
        raise VWError("confirmation_required", f"{path!r} already exists.", field="replace", example=True,
                      next_step="Choose a new file name, or ask the user and repeat with replace=true.")


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    action = C.choice(a.get("action"), "action", ACTIONS, ACTION_SYN)
    host = get_host()
    session = host.session
    out: dict = {"ok": True, "action": action}
    replace = C.boolean(a.get("replace", False), "replace")

    if action == "list":
        target = session.target(None, need_document=False)
        out["documents"] = [doc_brief(d, True) | ({"active": True} if d.get("active") else {})
                            for d in session.list_documents(target.inst)]
        return out
    if action == "dismiss_home":
        return _dismiss_home(host, a, out)
    if action == "info":
        target = session.target(a.get("document"))
        info = host.call(target, "get_document_info", {})
        doc = normalise_doc(info)
        if doc:
            session.observe(target.inst, doc)
        out["document"] = doc_brief(doc, True)
        out["result"] = {k: v for k, v in (info or {}).items() if k not in {"timing", "binding"}} if isinstance(info, dict) else info
        return out
    if action == "activate":
        hint = a.get("document") or a.get("name") or a.get("path")
        if not hint:
            raise validation("document (name, path or fingerprint) is required", "document", "Plan.vwx")
        target = session.target(None, need_document=False)
        doc = session.resolve(target.inst, str(hint))
        if target.inst.supports("activate_document"):
            result = host.call(target, "activate_document", {"fingerprint": doc["fingerprint"]}, kind="write")
            doc = normalise_doc((result or {}).get("active_document") if isinstance(result, dict) else None) or doc
        elif not doc.get("active"):
            raise VWError("upgrade_required", "Switching documents needs the 1.0 bridge.")
        session.observe(target.inst, doc)
        out["document"] = doc_brief(doc, True)
        return out
    if action == "new":
        target = session.target(None, need_document=False)
        params = {}
        if a.get("template"):
            params["template_path"] = _abs_path(a["template"], "template")
        result = host.call(target, "new_document", params, kind="write")
        doc = normalise_doc((result or {}).get("active_document") if isinstance(result, dict) else result) \
            or session.active_document(target.inst, fresh=True)
        session.observe(target.inst, doc)
        out["document"] = doc_brief(doc, True)
        return out
    if action == "open":
        path = _abs_path(a.get("path") or a.get("file_path"))
        if not os.path.isfile(path):
            raise VWError("path_error", f"{path!r} does not exist.", field="path")
        target = session.target(None, need_document=False)
        params = {"file_path": path, "path": path}
        if _discard(a):
            params["replace_dirty_confirmation"] = "REPLACE_DIRTY_DOCUMENT"
            params["discard_changes"] = "discard"
        result = host.call(target, "open_document", params, kind="long", ctx=ctx)
        doc = _confirm_open(host, target, path, result, ctx)
        session.observe(target.inst, doc)
        out["document"] = doc_brief(doc, True)
        return out
    if action in {"save", "save_as"}:
        target = session.target(a.get("document"), write=True)
        params: dict = {}
        if action == "save_as" or a.get("path"):
            path = _abs_path(a.get("path") or a.get("file_path"))
            _output_path(path, replace)
            params = {"file_path": path, "path": path}
            if replace:
                params["replace_confirmation"] = "REPLACE_EXISTING_FILE"
        result = host.call(target, "save_document", params, kind="write", ctx=ctx)
        out["result"] = _clean(result)
        out["document"] = doc_brief(session.active_document(target.inst, fresh=True) or target.document)
        if out["document"]:
            session.observe(target.inst, out["document"])
        return out
    if action == "close":
        target = session.target(a.get("document"), write=True)
        doc = target.document or {}
        params = {"fingerprint": doc.get("fingerprint")}
        if _discard(a):
            params["discard_changes"] = "discard"
        elif doc.get("dirty"):
            raise VWError("confirmation_required", f"'{doc.get('name')}' has unsaved changes.", field="discard_changes",
                          example="discard", next_step="Save first (vw_document action='save'), or ask the user and "
                          "repeat with discard_changes='discard'.")
        out["result"] = _clean(host.call(target, "close_document", params, kind="write"))
        session.pinned = None
        return out
    if action == "export":
        path = _abs_path(a.get("path") or a.get("file_path"))
        fmt = _fmt(a, path, EXPORT_FORMATS)
        _output_path(path, replace)
        target = session.target(a.get("document"))
        native = EXPORT_FORMATS[fmt]
        params = {"file_path": path, "path": path, "format": fmt}
        if replace:
            params.update({"replace": True, "replace_confirmation": "REPLACE_EXISTING_FILE"})
        if fmt == "pdf":
            rng = C.choice(a.get("range"), "range", ["current_view", "all_pages", "sheet"],
                           {"all": "all_pages", "sheets": "all_pages", "view": "current_view"}, default="current_view")
            params["range"] = rng
            params["current_view_only"] = rng == "current_view"
            if rng == "sheet":
                params["sheet"] = C.text(a.get("sheet"), "sheet", required=True)
            if a.get("dpi") not in (None, ""):
                params["dpi"] = params["resolution_dpi"] = C.integer(a["dpi"], "dpi")
            if not target.inst.v2 and rng != "current_view":
                warnings.append("Pre-1.0 bridge: PDF range all_pages/sheet may be ignored.")
        try:
            result = host.call(target, native, params, kind="long", ctx=ctx)
        except VWError as exc:
            if not (fmt == "ifc" and "no IFC file" in (exc.message or "")):
                raise
            _explain_ifc_failure(host, target, exc)
            if exc.code == "precondition_failed":
                raise
            # A document whose IFC export never ran exports nothing without UI: run Vectorworks' own export
            # dialog once (driven by control id); later exports of this document work without it.
            from .. import ifc_dialog
            first = ifc_dialog.export_with_dialog(host, target, path, ctx)
            if not first["ok"]:
                if first.get("needs_stories"):
                    exc.code, exc.retryable = "precondition_failed", True
                    exc.message = ("IFC export needs the design layers on stories; Vectorworks' export dialog "
                                   "showed no mapped layers.")
                    exc.next_step = ("Ask the user, then: vw_organize(kind='story', action='create', name='Ground "
                                     "Floor', elevation=0, layers=['<design layer>']) and export again.")
                exc.details = {"dialog_export": first}
                raise
            result = {"operation": "export_ifc", "path": path, "method": "export_dialog_first_run",
                      "note": "first IFC export of this document ran through Vectorworks' export dialog "
                              "(settings now stored; next exports run without it)"}
        out["result"] = compact_io(result, C.boolean(a.get("detail", False), "detail"))
        if fmt == "ifc" and isinstance(out["result"], dict):
            out["result"].pop("setup", None)
            if isinstance(out["result"].get("attempts"), list):
                out["result"]["attempts"] = len(out["result"]["attempts"])
        out["path"] = path
        if os.path.exists(path):
            out["bytes"] = os.path.getsize(path)
        else:
            warnings.append("Vectorworks reported success but the file is not visible yet.")
        return out
    # import
    path = _abs_path(a.get("path") or a.get("file_path"))
    if not os.path.isfile(path):
        raise VWError("path_error", f"{path!r} does not exist.", field="path")
    fmt = _fmt(a, path, IMPORT_FORMATS)
    target = session.target(a.get("document"), write=True)
    native = IMPORT_FORMATS[fmt]
    if native == "image":
        at = C.point(a["at"], "at", 1.0) if a.get("at") not in (None, "") else [0.0, 0.0]
        out["result"] = host.script(target, "import_image", {"path": path, "at": at}, label="MCP import image", ctx=ctx)
        out["via"] = "script"
        return out
    detail = C.boolean(a.get("detail", False), "detail")
    params = {"file_path": path, "path": path, "format": fmt}
    if detail:
        params["detail"] = True  # bridge 1.0.x returns compact import receipts unless detail
    out["result"] = compact_io(host.call(target, native, params, kind="long", ctx=ctx), detail)
    return out


def _explain_ifc_failure(host, target, exc: VWError) -> None:
    """Vectorworks exports IFC only when design layers sit on stories (live 2026-10-14: a document without
    stories always failed; after story + layer assignment and one export it worked)."""
    try:
        stories = (host.script(target, "stories", {"action": "list"}, label="MCP stories", kind="read") or {}).get("stories")
    except VWError:
        return
    if not stories:
        exc.code = "precondition_failed"
        exc.message = ("IFC export needs the design layers on a story (IFC building storeys); this document has no "
                       "stories, so Vectorworks exported nothing.")
        exc.next_step = ("Ask the user, then: vw_organize(kind='story', action='create', name='Ground Floor', "
                         "elevation=0, layers=['<design layer>']) and export again.")
        exc.retryable = True
        exc.details = None


def _dismiss_home(host, a: dict, out: dict) -> dict:
    """Close the Vectorworks Home screen / 'new version available' dialog (never any other dialog)."""
    inst = host.bridge.select(a.get("instance"))
    wait_ms = C.integer(a["wait_ms"], "wait_ms") if a.get("wait_ms") not in (None, "") else 3000
    wait_ms = max(0, min(wait_ms, 15000))
    result = host.bridge.call(inst, "dismiss_home", {"wait_ms": wait_ms}, kind="read", budget_s=wait_ms / 1000 + 5)
    result = _clean(result) if isinstance(result, dict) else {"result": result}
    out["result"] = result
    try:
        host.bridge.ping(inst)  # refresh ui_state / modal_dialog for the next call
    except VWError:
        pass
    dialog = result.get("modal_dialog")
    if isinstance(dialog, dict) and dialog.get("kind") not in {"home", "update_available", "backup_prompt"}:
        out["next_step"] = (f"Another dialog is open: ask the user to close '{dialog.get('title') or 'the dialog'}' in "
                            "Vectorworks, then continue.")
    elif result.get("still_shown"):
        out["next_step"] = "The Home screen is still shown; call vw_status in a few seconds."
    else:
        out["next_step"] = "Continue with the task (no need to call vw_status first)."
    return out


def _confirm_open(host, target, path: str, result, ctx) -> dict:
    """Pre-1.0 bridges accept the open and finish it later: poll the readback."""
    doc = normalise_doc((result or {}).get("active_document") if isinstance(result, dict) else None)
    if doc and doc.get("path"):
        return doc
    wanted = os.path.normcase(os.path.abspath(path))
    deadline = time.monotonic() + min(45.0, host.bridge.budgets.long_total)
    last = None
    while time.monotonic() < deadline:
        if ctx is not None and ctx.cancelled.is_set():
            break
        try:
            info = host.bridge.call(target.inst, "get_document_info", {}, budget_s=5.0)
            last = normalise_doc(info)
            if last and last.get("path") and os.path.normcase(os.path.abspath(last["path"])) == wanted:
                return last
        except VWError as exc:
            if exc.code not in {"vectorworks_busy", "no_active_document"}:
                raise
        if ctx is not None:
            ctx.report_progress(time.monotonic(), None, "waiting for Vectorworks to open the file")
        time.sleep(0.5)
    raise VWError("unknown_commit_state", "Vectorworks accepted the open but the document was not confirmed yet.",
                  commit_state="unknown", details={"last_active": last},
                  next_step="Call vw_status in a few seconds; if the file is open it will be listed.")


_IO_DROP = {"timing", "binding", "before", "after", "document_before", "document_after", "expected_document",
            "transaction_artifact"}


def compact_io(result, detail: bool = False):
    """Import/export receipts without the internal before/after document dumps (detail=true keeps everything)."""
    if not isinstance(result, dict) or detail:
        return _clean(result)
    out = {k: v for k, v in result.items() if k not in _IO_DROP}
    mutation = out.get("document_mutation")
    if isinstance(mutation, dict):
        created = mutation.get("created_object_uuids") or []
        deleted = mutation.get("deleted_object_uuids") or []
        summary = {"verified": mutation.get("verified"), "created_objects": len(created),
                   "deleted_objects": len(deleted)}
        if created:
            summary["created_uuids_first"] = created[:10]
        for key in ("created_layers", "deleted_layers", "changed_layers", "active_layer_changed"):
            value = mutation.get(key)
            if value not in (None, [], {}):
                summary[key] = value[:20] if isinstance(value, list) else value
        out["document_mutation"] = summary
    for key, value in list(out.items()):
        if isinstance(value, str) and len(value) > 1500:
            out[key] = value[:1500] + " ...(truncated; detail=true for all)"
        elif isinstance(value, list) and len(value) > 50:
            out[key] = value[:20] + [f"... {len(value) - 20} more (detail=true)"]
    return out


def _clean(value):
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "timing"}
    return value
