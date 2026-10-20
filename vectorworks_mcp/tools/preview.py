"""Small capture of what a write just changed, returned in the same tool result (MCP image content).

vw_create / vw_edit / vw_organize(kind=viewport) attach it when preview=true, and automatically when a call made
>= AUTO_COUNT objects or any 3D object / viewport (preview=false turns it off). The capture never fails the write:
problems become a warning."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .. import capture as cap
from ..errors import VWError
from ..mcp_core import ToolOutput

PREVIEW_EDGE = 768
AUTO_COUNT = 10
THREE_D_KINDS = {"extrude", "column", "roof", "slab"}
MAX_FIT_UUIDS = 400


def wanted(flag, count: int, three_d: bool) -> bool:
    if flag is not None:
        return bool(flag)
    return count >= AUTO_COUNT or three_d


def attach(host, document: Optional[str], out: dict, warnings: list, *, uuids: Optional[list] = None,
           sheet: Optional[str] = None, three_d: bool = False) -> ToolOutput:
    """Capture the objects (or the sheet) and return ToolOutput(out + preview info, [image, link])."""
    started = time.perf_counter()
    blocks: list[dict] = []
    try:
        target = host.session.target(document)
        inst = target.inst
        if not inst.supports("capture_view") or not inst.v2:
            raise VWError("unsupported", "this bridge cannot capture a fitted view")
        path = cap.new_capture_path("png")
        params: dict = {"path": str(path), "file_path": str(path), "format": "png", "restore": True,
                        "update_viewports": False, "reset_plugin_objects": False, "margin_pct": 8, "fill_pct": 90}
        if sheet:
            params.update({"layer": sheet, "fit": "layer", "capture_target": "sheet"})
        else:
            ids = [str(u) for u in (uuids or []) if u][:MAX_FIT_UUIDS]
            if not ids:
                raise VWError("validation_error", "nothing to preview")
            params.update({"fit": "objects", "object_uuids": ids, "capture_target": "objects"})
            # plan view for 2D work, an isometric hidden-line view when 3D objects were made
            params.update({"standard_view": 9, "projection": 0, "render_mode": 6} if three_d else
                          {"standard_view": 7, "projection": 6})
        result = host.call(target, "capture_view", params, kind="read")
        data = result if isinstance(result, dict) else {}
        real = Path(str(data.get("path") or data.get("file_path") or path))
        if not real.exists():
            raise VWError("native_error", "the preview capture file was not written")
        image, mime, info = cap.model_image(real, PREVIEW_EDGE)
        cap.prune()
        if image is not None:
            import base64
            blocks.append({"type": "image", "data": base64.b64encode(image).decode("ascii"), "mimeType": mime})
        blocks.append({"type": "resource_link", "uri": f"vw://capture/{real.name}", "name": real.name,
                       "mimeType": "image/png", "description": "Full-resolution preview"})
        # the image itself is in the content; next_step already says to look at it
        out["preview"] = {"view": "sheet" if sheet else ("iso" if three_d else "top_plan")}
        if info.get("model_width"):
            out["preview"]["size"] = [info["model_width"], info["model_height"]]
        if data.get("restored") is False:
            out["preview"]["restore_mismatch"] = data.get("restore_mismatch") or []
            warnings.append("The preview capture could not restore the user's view exactly (preview.restore_mismatch).")
    except VWError as exc:
        warnings.append(f"No preview image ({exc.message}); call vw_capture to check the result.")
    except Exception as exc:  # noqa: BLE001 - a preview must never fail the write
        warnings.append(f"No preview image ({type(exc).__name__}: {exc}); call vw_capture to check the result.")
    return ToolOutput(out, blocks)
