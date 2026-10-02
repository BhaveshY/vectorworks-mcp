# ARGS = {"resource_type": int, "query": str, "offset": int, "limit": int}
list_id, count = vs.BuildResourceList(int(ARGS["resource_type"]), 0, "")
query = (ARGS.get("query") or "").lower()
names = []
for i in range(1, count + 1):
    n = vs.GetNameFromResourceList(list_id, i)
    if not query or query in n.lower():
        names.append(n)
offset = int(ARGS.get("offset", 0))
limit = int(ARGS.get("limit", 100))
result = {"items": [{"name": n} for n in names[offset:offset + limit]], "total": len(names),
          "next_cursor": str(offset + limit) if offset + limit < len(names) else None}
