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

ACTIONS = ["list", "info", "activate", "new", "open", "save", "save_as", "close", "export", "import"]
ACTION_SYN = {"documents": "list", "ls": "list", "get": "info", "document_info": "info", "switch": "activate",
              "focus": "activate", "create": "new", "load": "open", "saveas": "save_as", "save_copy": "save_as",
              "write": "export", "print": "export", "publish": "export", "pdf": "export", "insert": "import"}
EXPORT_FORMATS = {"pdf": "export_pdf", "dwg": "export_dwg", "dxf": "export_dwg", "ifc": "export_ifc",
                  "png": "export_image", "jpg": "export_image", "tif": "export_image", "bmp": "export_image",
                  "vwx": "export_vectorworks_document"}
IMPORT_FORMATS = {"dwg": "import_dwg", "dxf": "import_dwg", "ifc": "import_ifc", "png": "image", "jpg": "image",
                  "tif": "image", "bmp": "image"}
FORMAT_SYN = {"jpeg": "jpg", "tiff": "tif", "image": "png", "autocad": "dwg"}


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
        if str(a.get("discard_changes") or "").lower() == "discard":
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
        if str(a.get("discard_changes") or "").lower() == "discard":
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
        result = host.call(target, native, params, kind="long", ctx=ctx)
        out["result"] = _clean(result)
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
    out["result"] = _clean(host.call(target, native, {"file_path": path, "path": path, "format": fmt}, kind="long", ctx=ctx))
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


def _clean(value):
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "timing"}
    return value
