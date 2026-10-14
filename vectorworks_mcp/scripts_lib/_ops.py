# --- script edit operations (included by edit_engine and massing; needs _style, _pio, _geometry) ---
ORDER_POSITIONS = ("front", "back", "forward", "backward")


def _layer_handle(name):
    h = vs.GetObject(name)
    if _nil(h) or vs.GetTypeN(h) != 31:
        names = []
        layer = vs.FLayer()
        while not _nil(layer) and len(names) < 500:
            names.append(vs.GetLName(layer))
            layer = vs.NextLayer(layer)
        close = did_you_mean(name, names)
        raise ValueError("not_found: design layer not found: %r%s" % (
            name, (" - did you mean: " + ", ".join(repr(n) for n in close) + "?") if close else ""))
    return h


def _in_wall(h):
    parent = vs.GetParent(h)
    return not _nil(parent) and vs.GetTypeN(parent) in (68, 89)


def order_object(h, position):
    """Stacking order inside the layer (first object of a layer = drawn at the back)."""
    if position not in ORDER_POSITIONS:
        raise ValueError("validation: order position must be one of front, back, forward, backward")
    if _in_wall(h):
        raise ValueError("validation: %s is inside a wall (doors/windows follow their wall); order the wall "
                         "instead" % uuid_of(h))
    if position in ("front", "forward"):
        vs.HMoveForward(h, position == "front")
    else:
        vs.HMoveBackward(h, position == "back")
    if position == "front" and not _nil(vs.NextObj(h)):
        raise ValueError("readback_mismatch: %s is not the front-most object of its layer" % uuid_of(h))
    if position == "back" and not _nil(vs.PrevObj(h)):
        raise ValueError("readback_mismatch: %s is not the back-most object of its layer" % uuid_of(h))
    return position


def _set_wall_height(h, want_mm):
    """Live (VW 2024): vs.SetWallHeights / vs.HWallHeight do not change a wall; the top bound offset does
    (SetWallOverallHeights, bound type 0). On walls made with corner heights (native create) the offset ADDS to the
    corner height, so the first readback gives that base and the offset is set once more."""
    vs.SetWallOverallHeights(h, 0, 0, "", 0.0, 0, 0, "", U(want_mm))
    vs.ResetObject(h)
    st, sb, et, eb = vs.GetWallHeight(h)
    got = MM(st - sb)
    if abs(got - want_mm) > 0.5:
        base = got - want_mm
        vs.SetWallOverallHeights(h, 0, 0, "", 0.0, 0, 0, "", U(want_mm - base))
        vs.ResetObject(h)
        st, sb, et, eb = vs.GetWallHeight(h)
        got = MM(st - sb)
    if abs(got - want_mm) > 0.5:
        _fail("wall height", want_mm, got)
    return round(got, 1)


