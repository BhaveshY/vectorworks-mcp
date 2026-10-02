# ARGS = {"ops": [{"op": ..., "targets": [...], ...}]}
done = []


def _layer_handle(name):
    h = vs.GetObject(name)
    if _nil(h) or vs.GetTypeN(h) != 31:
        raise ValueError("not_found: design layer not found: " + name)
    return h


for item in ARGS["ops"]:
    op = item["op"]
    handles = [resolve(t) for t in item.get("targets", [])]
    if op == "mirror":
        a, b = item["axis_start"], item["axis_end"]
        for h in handles:
            new = vs.HMirror(h, bool(item.get("copy")), (a[0], a[1]), (b[0], b[1]))
            done.append({"op": op, "uuid": uuid_of(new if (item.get("copy") and not _nil(new)) else h)})
    elif op == "layer":
        target = _layer_handle(item["layer"])
        for h in handles:
            vs.SetParent(h, target)
            vs.ResetObject(h)
            done.append({"op": op, "uuid": uuid_of(h), "layer": item["layer"]})
    elif op == "group":
        vs.DSelectAll()
        for h in handles:
            vs.SetSelect(h)
        vs.Group()
        g = vs.LSActLayer()
        if not _nil(g) and item.get("name"):
            vs.SetName(g, item["name"])
        vs.DSelectAll()
        done.append({"op": op, "uuid": "" if _nil(g) else uuid_of(g), "members": len(handles)})
    elif op == "ungroup":
        for h in handles:
            vs.DSelectAll()
            vs.SetSelect(h)
            vs.Ungroup()
            vs.DSelectAll()
            done.append({"op": op, "ungrouped": True})
    elif op == "record":
        rec = item["record"]
        if _nil(vs.GetObject(rec)):
            for field in (item["fields"] or {"Value": ""}):
                vs.NewField(rec, field, "", 4, 0)
        for h in handles:
            if item.get("detach"):
                vs.DelRecord(h, rec)
                done.append({"op": op, "uuid": uuid_of(h), "detached": rec})
                continue
            vs.SetRecord(h, rec)
            for field, value in item["fields"].items():
                vs.SetRField(h, rec, field, value)
            vs.ResetObject(h)
            done.append({"op": op, "uuid": uuid_of(h), "record": rec, "fields": list(item["fields"].keys())})
    else:
        raise ValueError("unsupported script edit op: " + op)

result = {"applied": len(done), "receipts": done}
