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


_MM_PER_UNIT = []


def U(mm):
    """Millimetres -> document length units (vs.* coordinates are document units)."""
    if not _MM_PER_UNIT:
        try:
            upi = float(vs.GetUnits()[3])
        except Exception:
            upi = 25.4
        _MM_PER_UNIT.append(25.4 / upi if upi > 0 else 1.0)
    return float(mm) / _MM_PER_UNIT[0]


def MM(units):
    U(0)
    return float(units) * _MM_PER_UNIT[0]


def PT(p):
    return (U(p[0]), U(p[1]))


def record_of(h):
    out = {"uuid": uuid_of(h), "type_id": vs.GetTypeN(h), "name": vs.GetName(h)}
    try:
        out["class"] = vs.GetClass(h)
    except Exception:
        pass
    try:
        out["layer"] = vs.GetLName(vs.GetLayer(h))  # vs.GetName returns '' for layers
    except Exception:
        pass
    return out
