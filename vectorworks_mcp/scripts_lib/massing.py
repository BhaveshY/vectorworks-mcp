# ARGS = {"objects": [{"id", "kind": "extrude", "points": [[x, y] mm], "bottom": mm, "top": mm, "style": {}}
#                    | {"id", "kind": "column", "plugin": "Column2", "at": [x, y] mm, "rotation": deg,
#                       "lengths": {field: mm}, "fields": {field: text}, "style": {}}],
#         "restyle": [{"id", "uuid", "style": {}}],  (styles for objects the native create just made)
#         "post": [edit op, ...],  (_ops.run_op: layer moves, stacking order; targets may be "$<id>" of this call)
#         "openings": [{"id", "kind": "door"|"window", "plugin": "Door"|"Window", "wall": uuid, "at": [x, y] mm,
#                       "width": mm, "flip": bool, "right": bool, "style": {}}]}
#   Doors/windows: vs.AddSymToWall makes a real wall break (live VW 2024: the native create does not, and Vectorworks
#   deletes such openings when the wall regenerates). Offset = projection of `at` onto the wall axis from the wall
#   start, clamped into the wall. Sizes/parameters are set by the host afterwards (native update_parametric).
created = []
styled = []
PIO_EXPECT = []


def _wall_breaks(wall):
    r = vs.GetNumOfWallBreaks(wall)
    return int(r[1] if isinstance(r, (tuple, list)) else r)


def insert_opening(item):
    label = "%s %r" % (item["kind"], item["id"])
    wall = resolve("uuid:" + item["wall"])
    if vs.GetTypeN(wall) != 68:
        raise ValueError("validation: %s: the host %s is not a straight wall (type %d); doors/windows go into "
                         "straight walls" % (label, item["wall"], vs.GetTypeN(wall)))
    p1, p2 = vs.GetSegPt1(wall), vs.GetSegPt2(wall)
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    length = (dx * dx + dy * dy) ** 0.5
    if length <= 0:
        raise ValueError("validation: %s: the host wall has no length" % label)
    at = PT(item["at"])
    along = ((at[0] - p1[0]) * dx + (at[1] - p1[1]) * dy) / length
    across = abs((at[0] - p1[0]) * dy - (at[1] - p1[1]) * dx) / length
    half = U(item.get("width") or 0.0) / 2.0
    lo, hi = min(half, length / 2.0), max(length - half, length / 2.0)
    offset = min(max(along, lo), hi)
    if abs(offset - along) > U(1.0):
        WARNINGS.append("%s: moved %.0f mm along its wall to fit inside it (wall length %.0f mm, opening %.0f mm)"
                        % (label, MM(abs(offset - along)), MM(length), float(item.get("width") or 0.0)))
    ok, thick = vs.GetWallThickness(wall)
    if across > max(U(50.0), thick if ok else 0.0):
        WARNINGS.append("%s: at is %.0f mm off the wall axis; inserted at the nearest point of the wall"
                        % (label, MM(across)))
    before = _wall_breaks(wall)
    vs.AddSymToWall(wall, offset, 0, bool(item.get("flip")), bool(item.get("right")), item["plugin"])
    h = vs.LNewObj()
    if _nil(h) or vs.GetTypeN(h) != 86:
        raise ValueError("native_rejected: Vectorworks did not insert the %s into wall %s (is the %s plug-in "
                         "installed?)" % (label, item["wall"], item["plugin"]))
    if uuid_of(vs.GetParent(h)) != uuid_of(wall):
        raise ValueError("readback_mismatch: %s was not inserted into wall %s" % (label, item["wall"]))
    if _wall_breaks(wall) <= before:
        raise ValueError("readback_mismatch: %s has no wall break record in wall %s" % (label, item["wall"]))
    if item.get("style"):
        apply_style(h, item["style"])
    return {"id": item["id"], "uuid": uuid_of(h), "type": item["kind"], "wall": item["wall"],
            "offset": round(MM(offset), 1)}


