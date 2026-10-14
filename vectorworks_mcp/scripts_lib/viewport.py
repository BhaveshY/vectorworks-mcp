# ARGS = {"action": "create"|"set"|"update"|"delete", "target": ref|None, "sheet": name|None,
#         "create_sheet": bool, "name", "new_name", "layers": {"show": [..], "gray": [..]}|None, "scale": float|None,
#         "view": [standard_view, projection]|None, "render": int|None, "at": [x, y] sheet mm|None,
#         "crop": [[x, y] model mm, ...]|"clear"|None, "classes": {name: 0|-1|2}|None, "title": str|None,
#         "annotations": [{"type": text|line|rect|polygon|dimension, ...model mm..., "style": {}}]|None}
# Live findings (Vectorworks 2024): vs.CreateVP starts with every layer AND class invisible and view type 0, so
# layers/classes/view are always set before vs.UpdateVP (else the viewport draws an empty red X box); a crop moves the
# viewport, so the position is applied last; vs.GetName returns '' for layers (vs.GetLName works);
# ovViewportX/YPosition (1024/1025, read-only) is where the model origin sits on the sheet: crop and annotation
# objects live in the viewport's internal frame = model + position * scale (plan views), so they are offset by it.
OV_PROJECTION, OV_RENDER, OV_SCALE, OV_DIRTY, OV_VIEW, OV_TITLE, OV_X, OV_Y = 1000, 1001, 1003, 1004, 1007, 1032, 1024, 1025
LAYER_DESIGN, LAYER_SHEET = 1, 2
WARN = []
A = ARGS
_prev_layer = vs.GetLName(vs.ActLayer())


def _layer(name, kind):
    h = vs.GetLayerByName(name)
    if _nil(h):
        return None
    if vs.GetObjectVariableInt(h, 154) != kind:
        raise ValueError("validation: layer %r is a %s layer, not a %s layer" % (
            name, "sheet" if kind == LAYER_DESIGN else "design", "design" if kind == LAYER_DESIGN else "sheet"))
    return h


def _layers(kind):
    out = []
    h = vs.FLayer()
    while not _nil(h):
        if vs.GetObjectVariableInt(h, 154) == kind:
            out.append(h)
        h = vs.NextLayer(h)
    return out


def _classes():
    return [vs.ClassList(i) for i in range(1, vs.ClassNum() + 1)]


