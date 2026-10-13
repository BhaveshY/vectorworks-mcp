# --- plug-in object parameters through the parametric record (included by edit_engine, massing, pio_schema) ---
# Fallback for plug-ins whose native SDK schema provider is missing (e.g. Column2): the parameters are the fields of
# the plug-in's parametric record. vs.GetFldType codes (verified live, VW 2024): 1 integer, 2 boolean, 3 real,
# 4 text, 7 dimension (document length units), 8 popup, 14 class; 10/11 are coordinate X/Y.
PIO_LENGTH = (7, 10, 11)
PIO_TYPE_NAMES = {1: "integer", 2: "boolean", 3: "real", 4: "text", 7: "length", 8: "popup", 10: "coordinate_x",
                  11: "coordinate_y", 14: "class"}


def pio_record(h):
    rec = vs.GetParametricRecord(h)
    if _nil(rec):
        raise ValueError("validation: %s is not a plug-in object" % uuid_of(h))
    return rec, vs.GetName(rec)


def _pio_value(ftype, raw):
    if ftype in PIO_LENGTH and isinstance(raw, str) and raw:
        try:
            return MM(float(raw))
        except ValueError:
            return raw
    if ftype in (1, 3) and isinstance(raw, str) and raw:
        try:
            return float(raw) if ftype == 3 else int(float(raw))
        except ValueError:
            return raw
    if ftype == 2:
        return str(raw).strip().lower() == "true"
    return raw


def pio_fields(rec, name, inst=None):
    rows = []
    for i in range(1, vs.NumFields(rec) + 1):
        field = vs.GetFldName(rec, i)
        if field.startswith("__"):
            continue  # internal bookkeeping fields of the plug-in
        ftype = vs.GetFldType(rec, i)
        raw = vs.GetRField(inst if inst is not None else rec, name, field)
        rows.append({"id": field, "type": PIO_TYPE_NAMES.get(ftype, str(ftype)), "field_style": ftype,
                     "value": _pio_value(ftype, raw)})
    return rows


def pio_set(h, params, scale_mm):
    """params: [{"id", "value", "mm": bool}] -> SetRField (+ unit conversion for length fields), ResetObject, readback."""
    rec, name = pio_record(h)
    types = {vs.GetFldName(rec, i): vs.GetFldType(rec, i) for i in range(1, vs.NumFields(rec) + 1)}
    want = {}
    for p in params:
        field = p["id"]
        if field not in types:
            lowered = {k.lower().replace(" ", ""): k for k in types}
            field = lowered.get(str(field).lower().replace(" ", ""), field)
        if field not in types:
            raise ValueError("not_found: %s has no parameter %r (parameters: %s)" % (
                name, p["id"], ", ".join(k for k in types if not k.startswith("__"))[:600]))
        ftype, value = types[field], p["value"]
        if ftype in PIO_LENGTH and not isinstance(value, bool):
            try:
                mm = float(value) if p.get("mm") else float(value) * float(scale_mm)
            except (TypeError, ValueError):
                raise ValueError("validation: %s.%s is a length; got %r" % (name, field, value))
            text, want[field] = str(U(mm)), ("length", mm)
        elif ftype == 2:
            flag = value if isinstance(value, bool) else str(value).strip().lower() in ("true", "1", "yes", "on")
            text, want[field] = ("True" if flag else "False"), ("bool", flag)
        elif ftype in (1, 3):
            text, want[field] = str(value), ("number", float(value))
        else:
            text, want[field] = str(value), ("text", str(value))
        vs.SetRField(h, name, field, text)
    vs.ResetObject(h)
    bad = pio_mismatches(h, name, want)
    if bad:
        raise ValueError("readback_mismatch: %s" % "; ".join(bad))
    return {k: list(v) for k, v in want.items()}


def pio_mismatches(h, name, want):
    """Compare record fields with {field: (kind, expected)}. NOTE (live, VW 2024): the plug-in regenerates only
    after the script ends, so the host re-checks with pio_schema {"verify": ...} afterwards."""
    bad = []
    for field, (kind, expected) in want.items():
        got = vs.GetRField(h, name, field)
        try:
            ok = (abs(MM(float(got)) - expected) <= 0.5 if kind == "length" else
                  (got.strip().lower() == "true") == expected if kind == "bool" else
                  abs(float(got) - expected) <= 1e-6 if kind == "number" else got == expected)
        except ValueError:
            ok = False
        if not ok:
            shown = ("%.0f mm" % MM(float(got))) if kind == "length" and got.replace(".", "").replace("e", "").replace("-", "").replace("+", "").isdigit() else repr(got)
            bad.append("%s.%s is %s, requested %r" % (name, field, shown, expected))
    return bad
