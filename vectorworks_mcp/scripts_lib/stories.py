# ARGS = {"action": "list" | "create" | "set", "name", "suffix", "elevation_mm", "new_name"}
action = ARGS.get("action", "list")
if action == "list":
    stories = []
    seen = set()
    h = vs.FLayer()
    while not _nil(h):
        st = vs.GetStoryOfLayer(h)
        if not _nil(st):
            sname = vs.GetName(st)
            if sname not in seen:
                seen.add(sname)
                stories.append({"name": sname, "suffix": vs.GetStorySuffix(st), "elevation": vs.GetStoryElevation(st)})
        h = vs.NextLayer(h)
    result = {"stories": stories}
elif action == "create":
    ok = vs.CreateStory(ARGS["name"], ARGS.get("suffix") or ARGS["name"][:3])
    st = vs.GetObject(ARGS["name"])
    if "elevation_mm" in ARGS and not _nil(st):
        vs.SetStoryElevation(st, float(ARGS["elevation_mm"]))
    result = {"created": ARGS["name"], "ok": bool(ok)}
elif action == "set":
    st = vs.GetObject(ARGS["name"])
    if _nil(st):
        raise ValueError("not_found: story not found: " + ARGS["name"])
    if "elevation_mm" in ARGS:
        vs.SetStoryElevation(st, float(ARGS["elevation_mm"]))
    if ARGS.get("new_name"):
        vs.SetName(st, ARGS["new_name"])
    result = {"updated": ARGS.get("new_name") or ARGS["name"]}
elif action == "assign":
    # Put design layers on a story (needed e.g. for IFC export): a layer joins a story through a layer level type.
    st = vs.GetObject(ARGS["name"])
    if _nil(st):
        raise ValueError("not_found: story not found: " + ARGS["name"])
    level = ARGS.get("level_type") or "Floor"
    vs.CreateLayerLevelType(level)  # False when it already exists
    done, failed = [], []
    for lname in ARGS.get("layers") or []:
        lay = vs.GetLayerByName(lname)
        if _nil(lay):
            raise ValueError("not_found: design layer not found: " + lname)
        if not _nil(vs.GetStoryOfLayer(lay)) and vs.GetName(vs.GetStoryOfLayer(lay)) == ARGS["name"]:
            done.append(lname)
            continue
        vs.SetLayerLevelType(lay, level)
        vs.AssociateLayerWithStory(lay, st)
        story_now = vs.GetStoryOfLayer(lay)
        (done if not _nil(story_now) and vs.GetName(story_now) == ARGS["name"] else failed).append(lname)
    result = {"story": ARGS["name"], "layers": done, "failed": failed}
    if failed:
        result["note"] = "a layer can be on one story only, with a level type unique within that story"
else:
    raise ValueError("stories action must be list, create, set or assign")
