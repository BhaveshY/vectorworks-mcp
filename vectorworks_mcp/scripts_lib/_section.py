# Strict 2D section geometry. All values crossing the host boundary are millimetres.
# Dimension selectors 13/14 are SDK ovDimStartPt/ovDimEndPt. SetSegPt does NOT edit dimensions.
# These selectors are SDK-internal; guarded by type and live readback (VW 2024 acceptance).
def section_walk(roots):
    rows, seen = [], set()
    def visit(h, depth):
        uid = uuid_of(h)
        if uid in seen:
            return
        if depth > 32 or len(rows) >= 5000:
            raise ValueError("validation: section exceeds 32 group levels or 5000 objects; split the scope")
        seen.add(uid)
        rows.append(h)
        if vs.GetTypeN(h) == 11:
            child = vs.FInGroup(h)
            while not _nil(child):
                visit(child, depth + 1)
                child = vs.NextObj(child)
    for root in roots:
        visit(root, 0)
    return rows


def section_state(h):
    t = vs.GetTypeN(h)
    out = record_of(h)
    parent = vs.GetParent(h)
    out["parent_uuid"] = uuid_of(parent)
    out["type"] = TYPE_NAMES.get(t, "type_%s" % t)
    if t in (2, 63, 68):
        out["points"] = [[MM(v) for v in vs.GetSegPt1(h)], [MM(v) for v in vs.GetSegPt2(h)]]
        if t == 63:
            out["dimension_text"] = vs.GetDimText(h)
            out["dimension_class"] = vs.GetObjectVariableInt(h, 26)
            out["offset"] = MM(vs.GetObjectVariableReal(h, 15))
            out["show_value"] = vs.GetObjectVariableBoolean(h, 6)
    elif t in (5, 21):
        n = vs.GetVertNum(h)
        if n > 10000:
            raise ValueError("validation: polygon exceeds 10000 vertices")
        out["points"], out["vertex_types"], out["radii"] = [], [], []
        for i in range(1, n + 1):
            if t == 21:
                p, kind, radius = vs.GetPolylineVertex(h, i)
            else:
                p, kind, radius = vs.GetPolyPt(h, i), 0, 0.0
            out["points"].append([MM(p[0]), MM(p[1])])
            out["vertex_types"].append(kind)
            out["radii"].append(MM(radius))
        out["closed"] = bool(vs.IsPolyClosed(h))
        out["holes"] = vs.GetNumHoles(h)[1] if t == 21 else 0
    elif t == 3:
        a, b = vs.GetBBox(h)
        out["points"] = [[MM(a[0]), MM(a[1])], [MM(b[0]), MM(b[1])]]
    elif t == 10:
        p, angle, mirrored = vs.GetTextOrientation(h)
        out["points"] = [[MM(p[0]), MM(p[1])]]
        out["text"] = vs.GetText(h)
    elif t != 11:
        out.update(geometry(h))
    return out


