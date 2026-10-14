# --- graphic attributes + named resources (included by edit_engine, massing, viewport) ---
# Resource node types (Objs.TDType.h) and the Vectorworks library folder that holds each kind
# (kLibrariesFolder = 13, kDefaultsFolder = 14; verified live in Vectorworks 2024).
RES_LIBRARY = {66: (13, "Attributes/Hatches"), 96: (13, "Attributes/Line Types"), 120: (13, "Attributes/Gradients"),
               119: (13, "Attributes/Image Fills"), 108: (13, "Attributes/Tile Fills"), 109: (14, "Text Styles")}
RES_LABEL = {66: "hatch", 96: "line type", 120: "gradient", 119: "image fill", 108: "tile", 109: "text style",
             127: "wall style", 107: "slab style", 16: "symbol"}


def _res_names(rtype, folder, sub):
    if folder:
        list_id, count = vs.BuildResourceList2(rtype, folder, sub, True)
    else:
        list_id, count = vs.BuildResourceList(rtype, 0, "")
    return list_id, [vs.GetNameFromResourceList(list_id, i) for i in range(1, count + 1)]


def find_resource(rtype, name, required=True):
    """Index of a named resource; imported from the Vectorworks library when the document lacks it."""
    _l, doc_names = _res_names(rtype, 0, "")
    if name in doc_names:
        return vs.Name2Index(name)
    lib_names = []
    lib = RES_LIBRARY.get(rtype)
    if lib:
        list_id, lib_names = _res_names(rtype, lib[0], lib[1])
        if name in lib_names:
            h = vs.ImportResToCurFileN(list_id, lib_names.index(name) + 1, lambda s: 1)
            if not _nil(h):
                IMPORTED.append({"type": RES_LABEL.get(rtype, str(rtype)), "name": name})
                return vs.Name2Index(name)
    if not required:
        return 0
    close = did_you_mean(name, doc_names + [n for n in lib_names if n not in doc_names])
    raise ValueError("not_found: no %s named %r in the document or the Vectorworks library%s; list names with "
                     "vw_read(what='resources', type='%s', search='...')" % (
                         RES_LABEL.get(rtype, "resource"), name,
                         (" - did you mean: " + ", ".join(repr(n) for n in close) + "?") if close else "",
                         RES_LABEL.get(rtype, "hatch").replace(" ", "_")))


def did_you_mean(name, names, n=5):
    """Up to n close names: case-insensitive substring hits first, then difflib similarity."""
    import difflib
    lowered = name.lower()
    by_lower = {}
    for candidate in names:
        by_lower.setdefault(candidate.lower(), candidate)
    words = [w for w in lowered.replace("-", " ").replace("_", " ").split() if len(w) > 2]
    hits = [c for c in names if lowered in c.lower() or c.lower() in lowered]
    hits += [c for c in names if c not in hits and words and all(w in c.lower() for w in words)]
    hits += [c for c in names if c not in hits and words and any(w in c.lower() for w in words)]
    # Compound spellings ("Crosshatch"): candidate words of >= 4 letters contained in the wanted name.
    hits += sorted((c for c in names if c not in hits and any(
        len(w) >= 4 and w in lowered for w in c.lower().replace("-", " ").replace("_", " ").split())), key=len)
    hits += [by_lower[m] for m in difflib.get_close_matches(lowered, list(by_lower), n=n, cutoff=0.5)
             if by_lower[m] not in hits]
    return hits[:n]


IMPORTED = []
WARNINGS = []
# Plain 2D (+ wall / plug-in) types: a failed readback rolls the call back. On other (3D) objects an attribute that
# does not stick is skipped with a warning instead.
STRICT_TYPES = (2, 3, 4, 5, 6, 10, 12, 21, 63, 68, 86)
MARKERS = {"arrow": (0, 15, 0.125, 0.0), "open_arrow": (1280, 15, 0.125, 0.0), "dot": (2, 0, 0.0625, 0.0625),
           "circle": (258, 0, 0.125, 0.125), "slash": (259, 45, 0.125, 0.0), "tick": (259, 45, 0.125, 0.0),
           "cross": (260, 45, 0.125, 0.0)}
FILL_KINDS = {"hatch": 66, "gradient": 120, "image": 119, "tile": 108}


def _text_len(h):
    return len(vs.GetText(h) or "")


