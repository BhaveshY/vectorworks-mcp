# ARGS = {"path": str (source .vwx), "resource_type": int, "name": str}
list_id, count = vs.BuildResourceListN(int(ARGS["resource_type"]), ARGS["path"])
index = 0
for i in range(1, count + 1):
    if vs.GetNameFromResourceList(list_id, i) == ARGS["name"]:
        index = i
        break
if not index:
    raise ValueError("not_found: resource not found in source file: " + ARGS["name"])
h = vs.ImportResToCurFileN(list_id, index, lambda s: 1)
result = {"imported": ARGS["name"], "ok": not _nil(h)}
