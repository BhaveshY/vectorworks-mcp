# ARGS = {"target": "uuid:..."}
h = resolve(ARGS["target"])
out = record_of(h)
try:
    p1, p2 = vs.GetBBox(h)
    out["bounds"] = {"top_left": list(p1), "bottom_right": list(p2)}
except Exception:
    pass
recs = []
for i in range(1, vs.NumRecords(h) + 1):
    rh = vs.GetRecord(h, i)
    rname = vs.GetName(rh)
    fields = {}
    for f in range(1, vs.NumFields(rh) + 1):
        fname = vs.GetFldName(rh, f)
        fields[fname] = vs.GetRField(h, rname, fname)
    recs.append({"record": rname, "fields": fields})
out["records"] = recs
if vs.GetTypeN(h) == 86:
    info = vs.GetCustomObjectInfo(h)
    if info and info[0]:
        out["plugin_name"] = info[1]
result = out
