# ARGS = {"action": "list" | "restore" | "create" | "delete", "name": str}
action = ARGS.get("action", "list")
name = ARGS.get("name", "")
if action == "list":
    list_id, count = vs.BuildResourceList(103, 0, "")
    result = {"saved_views": [{"name": vs.GetNameFromResourceList(list_id, i)} for i in range(1, count + 1)]}
elif action == "restore":
    vs.RestoreSavedView(name)
    result = {"restored": name}
elif action == "create":
    vs.SaveView(name)
    result = {"created": name}
elif action == "delete":
    h = vs.GetObject(name)
    if _nil(h):
        raise ValueError("not_found: saved view not found: " + name)
    vs.DelObject(h)
    result = {"deleted": name}
else:
    raise ValueError("saved_views action must be list, restore, create or delete")
