# ARGS = {"action": "list" | "restore" | "create" | "delete", "name": str}
action = ARGS.get("action", "list")
name = ARGS.get("name", "")
if action == "list":
    list_id, count = vs.BuildResourceList(103, 0, "")
    result = {"saved_views": [{"name": vs.GetNameFromResourceList(list_id, i)} for i in range(1, count + 1)]}
elif action == "restore":
    vs.VRestore(name)
    result = {"restored": name}
elif action == "create":
    vs.SaveSheet(name, True, True, True)
    result = {"created": name}
elif action == "delete":
    list_id, count = vs.BuildResourceList(103, 0, "")
    if name not in [vs.GetNameFromResourceList(list_id, i) for i in range(1, count + 1)]:
        raise ValueError("not_found: saved view not found: " + name)
    vs.VDelete(name)
    result = {"deleted": name}
else:
    raise ValueError("saved_views action must be list, restore, create or delete")
