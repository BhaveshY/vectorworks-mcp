# ARGS = {"path": str, "at": [x, y]}
at = ARGS.get("at") or [0, 0]
h = vs.ImportImageFile(str(ARGS["path"]), (float(at[0]), float(at[1])))
if _nil(h):
    raise ValueError("Vectorworks could not import the image: " + ARGS["path"])
result = {"imported": ARGS["path"], "object": record_of(h)}