def run_op(item, done):
    op = item["op"]
    handles = [resolve(t) for t in item.get("targets", [])]
    if op == "mirror":
        a, b = item["axis_start"], item["axis_end"]
        for h in handles:
            # vs.MirrorN(h, dup: BOOLEAN, p1: POINT, p2: POINT, preserveMatrix: BOOLEAN) -> HANDLE
            # (there is no vs.HMirror; preserveMatrix keeps symbol/PIO matrices correct)
            new = vs.MirrorN(h, bool(item.get("copy")), (float(a[0]), float(a[1])), (float(b[0]), float(b[1])), True)
            done.append({"op": op, "uuid": uuid_of(new if (item.get("copy") and not _nil(new)) else h)})
    elif op == "layer":
        target = _layer_handle(item["layer"])
        for h in handles:
            if _in_wall(h):
                continue  # doors/windows live inside their wall and move with it
            vs.SetParent(h, target)
            vs.ResetObject(h)
            got = vs.GetLName(vs.GetLayer(h))
            if got != item["layer"]:
                raise ValueError("readback_mismatch: %s is on layer %r, not %r" % (uuid_of(h), got, item["layer"]))
            done.append({"op": op, "uuid": uuid_of(h), "layer": got})
    elif op == "order":
        for h in handles:
            done.append({"op": op, "uuid": uuid_of(h), "position": order_object(h, item["position"])})
    elif op == "group":
        # vs.Group() groups the selection on the active layer only.
        vs.DSelectAll()
        for h in handles:
            vs.SetSelect(h)
        vs.Group()
        g = vs.LSActLayer()
        if not _nil(g) and item.get("name"):
            vs.SetName(g, item["name"])
        vs.DSelectAll()
        done.append({"op": op, "uuid": "" if _nil(g) else uuid_of(g), "members": len(handles)})
    elif op == "ungroup":
        for h in handles:
            vs.DSelectAll()
            vs.SetSelect(h)
            vs.Ungroup()
            vs.DSelectAll()
            done.append({"op": op, "ungrouped": True})
    elif op == "record":
        rec = item["record"]
        if _nil(vs.GetObject(rec)):
            for field in (item["fields"] or {"Value": ""}):
                vs.NewField(rec, str(field), "", 4, 0)
        for h in handles:
            if item.get("detach"):
                vs.DelRecord(h, rec)
                done.append({"op": op, "uuid": uuid_of(h), "detached": rec})
                continue
            vs.SetRecord(h, rec)
            for field, value in item["fields"].items():
                vs.SetRField(h, rec, field, "" if value is None else str(value))
            vs.ResetObject(h)
            done.append({"op": op, "uuid": uuid_of(h), "record": rec, "fields": list(item["fields"].keys())})
    elif op == "pio":
        for h in handles:
            expect = pio_set(h, item["parameters"], item.get("scale_mm", 1.0))
            done.append({"op": op, "uuid": uuid_of(h), "set": sorted(expect), "expect": expect,
                         "via": "parametric_record"})
    elif op == "style":
        for h in handles:
            done.append({"op": op, "uuid": uuid_of(h), "set": apply_style(h, item["style"])})
    elif op == "wall":
        # {"thickness": mm, "height": mm, "style": name}
        for h in handles:
            if vs.GetTypeN(h) not in (68, 89):
                raise ValueError("validation: %s is not a wall (type_id %s)" % (uuid_of(h), vs.GetTypeN(h)))
            got = {}
            if item.get("style"):
                find_resource(127, item["style"])  # imported from the library when the document lacks it
                if not vs.SetWallStyle(h, item["style"], 0.0, 0.0):
                    raise ValueError("native_rejected: Vectorworks refused wall style %r" % item["style"])
                got["style"] = vs.GetWallStyle(h)
            if item.get("thickness") is not None:
                if not vs.SetWallThickness(h, U(item["thickness"])):
                    raise ValueError("native_rejected: Vectorworks refused the wall thickness (styled walls take "
                                     "their thickness from the wall style)")
                ok, th = vs.GetWallThickness(h)
                if not ok or abs(MM(th) - float(item["thickness"])) > 0.5:
                    _fail("wall thickness", item["thickness"], MM(th))
                got["thickness"] = round(MM(th), 1)
            if item.get("height") is not None:
                got["height"] = _set_wall_height(h, float(item["height"]))
            vs.ResetObject(h)
            done.append({"op": op, "uuid": uuid_of(h), "set": got})
    elif op == "text":
        for h in handles:
            if vs.GetTypeN(h) != 10:
                raise ValueError("validation: %s is not a text object (type_id %s)" % (uuid_of(h), vs.GetTypeN(h)))
            vs.SetText(h, item["text"])
            vs.ResetObject(h)
            if (vs.GetText(h) or "") != item["text"]:
                _fail("text", item["text"], vs.GetText(h))
            done.append({"op": op, "uuid": uuid_of(h), "text": item["text"][:80]})
    elif op == "space_name":
        for h in handles:
            if plugin_of(h) != "Space":
                raise ValueError("validation: %s is not a Space" % uuid_of(h))
            set_space_label(h, item["name"])
            if item.get("object_name", True):
                other = vs.GetObject(item["name"])
                if _nil(other) or other == h:
                    vs.SetName(h, item["name"])
            done.append({"op": op, "uuid": uuid_of(h), "room_name": space_label(h), "expect_label": item["name"]})
    else:
        raise ValueError("unsupported script edit op: " + op)
