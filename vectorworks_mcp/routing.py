"""vw_edit: values whose meaning depends on the target's type.

A flat ``{"op":"set","target":..,"width":1800}`` means the door width on a door, the wall thickness on a wall and
nothing on a rectangle; ``{"op":"parametric","target":..,"parameters":{"sill_height":900}}`` takes its plug-in from
the target. ``builders.compile_edit`` collects these as ``plan.typed``; vw_edit reads the targets' records and
``route_typed`` turns them into native ``update_parametric`` ops or vetted script ops.
"""

from __future__ import annotations

from typing import Any

from . import coerce as C
from .builders import (FRIENDLY_PARAMS, SPACE_NAME_KEYS, EditPlan, _semantic, parametric_parameters,
                       pio_parameters)
from .errors import VWError, validation

WALL_KEYS = {"thickness": "thickness", "width": "thickness", "wall_thickness": "thickness", "height": "height",
             "wall_height": "height", "style": "style", "wall_style": "style", "style_name": "style"}
TEXT_KEYS = {"text", "content", "string", "value", "label"}
_SETTABLE = {
    "wall": ("thickness, height, style (wall style name)", {"thickness": 300, "height": 2800}),
    "text": ("text (content); font, text_size, text_style and align are style keys", {"text": "Kitchen", "text_size": 14}),
    "Door": ("width, height, open_angle, swing (In|Out), hinge (Left|Right), configuration, or any Door parameter id",
             {"width": 1000, "open_angle": 45}),
    "Window": ("width, height, sill (sill height), configuration, or any Window parameter id", {"width": 1500, "sill": 900}),
    "Space": ("name (displayed room name), room_id, height, or any Space parameter id", {"name": "Kitchen"}),
    "rect": ("size changes go through op='scale' (sx/sy) or op='move'; attributes: fill_color, pen_color, line_weight, "
             "fill, line_style", {"op": "scale", "sx": 1.5, "sy": 1.0}),
    "oval": ("size changes go through op='scale' (sx/sy); attributes: fill_color, pen_color, fill, line_style",
             {"op": "scale", "sx": 1.5, "sy": 1.0}),
    "line": ("endpoints change with op='reshape' (start, end); attributes: pen_color, line_weight, line_style, arrows",
             {"op": "reshape", "start": [0, 0], "end": [2000, 0]}),
}
_DEFAULT_HINT = ("attributes: name, class, fill_color, pen_color, line_weight, opacity, fill, line_style (geometry: op "
                 "move/rotate/scale/reshape)", {"fill_color": "#C0C0C0"})


_BUILTIN_PLUGINS = {"door": "Door", "window": "Window", "space": "Space"}


def kind_of(rec: dict) -> str:
    plugin = rec.get("plugin_name") or _BUILTIN_PLUGINS.get(str(rec.get("type") or ""))
    return str(plugin) if plugin else str(rec.get("type") or "object")


def plugins_needed(plan: EditPlan, info: dict[str, dict]) -> set[str]:
    out: set[str] = set()
    for entry in plan.typed:
        for t in entry["targets"]:
            rec = info.get(t)
            if rec and (rec.get("plugin_name") or rec.get("type") in _BUILTIN_PLUGINS):
                out.add(kind_of(rec))
    return out


def _error(field: str, keys: list[str], kind: str, target: str) -> VWError:
    hint, example = _SETTABLE.get(kind, _DEFAULT_HINT)
    example = {"op": "set", "target": target, **example} if "op" not in example else {**example, "target": target}
    return validation(f"{field}: {', '.join(map(str, keys))} cannot be set on a {kind}. Settable on a {kind}: {hint}.",
                      f"{field}.{keys[0]}", example)


