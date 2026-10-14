"""vw_view: standard view, projection, render mode, zoom/center/fit, saved
views, active layer. Non-destructive."""

from __future__ import annotations

from .. import coerce as C
from ..errors import VWError
from ..host import doc_brief, get_host
from .capture import view_codes

PROJECTIONS = {"orthogonal": 0, "perspective": 1, "plan": 6}


def handle(args: dict, ctx, warnings: list) -> dict:
    a = C.lower_keys(args)
    host = get_host()
    target = host.session.target(a.get("document"))
    inst = target.inst
    out: dict = {"ok": True}
    if target.document:
        out["document"] = doc_brief(target.document)
    params = view_codes(a)
    if a.get("projection") not in (None, ""):
        params["projection"] = PROJECTIONS[C.choice(a["projection"], "projection", list(PROJECTIONS),
                                                    {"ortho": "orthogonal", "persp": "perspective", "top_plan": "plan"})]
    fit = a.get("fit")
    if fit not in (None, "", False):
        fit = "drawing" if fit is True else C.choice(fit, "fit", ["drawing", "selection", "objects", "layer"],
                                                     {"all": "drawing", "extents": "drawing", "true": "drawing"})
    else:
        fit = None
    save_as = a.get("save_view_as") or a.get("save_as")
    saved = a.get("saved_view")
    layer = a.get("layer")
    zoom = a.get("zoom")
    center = a.get("center")
    if not params and not fit and not save_as and not saved and not layer and zoom in (None, "") and center in (None, ""):
        out["current"] = _clean(host.call(target, "get_view", {}))
        return out
    if inst.v2:
        if fit:
            params["fit"] = fit
            if fit == "objects":
                params["object_uuids"] = [u[5:] if u.startswith("uuid:") else u for u in C.str_list(a.get("objects"), "objects")]
        if zoom not in (None, ""):
            params["zoom"] = C.number(zoom, "zoom")
        if center not in (None, ""):
            params["center"] = C.point(center, "center", C.scale_for(C.units_name(a.get("units"))))
        if a.get("margin_pct") not in (None, ""):
            params["margin_pct"] = C.number(a["margin_pct"], "margin_pct")
        if saved:
            params["saved_view"] = str(saved)
        if layer:
            params["layer"] = str(layer)
        if params:
            try:
                result = host.call(target, "set_view", params, kind="write")
            except VWError as exc:
                if exc.code != "readback_mismatch":
                    raise
                # Seen once live (Codex, right_iso after layer edits): the projection settled only on a second
                # pass. Re-apply once; a second mismatch is reported.
                result = host.call(target, "set_view", params, kind="write")
            if isinstance(result, dict):
                out["previous"] = _clean(result.get("previous"))
                out["current"] = _clean(result.get("current", result))
        if save_as:
            if inst.supports("saved_views"):
                host.call(target, "saved_views", {"action": "create", "name": str(save_as)}, kind="write")
            else:
                host.script(target, "saved_views", {"action": "create", "name": str(save_as)}, label="Save view")
            out["saved_view_created"] = str(save_as)
        return out
    # ---- pre-1.0 bridge ----
    unsupported = [k for k, v in (("zoom", zoom), ("center", center), ("saved_view", saved), ("layer", layer),
                                  ("save_view_as", save_as)) if v not in (None, "")]
    if unsupported:
        raise VWError("upgrade_required", f"{', '.join(unsupported)} need the 1.0 bridge; the running bridge only sets "
                      "standard view, projection, render mode and fit.", details={"unsupported": unsupported})
    out["previous"] = _clean(host.call(target, "get_view", {}))
    if fit:
        if fit != "drawing":
            raise VWError("upgrade_required", f"fit='{fit}' needs the 1.0 bridge; the running bridge can only fit the drawing.")
        params["fit_to_objects"] = True
        params["clear_selection"] = True
        warnings.append("Pre-1.0 bridge: fitting the drawing clears the selection.")
    result = host.call(target, "set_view", params, kind="write")
    out["current"] = _clean(result)
    return out


def _clean(value):
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "timing"}
    return value
