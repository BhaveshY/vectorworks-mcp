# ARGS = {"target": "uuid:..." | "targets": [...], "detail": bool}
# Compact by default: per-type geometry + layer/class/plug-in name and the names of attached records.
# detail=true adds every record's fields (a door/window parametric record alone is ~10 KB).
def _detail_of(h):
    out = record_of(h)
    out.update(geometry(h))
    names = []
    recs = []
    for i in range(1, vs.NumRecords(h) + 1):
        rh = vs.GetRecord(h, i)
        rname = vs.GetName(rh)
        names.append(rname)
        if ARGS.get("detail"):
            fields = {}
            for f in range(1, vs.NumFields(rh) + 1):
                fname = vs.GetFldName(rh, f)
                fields[fname] = vs.GetRField(h, rname, fname)
            recs.append({"record": rname, "fields": fields})
    if ARGS.get("detail"):
        out["records"] = recs
    elif names:
        out["record_names"] = names
    return out


if ARGS.get("all_labels"):
    # every Space of the document in one pass: {uuid: room_name} (one script instead of one per query page)
    labels = {}

    def _space(h):
        labels[vs.GetObjectUuid(h)] = space_label(h)
    vs.ForEachObject(_space, "(PON='Space')")
    result = {"labels": labels}
elif ARGS.get("labels") is not None:
    # displayed Space room names for a query page: {uuid: room_name}
    labels = {}
    for u in ARGS["labels"]:
        h = vs.GetObjectByUuid(u)
        if not _nil(h) and plugin_of(h) == "Space":
            labels[u] = space_label(h)
    result = {"labels": labels}
elif ARGS.get("targets") is not None:
    rows = []
    for ref in ARGS["targets"]:
        try:
            rows.append(_detail_of(resolve(ref)))
        except ValueError:
            rows.append({"uuid": str(ref).replace("uuid:", ""), "missing": True})
    result = {"items": rows}
else:
    result = _detail_of(resolve(ARGS["target"]))
