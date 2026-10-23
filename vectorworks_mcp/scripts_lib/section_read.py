rows = section_walk([resolve(ARGS["target"])])
offset, limit = ARGS["offset"], ARGS["limit"]
items = []
vertices = 0
for h in rows[offset:offset+limit]:
    state = section_state(h)
    count = len(state.get("points", []))
    if items and ARGS.get("detail") and vertices + count > 20000:
        break
    vertices += count
    state["vertex_count"] = count
    if not ARGS.get("detail") and count > 50:
        for key in ("points", "vertex_types", "radii"):
            if key in state:
                state[key] = state[key][:50]
        state["vertices_truncated"] = True
    items.append(state)
result = {"items": items, "total": len(rows), "revision": section_revision(rows)}
