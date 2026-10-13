# ARGS = {"plugin": name, "target": ref|None} -> parameter list of a plug-in from its parametric record
# (instance values when target is given, else the record format defaults of the document).
if ARGS.get("verify"):
    # after the plug-in regenerated (next event): which requested values did it keep?
    problems = []
    for item in ARGS["verify"]:
        h = resolve("uuid:" + item["uuid"])
        rec, name = pio_record(h)
        problems += ["%s: %s" % (item["uuid"], m) for m in pio_mismatches(h, name, item["expect"])]
    result = {"problems": problems}
elif ARGS.get("target"):
    h = resolve(ARGS["target"])
    rec, name = pio_record(h)
    params = pio_fields(rec, name, h)
    source = "instance"
else:
    name = ARGS["plugin"]
    rec = vs.GetObject(name)
    if _nil(rec) or vs.GetTypeN(rec) != 47:
        raise ValueError("not_found: no %s object (and so no parameter record) in this document yet; create one "
                         "(vw_create type='parametric' plugin=%r) or pass target=<uuid of one>" % (name, name))
    params = pio_fields(rec, name)
    source = "record_format_defaults"
if not ARGS.get("verify"):
    result = {"plugin_name": name, "parameters": params, "source": source, "schema_provider": "parametric_record"}