def route_typed(plan: EditPlan, info: dict[str, dict], scale: float, schemas: dict[str, dict],
                descriptors: dict[str, str]) -> None:
    for entry in plan.typed:
        field = entry["field"]
        groups: dict[str, list[str]] = {}
        for t in entry["targets"]:
            rec = info.get(t)
            if rec is None:
                raise VWError("stale_reference", f"{field}: target {t} was not found.", field=f"{field}.target",
                              next_step="Re-read the objects with vw_read(what='query') and use the returned uuid.")
            groups.setdefault(kind_of(rec), []).append(t)
        for kind, targets in groups.items():
            values = C.parse_json_maybe(entry.get("values"))
            values = dict(values) if isinstance(values, dict) else values
            if entry["kind"] == "parametric":
                if not _is_plugin(kind, schemas):
                    raise validation(f"{field}: the target is a {kind}, not a plug-in object; op='parametric' works on "
                                     "doors, windows, spaces, stairs, columns ...", f"{field}.target",
                                     {"op": "set", "target": targets[0], **_SETTABLE.get(kind, _DEFAULT_HINT)[1]})
                _parametric(plan, field, targets, kind, values, scale, schemas, descriptors)
                continue
            if kind == "Space":
                name = entry.get("name")
                for key in list(values):
                    if _semantic(key) in SPACE_NAME_KEYS:
                        name = str(values.pop(key))
                if name is not None:
                    _drop_native_name(plan, targets)
                    plan.script_ops.append({"op": "space_name", "targets": targets,
                                            "name": C.text(name, f"{field}.name", required=True, max_len=255)})
                    plan.needs_script = True
                if values:
                    _parametric(plan, field, targets, kind, values, scale, schemas, descriptors)
                continue
            if not values:
                continue
            if kind in {"wall", "round_wall"}:
                bad = [k for k in values if str(k).lower() not in WALL_KEYS]
                if bad:
                    raise _error(field, bad, "wall", targets[0])
                item: dict[str, Any] = {"op": "wall", "targets": targets}
                for key, value in values.items():
                    canonical = WALL_KEYS[str(key).lower()]
                    if canonical == "style":
                        item["style"] = C.text(value, f"{field}.{key}", required=True, max_len=255)
                        continue
                    item[canonical] = C.length(value, f"{field}.{key}", scale)
                    if item[canonical] <= 0:
                        raise validation(f"{field}.{key} must be > 0", f"{field}.{key}", 200)
                plan.script_ops.append(item)
                plan.needs_script = True
            elif kind == "text":
                bad = [k for k in values if str(k).lower() not in TEXT_KEYS]
                if bad:
                    raise _error(field, bad, kind, targets[0])
                plan.script_ops.append({"op": "text", "targets": targets,
                                        "text": C.text(next(iter(values.values())), f"{field}.text", required=True)})
                plan.needs_script = True
            elif _is_plugin(kind, schemas):
                _parametric(plan, field, targets, kind, values, scale, schemas, descriptors)
            else:
                raise _error(field, list(values), kind, targets[0])


def _is_plugin(kind: str, schemas: dict) -> bool:
    return kind in schemas or kind in FRIENDLY_PARAMS or kind[:1].isupper()


def _drop_native_name(plan: EditPlan, targets: list[str]) -> None:
    """A Space's `name` is its displayed room name (set by script with the object name), so the plain native
    object rename is dropped for those targets."""
    for op in plan.ops:
        if op.get("type") == "set_properties":
            for edit in op["params"]["edits"]:
                if edit["ref"] in targets:
                    edit["properties"].pop("name", None)
            op["params"]["edits"] = [e for e in op["params"]["edits"] if e["properties"]]
    plan.ops = [op for op in plan.ops if op.get("type") != "set_properties" or op["params"]["edits"]]


def _parametric(plan: EditPlan, field: str, targets: list[str], plugin: str, values: Any, scale: float,
                schemas: dict[str, dict], descriptors: dict[str, str]) -> None:
    schema = schemas.get(plugin) or {}
    if schema.get("script_only"):
        if isinstance(values, dict):
            table = FRIENDLY_PARAMS.get(plugin, {})
            values = {table.get(_semantic(k), k): v for k, v in values.items()}
        plan.script_ops.append({"op": "pio", "targets": targets, "plugin": plugin, "scale_mm": scale,
                                "parameters": pio_parameters(values, f"{field}.parameters")})
        plan.needs_script = True
        plan.warnings.append(f"{plugin} has no native schema provider; parameters were set through its "
                             "parametric record (script).")
        return
    parameters = parametric_parameters(values, f"{field}.parameters", scale, schema or None,
                                       warnings=plan.warnings, plugin=plugin)
    if not parameters:
        raise validation(f"{field} has nothing settable on the {plugin} (see warnings: read-only parameters are "
                         "computed by Vectorworks)", f"{field}.parameters", {"width": 1000})
    fingerprint = descriptors.get(plugin) or "pending"
    plan.ops.extend({"type": "update_parametric", "params": {"target": t, "plugin_name": plugin,
                                                             "descriptor_fingerprint": fingerprint,
                                                             "parameters": parameters}} for t in targets)
