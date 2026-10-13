# ARGS = {"objects": [{"id", "kind": "extrude", "points": [[x, y] mm], "bottom": mm, "top": mm, "style": {}}
#                    | {"id", "kind": "column", "plugin": "Column2", "at": [x, y] mm, "rotation": deg,
#                       "lengths": {field: mm}, "fields": {field: text}, "style": {}}],
#         "restyle": [{"id", "uuid", "style": {}}]}  (styles for objects the native create just made)
created = []
styled = []
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
        h = vs.HExtrude(profile, U(item["bottom"]), U(item["top"]))
        if _nil(h) or vs.GetTypeN(h) != 24:
            raise ValueError("native_rejected: Vectorworks did not create the extrude")
        height = MM(vs.Get3DInfo(h)[2])
        want = float(item["top"]) - float(item["bottom"])
        if abs(height - want) > 0.5:
            raise ValueError("readback_mismatch: extrude height is %.1f mm, expected %.1f mm" % (height, want))
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
    created.append({"id": item["id"], "uuid": uuid_of(h), "type": kind})

for item in ARGS.get("restyle") or []:
    styled.append({"id": item["id"], "uuid": item["uuid"], "set": apply_style(resolve("uuid:" + item["uuid"]), item["style"])})

result = {"created": created, "styled": styled}
if IMPORTED:
    result["imported_resources"] = IMPORTED
