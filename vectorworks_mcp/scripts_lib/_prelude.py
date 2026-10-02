# --- vectorworks_mcp snippet prelude (runs inside Vectorworks; `vs` is pre-imported) ---
def _nil(h):
    if h is None:
        return True
    try:
        return vs.GetTypeN(h) == 0
    except Exception:
        return True


def resolve(ref):
    """'uuid:<u>' | 'name:<n>' | '<uuid>' -> handle (raises on failure)."""
    ref = str(ref)
    if ref.startswith("uuid:"):
        h = vs.GetObjectByUuid(ref[5:])
    elif ref.startswith("name:"):
        h = vs.GetObject(ref[5:])
    elif len(ref) >= 32:
        h = vs.GetObjectByUuid(ref)
    else:
        h = vs.GetObject(ref)
    if _nil(h):
        raise ValueError("stale_reference: object not found: " + ref)
    return h


def uuid_of(h):
    try:
        return vs.GetObjectUuid(h)
    except Exception:
        return ""


def record_of(h):
    out = {"uuid": uuid_of(h), "type_id": vs.GetTypeN(h), "name": vs.GetName(h)}
    try:
        out["class"] = vs.GetClass(h)
    except Exception:
        pass
    try:
        out["layer"] = vs.GetName(vs.GetLayer(h))
    except Exception:
        pass
    return out