def _fail(what, want, got):
    raise ValueError("readback_mismatch: %s is %r after setting %r" % (what, got, want))


def apply_style(h, st):
    """st: host-normalised style dict (see builders.style_props). Sets + reads back every attribute."""
    done = {}
    t = vs.GetTypeN(h)
    soft = t not in STRICT_TYPES
    if "class" in st:
        vs.SetClass(h, st["class"])
    if "name" in st:
        vs.SetName(h, st["name"])
    if "pen_color" in st:
        vs.SetPenFore(h, tuple(int(c) for c in st["pen_color"]))
    if "line_weight" in st:
        vs.SetLW(h, int(st["line_weight"]))
    if "opacity" in st:
        vs.SetOpacityN(h, int(st["opacity"]), int(st["opacity"]))
    fill_h = h
    if t == 24 and not _nil(vs.FInGroup(h)):
        fill_h = vs.FInGroup(h)  # an extrude keeps its fill on the profile (vs.SetFPat on the extrude reads back 0)
    for part, target in ((_apply_line_style, h), (_apply_fill, fill_h)):
        try:
            part(target, st, done)
        except ValueError as exc:
            if not (soft and ("readback_mismatch" in str(exc) or "native_rejected" in str(exc))):
                raise
            WARNINGS.append("%s (type_id %s): %s - skipped" % (uuid_of(h), t, str(exc).split(":", 1)[1].strip()))
    return _apply_rest(h, st, done)


# Generic line style words -> (must contain, must not contain) for picking a real line type name.
GENERIC_LINE = {"dashed": ("dash", "dot"), "dash": ("dash", "dot"), "hidden": ("dash", "dot"),
                "dotted": ("dot", "dash"), "dot": ("dot", "dash"), "dashdot": ("dash", None),
                "dashdotted": ("dash", None), "center": ("dash", None), "centerline": ("dash", None)}


def _generic_line_type(word):
    """'dashed' -> the shortest line type whose name says dashed (document first, then the library)."""
    key = word.strip().lower().replace("-", "").replace("_", "").replace(" ", "")
    must, avoid = GENERIC_LINE[key]
    want_dot = key in ("dashdot", "dashdotted", "center", "centerline")
    names = _res_names(96, 0, "")[1]
    lib = RES_LIBRARY.get(96)
    if lib:
        names = names + [n for n in _res_names(96, lib[0], lib[1])[1] if n not in names]
    hits = [n for n in names if must in n.lower() and (avoid is None or avoid not in n.lower())
            and (not want_dot or "dot" in n.lower())]
    if not hits:
        raise ValueError("not_found: no %s line type in the document or the Vectorworks library" % word)
    return sorted(hits, key=len)[0]


def _apply_line_style(h, st, done):
    ls = st.get("line_style")
    if ls is not None:
        if ls["kind"] == "name" and ls["name"].strip().lower().replace("-", "").replace("_", "").replace(" ", "") in GENERIC_LINE:
            ls = dict(ls, name=_generic_line_type(ls["name"]))
            done["line_style_picked"] = ls["name"]
        value = ls["value"] if ls["kind"] == "index" else -find_resource(96, ls["name"])
        vs.SetLSN(h, int(value))
        got = vs.GetLSN(h)
        if got != int(value):
            _fail("line_style", ls, got)
        done["line_style"] = ls.get("name", value)


def _apply_fill(h, st, done):
    if "fill_color" in st:
        vs.SetFillFore(h, tuple(int(c) for c in st["fill_color"]))
    fill = st.get("fill")
    if fill is not None:
        kind = fill["kind"]
        if kind in ("none", "solid"):
            vs.SetFPat(h, 0 if kind == "none" else 1)
            got = vs.GetFPat(h)
            if got != (0 if kind == "none" else 1):
                _fail("fill", kind, got)
            done["fill"] = kind
        else:
            generic = str(fill.get("name") or "").strip().lower()
            if generic in FILL_KINDS or not generic:
                # fill="gradient" / {"kind": "hatch"} without a name: the first one in the document, else the library.
                kind = generic or (kind if kind in FILL_KINDS else "hatch")
                names = _res_names(FILL_KINDS[kind], 0, "")[1]
                lib = RES_LIBRARY.get(FILL_KINDS[kind])
                if not names and lib:
                    names = _res_names(FILL_KINDS[kind], lib[0], lib[1])[1]
                if not names:
                    raise ValueError("not_found: no %s resources in the document or the Vectorworks library" % kind)
                fill = dict(fill, name=names[0])
                done["fill_picked"] = names[0]
            if kind == "auto":
                kind = next((k for k in ("hatch", "gradient", "image", "tile")
                             if find_resource(FILL_KINDS[k], fill["name"], required=False)), "hatch")
            index = find_resource(FILL_KINDS[kind], fill["name"])
            if kind == "hatch":
                if not vs.SetVectorFill(h, fill["name"]):
                    raise ValueError("native_rejected: Vectorworks refused hatch %r on this object" % fill["name"])
                ok, got = vs.GetVectorFill(h)
                if not ok or got != fill["name"]:
                    _fail("fill hatch", fill["name"], got)
            else:
                vs.SetObjectVariableLongInt(h, 695, -index)  # ovFillStyle: -refnum of gradient/image/tile
                got = vs.GetObjectVariableLongInt(h, 695)
                if got != -index:
                    _fail("fill " + kind, fill["name"], got)
            done["fill"] = {kind: fill["name"]}



