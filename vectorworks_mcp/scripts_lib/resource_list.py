# ARGS = {"resource_type": int | "resource_types": [int], "query": str, "offset": int, "limit": int, "library": bool}
# query = case-insensitive name substring. Several types (search across kinds) add "type" to every item.
types = [int(t) for t in ARGS.get("resource_types") or [ARGS["resource_type"]]]
query = (ARGS.get("query") or "").lower()
items = []
for rtype in types:
    _l, doc_names = _res_names(rtype, 0, "")
    rows = [{"name": n, "source": "document"} for n in doc_names]
    lib = RES_LIBRARY.get(rtype) if ARGS.get("library", True) else None
    if lib:
        _l, lib_names = _res_names(rtype, lib[0], lib[1])
        seen = set(doc_names)
        rows += [{"name": n, "source": "library"} for n in lib_names if n not in seen]
    if query:
        rows = [i for i in rows if query in i["name"].lower()]
    if len(types) > 1:
        for row in rows:
            row["type"] = RES_LABEL.get(rtype, str(rtype)).replace(" ", "_")
    items += rows
offset = int(ARGS.get("offset", 0))
limit = int(ARGS.get("limit", 100))
result = {"items": items[offset:offset + limit], "total": len(items),
          "next_cursor": str(offset + limit) if offset + limit < len(items) else None}
