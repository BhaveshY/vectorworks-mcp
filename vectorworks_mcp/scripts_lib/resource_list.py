# ARGS = {"resource_type": int, "query": str, "offset": int, "limit": int, "library": bool}
rtype = int(ARGS["resource_type"])
_l, doc_names = _res_names(rtype, 0, "")
items = [{"name": n, "source": "document"} for n in doc_names]
lib = RES_LIBRARY.get(rtype) if ARGS.get("library", True) else None
if lib:
    _l, lib_names = _res_names(rtype, lib[0], lib[1])
    seen = set(doc_names)
    items += [{"name": n, "source": "library"} for n in lib_names if n not in seen]
query = (ARGS.get("query") or "").lower()
if query:
    items = [i for i in items if query in i["name"].lower()]
offset = int(ARGS.get("offset", 0))
limit = int(ARGS.get("limit", 100))
result = {"items": items[offset:offset + limit], "total": len(items),
          "next_cursor": str(offset + limit) if offset + limit < len(items) else None}