def _apply_rest(h, st, done):
    text_keys = [k for k in ("text_style_name", "font", "text_size", "text_style", "align", "valign") if k in st]
    if text_keys:
        if vs.GetTypeN(h) != 10:
            raise ValueError("validation: %s apply to text objects only (type_id %s)" % (", ".join(text_keys), vs.GetTypeN(h)))
        n = _text_len(h)
        if "text_style_name" in st:
            ref = find_resource(109, st["text_style_name"])
            vs.SetTextStyleRef(h, ref)
            if vs.GetTextStyleRef(h) != ref:
                _fail("text style", st["text_style_name"], vs.GetTextStyleRef(h))
        if "font" in st:
            fid = vs.GetFontID(st["font"])
            vs.SetTextFont(h, 0, n, fid)
            got = vs.GetFontName(vs.GetTextFont(h, 0))
            if got.lower() != st["font"].lower():
                raise ValueError("not_found: font %r is not installed (Vectorworks used %r)" % (st["font"], got))
        if "text_size" in st:
            vs.SetTextSize(h, 0, n, float(st["text_size"]))
            if abs(vs.GetTextSize(h, 0) - float(st["text_size"])) > 0.01:
                _fail("text_size", st["text_size"], vs.GetTextSize(h, 0))
        if "text_style" in st:
            # Live (VW 2024): SetTextStyle(0) clears the face, any other value TOGGLES those bits -> clear, then set.
            vs.SetTextStyle(h, 0, n, 0)
            if int(st["text_style"]):
                vs.SetTextStyle(h, 0, n, int(st["text_style"]))
            if vs.GetTextStyle(h, 0) != int(st["text_style"]):
                _fail("text_style", st["text_style"], vs.GetTextStyle(h, 0))
        if "align" in st:
            vs.SetTextJust(h, int(st["align"]))
            if vs.GetTextJust(h) != int(st["align"]):
                _fail("align", st["align"], vs.GetTextJust(h))
        if "valign" in st:
            vs.SetTextVerticalAlign(h, int(st["valign"]))
            if vs.GetTextVerticalAlign(h) != int(st["valign"]):
                _fail("valign", st["valign"], vs.GetTextVerticalAlign(h))
        done["text"] = text_keys
    for key in ("start_marker", "end_marker"):
        m = st.get(key)
        if m is None:
            continue
        style, angle, size, width = MARKERS.get(m.get("preset") or "arrow", MARKERS["arrow"])
        size = float(m.get("size_in", size))
        width = float(m.get("width_in", width if width else 0.0))
        setter = vs.SetObjBeginningMarker if key == "start_marker" else vs.SetObjEndMarker
        getter = vs.GetObjBeginningMarker if key == "start_marker" else vs.GetObjEndMarker
        if not setter(h, style, angle, size, width, 0, 0.0, bool(m["visible"])):
            raise ValueError("native_rejected: Vectorworks refused the %s on this object (lines, arcs, polylines "
                             "and walls take markers)" % key)
        ok, g_style, _a, _s, _w, _tb, _t, g_vis = getter(h)
        if not ok or bool(g_vis) != bool(m["visible"]) or (m["visible"] and g_style != style):
            _fail(key, m, [g_style, g_vis])
        done[key] = m.get("preset") if m["visible"] else "none"
    vs.ResetObject(h)
    return done
