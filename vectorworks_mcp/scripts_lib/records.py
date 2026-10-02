# ARGS = {"action": "formats" | "object", "target": "uuid:..."}
def _fields_of(h, rname, rh):
    fields = {}
    for f in range(1, vs.NumFields(rh) + 1):
        fname = vs.GetFldName(rh, f)
        fields[fname] = vs.GetRField(h, rname, fname)
    return fields


if ARGS.get("action") == "object":
    h = resolve(ARGS["target"])
    recs = []
    for i in range(1, vs.NumRecords(h) + 1):
        rh = vs.GetRecord(h, i)
        rname = vs.GetName(rh)
        recs.append({"record": rname, "fields": _fields_of(h, rname, rh)})
    result = {"object": record_of(h), "records": recs}
else:
    formats = []
    list_id, count = vs.BuildResourceList(47, 0, "")
    for i in range(1, count + 1):
        name = vs.GetNameFromResourceList(list_id, i)
        rh = vs.GetObject(name)
        names = []
        if not _nil(rh):
            for f in range(1, vs.NumFields(rh) + 1):
                names.append(vs.GetFldName(rh, f))
        formats.append({"name": name, "fields": names})
    result = {"formats": formats}