for item in ARGS.get("openings") or []:
    created.append(insert_opening(item))

for item in ARGS.get("objects") or []:
    kind = item["kind"]
    if kind == "extrude":
        vs.BeginPoly()
        for p in item["points"]:
            vs.AddPoint(PT(p))
        vs.EndPoly()
        profile = vs.LNewObj()
        if _nil(profile):
            raise ValueError("native_rejected: the extrude profile polygon was not created")
        # BeginPoly/EndPoly makes an OPEN polygon: extruded it gives side walls without top/bottom faces (live 2024)
        vs.SetPolyClosed(profile, True)
        h = vs.HExtrude(profile, U(item["bottom"]), U(item["top"]))
        if _nil(h) or vs.GetTypeN(h) != 24:
            raise ValueError("native_rejected: Vectorworks did not create the extrude")
        if not vs.IsPolyClosed(vs.FInGroup(h)):
            raise ValueError("readback_mismatch: the extrude profile is not closed")
        height = MM(vs.Get3DInfo(h)[2])
        want = float(item["top"]) - float(item["bottom"])
        if abs(height - want) > 0.5:
            raise ValueError("readback_mismatch: extrude height is %.1f mm, expected %.1f mm" % (height, want))
    elif kind == "pio":
        h = vs.CreateCustomObjectN(item["plugin"], PT(item["at"]), float(item.get("rotation") or 0.0), False)
        if _nil(h):
            raise ValueError("not_found: the %s plug-in object could not be created (not installed?)" % item["plugin"])
        if item.get("parameters"):
            PIO_EXPECT.append({"uuid": uuid_of(h), "expect": pio_set(h, item["parameters"], item.get("scale_mm", 1.0))})
        else:
            vs.ResetObject(h)
    elif kind == "column":
        h = vs.CreateCustomObjectN(item["plugin"], PT(item["at"]), float(item.get("rotation") or 0.0), False)
        if _nil(h):
            raise ValueError("not_found: the %s plug-in object could not be created (not installed?)" % item["plugin"])
        rec = vs.GetName(vs.GetParametricRecord(h))
        values = {k: str(U(v)) for k, v in (item.get("lengths") or {}).items()}
        values.update({k: str(v) for k, v in (item.get("fields") or {}).items()})
        for field, value in values.items():
            vs.SetRField(h, rec, field, value)
        vs.ResetObject(h)
        for field in (item.get("lengths") or {}):
            got = vs.GetRField(h, rec, field)
            try:
                if abs(MM(float(got)) - float(item["lengths"][field])) > 0.5:
                    raise ValueError("readback_mismatch: %s.%s is %s" % (item["plugin"], field, got))
            except ValueError as exc:
                if "readback_mismatch" in str(exc):
                    raise
                raise ValueError("readback_mismatch: %s.%s is %r (not a length)" % (item["plugin"], field, got))
    else:
        raise ValueError("unsupported massing kind: " + kind)
    if item.get("style"):
        apply_style(h, item["style"])
    created.append({"id": item["id"], "uuid": uuid_of(h),
                    "type": item.get("type_label") or ("parametric" if kind == "pio" else kind)})

for item in ARGS.get("restyle") or []:
    styled.append({"id": item["id"], "uuid": item["uuid"], "set": apply_style(resolve("uuid:" + item["uuid"]), item["style"])})

post_done = []
if ARGS.get("post"):
    local = {row["id"]: row["uuid"] for row in created}
    for item in ARGS["post"]:
        item = dict(item, targets=["uuid:" + local[t[1:]] if t.startswith("$") and t[1:] in local else t
                                   for t in item.get("targets", [])])
        run_op(item, post_done)

result = {"created": created, "styled": styled}
if post_done:
    result["post"] = post_done
if PIO_EXPECT:
    result["pio_expect"] = PIO_EXPECT
if IMPORTED:
    result["imported_resources"] = IMPORTED
if WARNINGS:
    result["warnings"] = WARNINGS