def section_revision(rows):
    import hashlib
    import json
    digest = hashlib.sha256()
    vertices = 0
    for h in rows:
        state = section_state(h)
        vertices += len(state.get("points", []))
        if vertices > 200000:
            raise ValueError("validation: section exceeds 200000 vertices; split the scope")
        digest.update(json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def section_plane(h):
    planar, offset, rx, ry, rz = vs.GetEntityMatrix(h)
    if planar and (abs(rx) > 1e-7 or abs(ry) > 1e-7 or abs(rz) > 1e-7 or any(abs(v) > 1e-7 for v in offset)):
        raise ValueError("validation: geometry must use the untransformed XY plane")


def section_check(h, state):
    t = state["type_id"]
    if t not in (2, 3, 5, 10, 11, 21, 63):
        raise ValueError("validation: unsupported section object %s (%s); use model/PIO tools for BIM or viewport geometry" % (state["uuid"], state["type"]))
    if vs.GetObjectVariableBoolean(h, 700):
        raise ValueError("validation: locked section object " + state["uuid"])
    if t == 63 and (state["dimension_class"] not in (0, 1) or not state["show_value"]):
        raise ValueError("validation: section dimensions must be linear and display their measured value")
    if state.get("holes") or any(state.get("vertex_types", [])):
        raise ValueError("validation: section deformation requires straight vertices without holes; use explicit reshape for curves")
    if t != 11:
        section_plane(h)


def section_set_points(h, state, points):
    t = state["type_id"]
    if t in (2, 68):
        vs.SetSegPt1(h, PT(points[0]))
        vs.SetSegPt2(h, PT(points[1]))
    elif t == 63:
        for selector, p in zip((13, 14), points):
            vs.SetObjectVariablePoint(h, selector, (U(p[0]), U(p[1]), 0.0))
    elif t in (5, 21):
        if len(points) != len(state["points"]):
            raise ValueError("validation: reshape preserves vertex count; supply every vertex")
        for i, p in enumerate(points, 1):
            if t == 21:
                vs.SetPolylineVertex(h, i, PT(p), state["vertex_types"][i-1], U(state["radii"][i-1]), True)
            else:
                vs.SetPolyPt(h, i, U(p[0]), U(p[1]))
    elif t == 3:
        vs.SetBBox(h, PT(points[0]), PT(points[1]))
    elif t == 10:
        old = state["points"][0]
        vs.HMove(h, U(points[0][0] - old[0]), U(points[0][1] - old[1]))
    vs.ResetObject(h)
    got = section_state(h)
    if len(got.get("points", [])) != len(points) or any(abs(a-b) > 0.01 for p,q in zip(got.get("points", []), points) for a,b in zip(p,q)):
        raise ValueError("readback_mismatch: section geometry " + state["uuid"])
    if got["parent_uuid"] != state["parent_uuid"] or got["type_id"] != t:
        raise ValueError("readback_mismatch: section object identity/container changed")
    for key in ("closed", "vertex_types", "radii", "offset", "dimension_class", "show_value"):
        if key in state and got.get(key) != state[key]:
            raise ValueError("readback_mismatch: geometry attribute changed: " + key)
    return got


def section_y(y, bands):
    return y + sum(b["delta"] * max(0.0, min(1.0, (y-b["bottom"])/(b["top"]-b["bottom"]))) for b in bands)


def section_edit(item, handles, done):
    expected = {uuid_of(resolve(ref)): revision for ref, revision in item["expected_geometry"].items()}
    if set(expected) != {uuid_of(h) for h in handles}:
        raise ValueError("validation: expected_geometry must cover exactly the section roots")
    for h in handles:
        if section_revision(section_walk([h])) != expected[uuid_of(h)]:
            raise ValueError("validation: section geometry changed since read (or edit already applied); re-read before planning")
    rows = section_walk(handles)
    stair_members = {}
    for stair in item["stairs"]:
        for h in section_walk([resolve(stair["target"])]):
            if vs.GetTypeN(h) == 11:
                continue
            uid = uuid_of(h)
            if uid in stair_members:
                raise ValueError("validation: overlapping stair flight scopes")
            stair_members[uid] = stair
    anchors = {}
    for anchor in item["fixed_points"]:
        uid = uuid_of(resolve(anchor["target"]))
        anchors.setdefault(uid, set()).add(anchor["vertex"])
    if set(anchors) & set(stair_members):
        raise ValueError("validation: fixed_points cannot override an equal-riser stair flight")
    labels = {}
    for label in item["labels"]:
        uid = uuid_of(resolve(label["target"]))
        if uid in labels:
            raise ValueError("validation: duplicate level label " + uid)
        labels[uid] = label
    preserved = {uuid_of(resolve(ref)) for ref in item["preserve_text"]}
    prepared = []
    texts = set()
    for h in rows:
        state = section_state(h)
        section_check(h, state)
        uid = state["uuid"]
        if state["type_id"] == 11:
            continue
        points = [[p[0], section_y(p[1], item["bands"])] for p in state["points"]]
        if uid in stair_members:
            stair = stair_members[uid]
            bottom, top = stair["bottom"], stair["top"]
            if state["type_id"] not in (2, 5, 21) or any(p[1] < bottom-.01 or p[1] > top+.01 for p in state["points"]):
                raise ValueError("validation: stair scope must contain straight 2D flight geometry inside its bottom/top")
            new_bottom, new_top = section_y(bottom, item["bands"]), section_y(top, item["bands"])
            points = [[p[0], new_bottom+(p[1]-bottom)*(new_top-new_bottom)/(top-bottom)] for p in state["points"]]
        for vertex in anchors.get(uid, []):
            if not 1 <= vertex <= len(points):
                raise ValueError("validation: fixed_points vertex is outside geometry")
            points[vertex-1] = list(state["points"][vertex-1])
        text = None
        if state["type_id"] == 10:
            texts.add(uid)
            if uid in labels:
                l = labels[uid]
                elevation = section_y(l["elevation"], item["bands"])
                points[0][1] = state["points"][0][1] + elevation - l["elevation"]
                text = l["template"].replace("{level}", format(elevation / 1000.0, "+.%df" % l["precision"]))
                text = text.replace("{absolute}", format((elevation + l["datum"]) / 1000.0, ".%df" % l["precision"]))
            elif uid not in preserved:
                raise ValueError("validation: bind text %s in labels or explicitly preserve_text" % uid)
        prepared.append((h, state, points, text))
    if (set(anchors) | set(stair_members)) - {s["uuid"] for _,s,_,_ in prepared}:
        raise ValueError("validation: fixed_points must be inside section targets")
    if (set(labels) | preserved) - texts or set(labels) & preserved:
        raise ValueError("validation: labels/preserve_text must be distinct text objects inside section targets")
    # Preflight every member before any write; the enclosing script is one native undo transaction.
    changed = []
    for h, state, points, text in prepared:
        got = section_set_points(h, state, points)
        if text is not None:
            vs.SetText(h, text)
            vs.ResetObject(h)
            if vs.GetText(h) != text:
                raise ValueError("readback_mismatch: level annotation " + state["uuid"])
        changed.append(state["uuid"])
    for h in reversed(rows):
        if vs.GetTypeN(h) == 11:
            vs.ResetObject(h)
    done.append({"op": "section_heights", "objects": len(changed), "uuids": changed,
                 "labels": len(labels), "bands": item["bands"], "verified": True})


def reshape_geometry(item, handles, done):
    prepared = []
    for h in handles:
        state = section_state(h)
        t = state["type_id"]
        points = item.get("points") or [item["start"], item["end"]]
        if t in (5, 21):
            if not item.get("points") or len(points) != len(state["points"]) or state.get("holes"):
                raise ValueError("validation: polygon reshape requires every existing vertex and no holes")
        elif t not in (2, 63, 68) or len(points) != 2:
            raise ValueError("validation: reshape supports lines, linear dimensions, polygons and polylines")
        if t == 63 and state["dimension_class"] not in (0, 1):
            raise ValueError("validation: reshape requires a linear dimension")
        if vs.GetObjectVariableBoolean(h, 700):
            raise ValueError("validation: object is locked")
        section_plane(h)
        if t in (2, 63, 68) and sum((a-b)**2 for a,b in zip(points[0],points[1])) < 1e-12:
            raise ValueError("validation: reshape endpoints must be distinct")
        prepared.append((h, state, points))
    for h, state, points in prepared:
        got = section_set_points(h, state, points)
        done.append({"op": "reshape", "uuid": state["uuid"], "vertices": len(points), "verified": True,
                     "dimension_text": got.get("dimension_text")})
