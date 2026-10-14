# --- per-type geometry + Space room name (included by object_detail, edit_engine, massing, query_extra) ---
# Type ids (Objs.TDType.h, live VW 2024): 2 line, 3 rect, 4 oval, 5 polygon, 6 arc, 10 text, 11 group, 15 symbol,
# 21 polyline, 24 extrude, 63 linear dimension, 68 wall, 71 slab, 84 roof, 86 plug-in object, 89 round wall,
# 122 viewport.
TYPE_NAMES = {2: "line", 3: "rect", 4: "oval", 5: "polygon", 6: "arc", 10: "text", 11: "group", 15: "symbol",
              21: "polyline", 24: "extrude", 63: "dimension", 68: "wall", 71: "slab", 86: "parametric",
              89: "round_wall", 122: "viewport"}
# Space label (live VW 2024): the native create writes the requested name into the label text; the plug-in keeps
# 'NumberLabelText' as the master and mirrors it to '11_Number' / 'Number' on regeneration (setting only 'Number' or
# '11_Number' is reverted). 'Name'/'11_Name' stay "Custom" (no second label line).
SPACE_LABEL_FIELDS = ("NumberLabelText", "11_Number", "Number")


def _r(v, nd=1):
    try:
        v = round(float(v), nd)
        return int(v) if v == int(v) else v
    except (TypeError, ValueError):
        return v


def _xy(p):
    return [_r(MM(p[0])), _r(MM(p[1]))]


def plugin_of(h):
    if vs.GetTypeN(h) != 86:
        return ""
    rec = vs.GetParametricRecord(h)
    return "" if _nil(rec) else vs.GetName(rec)


def _num(h, plugin, field, length=True):
    raw = vs.GetRField(h, plugin, field)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return raw if raw != "" else None
    return _r(MM(value)) if length else _r(value)


def space_label(h):
    for field in SPACE_LABEL_FIELDS:
        value = vs.GetRField(h, "Space", field)
        if value:
            return value
    return ""


def set_space_label(h, text):
    for field in SPACE_LABEL_FIELDS:
        vs.SetRField(h, "Space", field, text)
    vs.ResetObject(h)
    got = vs.GetRField(h, "Space", "NumberLabelText")
    if got != text:
        raise ValueError("readback_mismatch: Space label is %r after setting %r" % (got, text))


def geometry(h):
    """Compact per-type geometry in mm (no records)."""
    t = vs.GetTypeN(h)
    g = {}
    try:
        if t in (2, 68, 89):
            p1, p2 = vs.GetSegPt1(h), vs.GetSegPt2(h)
            g["start"], g["end"] = _xy(p1), _xy(p2)
            g["length"] = _r(MM(((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2) ** 0.5))
            if t != 2:
                ok, th = vs.GetWallThickness(h)
                if ok:
                    g["thickness"] = _r(MM(th))
                st, sb, et, eb = vs.GetWallHeight(h)
                g["height"] = _r(MM(st - sb))
                if abs((et - eb) - (st - sb)) > 1e-6:
                    g["end_height"] = _r(MM(et - eb))
                style = vs.GetWallStyle(h)
                if style:
                    g["style"] = style
                inserts = []
                c = vs.FIn3D(h)
                while not _nil(c) and len(inserts) < 20:
                    name = plugin_of(c)
                    if name:
                        inserts.append({"uuid": uuid_of(c), "plugin": name})
                    c = vs.NextObj(c)
                if inserts:
                    g["inserts"] = inserts
        elif t in (3, 4):
            p1, p2 = vs.GetBBox(h)
            g["origin"] = [_r(MM(min(p1[0], p2[0]))), _r(MM(min(p1[1], p2[1])))]
            g["width"], g["height"] = _r(MM(abs(p2[0] - p1[0]))), _r(MM(abs(p2[1] - p1[1])))
        elif t in (5, 21, 71):
            n = vs.GetVertNum(h)
            g["vertex_count"] = n
            g["points"] = [_xy(vs.GetPolyPt(h, i)) for i in range(1, min(n, 50) + 1)]
            if t != 71:
                g["closed"] = bool(vs.IsPolyClosed(h))
        elif t == 6:
            p1, p2 = vs.GetBBox(h)
            g["bounds"] = [_xy(p1), _xy(p2)]
            start, sweep = vs.GetArc(h)
            g["start_angle"], g["sweep_angle"] = _r(start), _r(sweep)
        elif t == 10:
            text = vs.GetText(h) or ""
            g["text"] = text[:300] + ("..." if len(text) > 300 else "")
            g["font"] = vs.GetFontName(vs.GetTextFont(h, 0))
            g["size_pt"] = _r(vs.GetTextSize(h, 0), 2)
            p1, p2 = vs.GetBBox(h)
            g["bounds"] = [_xy(p1), _xy(p2)]
        elif t == 15:
            g["symbol"] = vs.GetSymName(h)
            g["at"] = _xy(vs.GetSymLoc(h))
            g["rotation"] = _r(vs.GetSymRot(h))
        elif t == 24:
            height, width, depth = vs.Get3DInfo(h)
            centre, z = vs.Get3DCntr(h)
            g["height"] = _r(MM(height))
            g["base"] = _r(MM(z - height / 2.0))
            g["footprint"] = [_r(MM(width)), _r(MM(depth))]
        elif t == 63:
            g["start"], g["end"] = _xy(vs.GetSegPt1(h)), _xy(vs.GetSegPt2(h))
        elif t == 122:
            g["scale"] = _r(vs.GetObjectVariableReal(h, 1003), 3)
            g["view"] = vs.GetObjectVariableInt(h, 1004)
        elif t == 86:
            name = plugin_of(h)
            g["plugin"] = name
            if name in ("Door", "Window"):
                g["width"] = _num(h, name, "Width")
                g["height"] = _num(h, name, "Height")
                if name == "Window":
                    elevation = _num(h, name, "Elevation")
                    if vs.GetRField(h, name, "ElevationSetAt").startswith("Head") and                             isinstance(elevation, (int, float)) and isinstance(g["height"], (int, float)):
                        g["sill"] = _r(elevation - g["height"])
                        g["head"] = elevation
                    else:
                        g["sill"] = elevation
                else:
                    g["open_angle"] = _num(h, name, "OpenAngle", length=False)
                    g["swing"] = vs.GetRField(h, name, "SwingDirection") or None
                g["at"] = _xy(vs.GetSymLoc(h))
                parent = vs.GetParent(h)
                if not _nil(parent) and vs.GetTypeN(parent) in (68, 89):
                    g["wall"] = uuid_of(parent)
            elif name == "Space":
                g["room_name"] = space_label(h)
                g["room_id"] = vs.GetRField(h, name, "11_Room ID")
                g["height"] = _num(h, name, "11_Gross Height") or _num(h, name, "Extrusion Height")
                g["area"] = vs.GetRField(h, name, "Area")
            else:
                g["at"] = _xy(vs.GetSymLoc(h))
    except Exception as exc:  # geometry is best effort; the record itself is still returned
        g["geometry_error"] = "%s: %s" % (type(exc).__name__, exc)
    return {k: v for k, v in g.items() if v not in (None, "", [])}