def _box(vp):
    p1, p2 = vs.GetBBox(vp)
    return [(p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0], [abs(p2[0] - p1[0]), abs(p1[1] - p2[1])]


def _viewport(ref):
    h = resolve(ref)
    if vs.GetTypeN(h) != 122:
        raise ValueError("validation: %s is not a viewport (type_id %s)" % (ref, vs.GetTypeN(h)))
    return h


def _set_layers(vp, spec):
    names = {vs.GetLName(h): h for h in _layers(LAYER_DESIGN)}
    unknown = [n for n in spec.get("show", []) + spec.get("gray", []) if n not in names]
    if unknown:
        raise ValueError("not_found: design layer(s) %s not found (design layers: %s)" % (unknown, sorted(names)))
    for name, h in names.items():
        vis = 0 if name in spec.get("show", []) else 2 if name in spec.get("gray", []) else -1
        if not vs.SetVPLayerVisibility(vp, h, vis):
            raise ValueError("native_rejected: Vectorworks refused layer visibility for %r" % name)


def _set_classes(vp, overrides, base):
    names = _classes()
    unknown = [n for n in (overrides or {}) if n not in names]
    if unknown:
        raise ValueError("not_found: class(es) %s not found" % unknown)
    for name in names:
        if overrides and name in overrides:
            vis = int(overrides[name])
        elif base:
            vis = vs.GetCVis(name)
        else:
            continue
        if not vs.SetVPClassVisibility(vp, name, vis):
            raise ValueError("native_rejected: Vectorworks refused class visibility for %r" % name)


FRAME = [0.0, 0.0]


def _frame(vp):
    scale = vs.GetObjectVariableReal(vp, OV_SCALE)
    FRAME[0] = vs.GetObjectVariableReal(vp, OV_X) * scale
    FRAME[1] = vs.GetObjectVariableReal(vp, OV_Y) * scale


def VP(p):
    """Model point (mm) -> viewport-internal point (document units)."""
    return (U(p[0]) + FRAME[0], U(p[1]) + FRAME[1])


def _poly(points):
    vs.BeginPoly()
    for p in points:
        vs.AddPoint(VP(p))
    vs.EndPoly()
    return vs.LNewObj()


def _annotation(vp, item):
    kind = item["type"]
    if kind == "text":
        vs.TextOrigin(VP(item["at"]))
        vs.CreateText(item["text"])
        h = vs.LNewObj()
    elif kind == "line":
        vs.MoveTo(VP(item["start"]))
        vs.LineTo(VP(item["end"]))
        h = vs.LNewObj()
    elif kind == "rect":
        vs.Rect(VP(item["start"]), VP(item["end"]))
        h = vs.LNewObj()
    elif kind == "polygon":
        h = _poly(item["points"])
    elif kind == "dimension":
        a, b = item["start"], item["end"]
        horizontal = abs(b[0] - a[0]) >= abs(b[1] - a[1])
        # dimType 0 = horizontal, 1 = vertical (2/3 draw a broken dimension; checked visually in Vectorworks 2024)
        vs.LinearDim(VP(a), VP(b), U(item.get("offset", 0.0)), 0 if horizontal else 1, 0, 0, 0.0)
        h = vs.LNewObj()
    else:
        raise ValueError("validation: unsupported annotation type " + kind)
    if _nil(h):
        raise ValueError("native_rejected: annotation %s was not created" % kind)
    if item.get("style"):
        apply_style(h, item["style"])
    if not vs.AddVPAnnotationObject(vp, h) or not vs.IsVPGroupContainedObject(h, 2):
        raise ValueError("native_rejected: Vectorworks did not add the %s to the viewport annotations" % kind)
    return {"type": kind, "uuid": uuid_of(h)}


def _record(vp):
    center, size = _box(vp)
    shown, gray = [], []
    for h in _layers(LAYER_DESIGN):
        _ok, vis = vs.GetVPLayerVisibility(vp, h)
        if vis == 0:
            shown.append(vs.GetLName(h))
        elif vis == 2:
            gray.append(vs.GetLName(h))
    hidden = [n for n in _classes() if vs.GetVPClassVisibility(vp, n)[1] == -1]
    group = vs.GetVPGroup(vp, 2)
    count = 0
    h = None if _nil(group) else vs.FInGroup(group)
    while not _nil(h):
        count += vs.GetTypeN(h) != 76  # kUserDataNode: internal, not an annotation
        h = vs.NextObj(h)
    return {"uuid": uuid_of(vp), "name": vs.GetName(vp), "sheet": vs.GetLName(vs.GetLayer(vp)),
            "scale": vs.GetObjectVariableReal(vp, OV_SCALE),
            "view_code": [vs.GetObjectVariableInt(vp, OV_VIEW), vs.GetObjectVariableInt(vp, OV_PROJECTION)],
            "render_code": vs.GetObjectVariableInt(vp, OV_RENDER), "layers": shown, "gray_layers": gray,
            "hidden_classes": hidden, "center": [MM(center[0]), MM(center[1])], "size": [MM(size[0]), MM(size[1])],
            "crop": bool(vs.VPHasCropObject(vp)), "title": vs.GetObjectVariableString(vp, OV_TITLE) or "",
            "annotations": count}


def _apply(vp, creating):
    before = _box(vp)[0]
    regen = creating
    if A.get("view") is not None:
        vs.SetObjectVariableInt(vp, OV_VIEW, int(A["view"][0]))
        vs.SetObjectVariableInt(vp, OV_PROJECTION, int(A["view"][1]))
        regen = True
    if A.get("render") is not None:
        vs.SetObjectVariableInt(vp, OV_RENDER, int(A["render"]))
        regen = True
    if A.get("scale") is not None:
        vs.SetObjectVariableReal(vp, OV_SCALE, float(A["scale"]))
        regen = True
    if A.get("layers") is not None:
        _set_layers(vp, A["layers"])
        regen = True
    if creating or A.get("classes"):
        _set_classes(vp, A.get("classes"), creating)
        regen = True
    name = A.get("new_name") if not creating else A.get("name")
    if name:
        other = vs.GetObject(name)
        if not _nil(other) and uuid_of(other) != uuid_of(vp):
            raise ValueError("already_exists: another object is already named %r" % name)
        vs.SetName(vp, name)
    if A.get("title") is not None:
        vs.SetObjectVariableString(vp, OV_TITLE, A["title"])
    if regen:
        vs.SetObjectVariableBoolean(vp, OV_DIRTY, True)
        vs.UpdateVP(vp)
        vs.ResetObject(vp)
    _frame(vp)
    crop = A.get("crop")
    if (crop and crop != "clear") or A.get("annotations"):
        # Objects drawn on a sheet layer are in paper units: a crop made there is scaled by the viewport scale.
        # Draw on a design layer (model units); the user's active layer is restored at the end.
        if vs.GetObjectVariableInt(vs.ActLayer(), 154) != LAYER_DESIGN:
            vs.Layer(vs.GetLName(_layers(LAYER_DESIGN)[0]))
    if crop == "clear":
        old = vs.GetVPCropObject(vp)
        if not _nil(old):
            vs.DelObject(old)
        vs.ResetObject(vp)
    elif crop:
        c = _poly(crop) if len(crop) > 2 else None
        if c is None:
            vs.Rect(VP(crop[0]), VP(crop[1]))
            c = vs.LNewObj()
        if not vs.SetVPCropObject(vp, c):
            raise ValueError("native_rejected: Vectorworks did not accept the crop shape")
        vs.ResetObject(vp)
        q1, q2 = vs.GetBBox(vs.GetVPCropObject(vp))
        want = U(max(p[0] for p in crop) - min(p[0] for p in crop))
        if abs(abs(q2[0] - q1[0]) - want) > max(0.01 * want, 1e-6):
            raise ValueError("readback_mismatch: crop width %s, expected %s (document units)" % (abs(q2[0] - q1[0]), want))
    made = [_annotation(vp, item) for item in (A.get("annotations") or [])]
    if made:
        vs.ResetObject(vp)
    target = A.get("at")
    goal = [U(target[0]), U(target[1])] if target is not None else ([0.0, 0.0] if creating else before)
    now = _box(vp)[0]
    moved = creating or target is not None or crop or any(A.get(k) is not None for k in ("view", "scale", "layers"))
    if moved and (abs(goal[0] - now[0]) > 1e-6 or abs(goal[1] - now[1]) > 1e-6):
        vs.HMove(vp, goal[0] - now[0], goal[1] - now[1])
        vs.ResetObject(vp)
    return made


def _check(vp, rec):
    if A.get("scale") is not None and abs(rec["scale"] - float(A["scale"])) > 1e-6:
        raise ValueError("readback_mismatch: viewport scale is %s" % rec["scale"])
    if A.get("view") is not None and rec["view_code"] != [int(A["view"][0]), int(A["view"][1])]:
        # Vectorworks turns Top/Plan (7, projection 6) into Top (7, orthogonal 0) for every rendered mode.
        if rec["view_code"][0] == int(A["view"][0]) and int(A["view"][1]) == 6 and rec["view_code"][1] == 0                 and rec["render_code"] != 0:
            WARN.append("Vectorworks shows a rendered Top/Plan viewport as Top (orthogonal projection); "
                        "use render_mode='wireframe' to keep the 2D plan projection.")
        else:
            raise ValueError("readback_mismatch: viewport view is %s" % rec["view_code"])
    if A.get("render") is not None and rec["render_code"] != int(A["render"]):
        raise ValueError("readback_mismatch: viewport render mode is %s" % rec["render_code"])
    if A.get("layers") is not None and (sorted(rec["layers"]) != sorted(A["layers"].get("show", [])) or
                                        sorted(rec["gray_layers"]) != sorted(A["layers"].get("gray", []))):
        raise ValueError("readback_mismatch: viewport layers are %s" % rec["layers"])
    if A.get("at") is not None and (abs(rec["center"][0] - A["at"][0]) > 0.5 or abs(rec["center"][1] - A["at"][1]) > 0.5):
        raise ValueError("readback_mismatch: viewport centre is %s" % rec["center"])
    if A.get("crop") == "clear" and rec["crop"] or (A.get("crop") not in (None, "clear") and not rec["crop"]):
        raise ValueError("readback_mismatch: viewport crop is %s" % rec["crop"])


action = A["action"]
out = {"action": action}
if action == "create":
    sheet = _layer(A["sheet"], LAYER_SHEET)
    if sheet is None:
        if not A.get("create_sheet", True):
            raise ValueError("not_found: sheet layer %r not found" % A["sheet"])
        sheet = vs.CreateLayer(A["sheet"], LAYER_SHEET)
        if _nil(sheet):
            raise ValueError("native_rejected: could not create sheet layer %r" % A["sheet"])
        out["sheet_created"] = A["sheet"]
    if A.get("layers") is None:
        A["layers"] = {"show": [vs.GetLName(h) for h in _layers(LAYER_DESIGN) if vs.GetLVis(h) == 0],
                       "gray": [vs.GetLName(h) for h in _layers(LAYER_DESIGN) if vs.GetLVis(h) == 2]}
    if A.get("scale") is None:
        first = [h for h in _layers(LAYER_DESIGN) if vs.GetLName(h) in A["layers"]["show"]]
        A["scale"] = float(vs.GetLScale(first[0])) if first else 100.0
    if A.get("view") is None:
        A["view"] = [7, 6]
    if A.get("render") is None:
        A["render"] = 0
    vp = vs.CreateVP(sheet)
    if _nil(vp) or vs.GetTypeN(vp) != 122:
        raise ValueError("native_rejected: Vectorworks did not create the viewport")
    out["annotations_created"] = _apply(vp, True)
elif action == "set":
    vp = _viewport(A["target"])
    out["annotations_created"] = _apply(vp, False)
elif action == "update":
    if A.get("target"):
        vps = [_viewport(A["target"])]
    else:
        sheet = _layer(A["sheet"], LAYER_SHEET)
        if sheet is None:
            raise ValueError("not_found: sheet layer %r not found" % A["sheet"])
        vps = []
        h = vs.FInLayer(sheet)
        while not _nil(h):
            if vs.GetTypeN(h) == 122:
                vps.append(h)
            h = vs.NextObj(h)
    for vp in vps:
        vs.SetObjectVariableBoolean(vp, OV_DIRTY, True)
        vs.UpdateVP(vp)
        vs.ResetObject(vp)
    out["updated"] = [uuid_of(vp) for vp in vps]
    vp = vps[0] if len(vps) == 1 else None
elif action == "list":
    ref = str(A.get("sheet") or "")
    if ref:
        sheet = resolve(ref) if ref.startswith("uuid:") or len(ref) >= 32 else _layer(ref, LAYER_SHEET)
        if sheet is None:
            raise ValueError("not_found: sheet layer %r not found" % A["sheet"])
        sheets = [sheet]
    else:
        sheets = _layers(LAYER_SHEET)  # no sheet given: every sheet layer
    out["viewports"] = []
    for sheet in sheets:
        h = vs.FInLayer(sheet)
        while not _nil(h):
            if vs.GetTypeN(h) == 122:
                out["viewports"].append(h)
            h = vs.NextObj(h)
    out["viewports"] = [_record(x) for x in out["viewports"]]
    vp = None
elif action == "annotations":
    vp = _viewport(A["target"])
    group = vs.GetVPGroup(vp, 2)
    items = []
    h = None if _nil(group) else vs.FInGroup(group)
    while not _nil(h):
        if vs.GetTypeN(h) != 76:
            row = record_of(h)
            row.pop("layer", None)
            if vs.GetTypeN(h) == 10:
                row["text"] = vs.GetText(h)
            items.append(row)
        h = vs.NextObj(h)
    out["annotations"] = items
    vp = None
elif action == "delete":
    vp = _viewport(A["target"])
    out["deleted"] = {"uuid": uuid_of(vp), "name": vs.GetName(vp)}
    vs.DelObject(vp)
    vp = None
else:
    raise ValueError("validation: unknown viewport action " + action)
if vp is not None:
    out["viewport"] = _record(vp)
    if action in ("create", "set"):
        _check(vp, out["viewport"])
if vs.GetLName(vs.ActLayer()) != _prev_layer and _prev_layer:
    vs.Layer(_prev_layer)
if IMPORTED:
    out["imported_resources"] = IMPORTED
if WARN or WARNINGS:
    out["warnings"] = WARN + WARNINGS
result = out
