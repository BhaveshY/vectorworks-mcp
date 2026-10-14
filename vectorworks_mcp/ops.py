"""vw_apply grammar (0.7 ``operations`` plans) - ported from the 0.7 host.

The normalisation (including unit scaling to mm, where built-in defaults stay
in mm) was verified correct in 0.7 and is kept nearly verbatim. Changes:
numbers may be numeric strings, booleans may be "true"/"false", and ``op`` is
accepted as an alias of ``type`` (weak models).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Optional

CoordinateUnits = str
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")



NATIVE_PHASE_FOUR_CREATE_OBJECT_TYPES = {
    "arc",
    "box",
    "circle",
    "dimension",
    "door",
    "line",
    "linear_dimension",
    "oval",
    "parametric",
    "polygon",
    "polyline",
    "rect",
    "rectangle",
    "roof",
    "slab",
    "space",
    "symbol",
    "text",
    "wall",
    "window",
}


PROPERTY_NAME_VALUES = {
    "name",
    "class",
    "fillColor",
    "penColor",
    "fillPattern",
    "lineWeight",
    "opacity",
}
MAX_PROPERTY_VALUE_CHARS = 1024


_PRIMITIVE_COORD_KEYS = ("x1", "y1", "x2", "y2")
_PRIMITIVE_ALLOWED_KEYS = {
    "role",
    "object_type",
    "type",
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "start_x",
    "start_y",
    "end_x",
    "end_y",
    "radius",
    "start_angle",
    "sweep_angle",
    "points",
    "closed",
    "height",
    "sill_height",
    "thickness",
    "elevation",
    "bearing_height",
    "bearing_inset",
    "vertical_miter",
    "miter_type",
    "generate_gable_walls",
    "slope",
    "overhang",
    "style_name",
    "plugin_name",
    "definition_name",
    "descriptor_fingerprint",
    "wall_uuid",
    "require_wall_host",
    "parameters",
    "elevation",
    "bearing_height",
    "slope",
    "overhang",
    "bearing_inset",
    "vertical_miter",
    "miter_type",
    "generate_gable_walls",
    "room_id",
    "text",
    "width",
    "rotation",
    "text_size",
    "size",
    "fixed_size",
    "wrap",
    "offset",
    "dimension_offset",
    "text_offset",
    "direction_x",
    "direction_y",
    "dimension_type",
    "name",
    "room_id",
    "class_name",
    "join_walls",
}




def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        try:
            result = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _is_real_number(value: Any) -> bool:
    return _to_float(value) is not None



def _normalize_property_value(property_name: str, value: Any) -> tuple[str | None, str | None]:
    if value is None or isinstance(value, (dict, list, tuple)):
        return None, "property value must be a scalar"

    value_text = str(value)
    if len(value_text) > MAX_PROPERTY_VALUE_CHARS:
        return None, f"property value is limited to {MAX_PROPERTY_VALUE_CHARS} characters"

    if property_name == "class":
        class_name = value_text.strip()
        if not class_name:
            return None, "class value is required"
        return class_name, None

    if property_name in {"fillPattern", "lineWeight", "opacity"}:
        trimmed = value_text.strip()
        if not re.fullmatch(r"[+-]?\d+", trimmed):
            return None, f"{property_name} must be an integer"
        parsed = int(trimmed, 10)
        max_value = 100 if property_name == "opacity" else 32767
        if parsed < 0 or parsed > max_value:
            return None, f"{property_name} must be between 0 and {max_value}"
        return str(parsed), None

    if property_name in {"fillColor", "penColor"}:
        parts = [part.strip() for part in value_text.split(",")]
        if len(parts) != 3 or any(not re.fullmatch(r"\d+", part) for part in parts):
            return None, "color must be r,g,b with components in 0..65535"
        components = [int(part, 10) for part in parts]
        if any(component < 0 or component > 65535 for component in components):
            return None, "color must be r,g,b with components in 0..65535"
        return ",".join(str(component) for component in components), None

    return value_text, None


def _coerce_number(
    item: dict[str, Any],
    key: str,
    *,
    default: Optional[float] = None,
    required: bool = False,
    min_value: Optional[float] = None,
    label: str = "item",
) -> float:
    if key not in item or item.get(key) is None:
        if required:
            raise ValueError(f"{label}.{key} is required")
        if default is None:
            raise ValueError(f"{label}.{key} has no default")
        return float(default)
    value = item[key]
    if not _is_real_number(value):
        raise ValueError(f"{label}.{key} must be a finite number")
    result = float(_to_float(value))
    if min_value is not None and result < min_value:
        raise ValueError(f"{label}.{key} must be >= {min_value}")
    return result


def _coerce_number_any(
    item: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: Optional[float] = None,
    required: bool = False,
    min_value: Optional[float] = None,
    label: str = "item",
) -> float:
    for key in keys:
        if key in item and item.get(key) is not None:
            return _coerce_number(item, key, required=True, min_value=min_value, label=label)
    if required:
        raise ValueError(f"{label}.{keys[0]} is required")
    if default is None:
        raise ValueError(f"{label}.{keys[0]} has no default")
    if min_value is not None and default < min_value:
        raise ValueError(f"{label}.{keys[0]} must be >= {min_value}")
    return float(default)


def _coerce_positive_number(
    item: dict[str, Any],
    key: str,
    *,
    default: Optional[float] = None,
    label: str = "item",
) -> float:
    result = _coerce_number(item, key, default=default, required=default is None, min_value=0, label=label)
    if result <= 0:
        raise ValueError(f"{label}.{key} must be > 0")
    return result


def _coerce_positive_number_any(
    item: dict[str, Any],
    keys: tuple[str, ...],
    *,
    default: Optional[float] = None,
    label: str = "item",
) -> float:
    result = _coerce_number_any(item, keys, default=default, required=default is None, min_value=0, label=label)
    if result <= 0:
        raise ValueError(f"{label}.{keys[0]} must be > 0")
    return result


def _coerce_bool(item: dict[str, Any], key: str, default: bool = False, *, label: str = "item") -> bool:
    value = item.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no", "1", "0"}:
        return value.strip().lower() in {"true", "yes", "1"}
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError(f"{label}.{key} must be a boolean")


def _coerce_int(
    item: dict[str, Any],
    key: str,
    *,
    default: int,
    min_value: int,
    max_value: int,
    label: str = "item",
) -> int:
    value = item.get(key, default)
    as_float = _to_float(value)
    if as_float is not None and as_float == int(as_float):
        value = int(as_float)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label}.{key} must be an integer")
    if value < min_value or value > max_value:
        raise ValueError(f"{label}.{key} must be between {min_value} and {max_value}")
    return value


def _optional_text(item: dict[str, Any], key: str, default: str = "") -> str:
    value = item.get(key, default)
    if value is None:
        return default
    return str(value)


_GEOMETRY_LENGTH_KEYS = {
    "x",
    "y",
    "x1",
    "y1",
    "x2",
    "y2",
    "start_x",
    "start_y",
    "end_x",
    "end_y",
    "dx",
    "dy",
    "pivot_x",
    "pivot_y",
    "radius",
    "width",
    "height",
    "thickness",
    "elevation",
    "sill_height",
    "overhang",
    "bearing_inset",
    "vertical_miter",
    "offset",
    "text_offset",
}


_GEOMETRY_INPUT_ALIASES = {
    "x1": ("x1", "x"),
    "y1": ("y1", "y"),
    "start_x": ("start_x", "x1"),
    "start_y": ("start_y", "y1"),
    "end_x": ("end_x", "x2"),
    "end_y": ("end_y", "y2"),
    "elevation": ("elevation", "bearing_height"),
    "offset": ("offset", "dimension_offset"),
}


def _coordinate_scale_to_mm(units: CoordinateUnits) -> float:
    return {
        "mm": 1.0,
        "cm": 10.0,
        "m": 1000.0,
        "in": 25.4,
        "ft": 304.8,
    }[units]


def _scale_geometry_params(params: dict[str, Any], scale_to_mm: float) -> dict[str, Any]:
    """Convert only typed geometry fields; plugin parameters keep their declared schema units."""
    if scale_to_mm == 1.0:
        return params
    scaled = dict(params)
    for key in _GEOMETRY_LENGTH_KEYS:
        value = scaled.get(key)
        if _is_real_number(value):
            scaled[key] = float(value) * scale_to_mm
    points = scaled.get("points")
    if isinstance(points, list):
        scaled["points"] = [
            [float(point[0]) * scale_to_mm, float(point[1]) * scale_to_mm]
            for point in points
        ]
    return scaled



def _normalise_create_primitive(
    raw: dict[str, Any],
    *,
    label: str,
    default_class_name: str = "",
    name_prefix: str = "",
    scale_to_mm: float = 1.0,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")

    unknown = sorted(set(raw) - _PRIMITIVE_ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"{label} has unsupported key(s): {', '.join(unknown)}")

    object_type = str(raw.get("object_type", raw.get("type", "")) or "").strip().lower()
    requested_object_type = object_type
    if object_type == "rectangle" or object_type == "box":
        object_type = "rect"
    if object_type == "polyline":
        object_type = "polygon"
    if object_type == "dimension":
        object_type = "linear_dimension"
    if object_type not in NATIVE_PHASE_FOUR_CREATE_OBJECT_TYPES:
        raise ValueError(
            f"{label}.object_type must be one of: {', '.join(sorted(NATIVE_PHASE_FOUR_CREATE_OBJECT_TYPES))}"
        )

    params: dict[str, Any] = {"object_type": object_type}
    if object_type in {"rect", "oval", "line"}:
        for key in _PRIMITIVE_COORD_KEYS:
            params[key] = _coerce_number(raw, key, required=True, label=label)
        if object_type == "line" and params["x1"] == params["x2"] and params["y1"] == params["y2"]:
            raise ValueError(f"{label} line endpoints must not be identical")
    elif object_type == "circle":
        params["x1"] = _coerce_number(raw, "x1", required=True, label=label)
        params["y1"] = _coerce_number(raw, "y1", required=True, label=label)
        params["radius"] = _coerce_positive_number(raw, "radius", label=label)
    elif object_type == "arc":
        params["x1"] = _coerce_number(raw, "x1", required=True, label=label)
        params["y1"] = _coerce_number(raw, "y1", required=True, label=label)
        params["radius"] = _coerce_positive_number(raw, "radius", label=label)
        params["start_angle"] = _coerce_number(raw, "start_angle", default=0, label=label)
        params["sweep_angle"] = _coerce_number(raw, "sweep_angle", default=90, label=label)
    elif object_type in {"polygon", "slab", "roof", "space"}:
        raw_points = raw.get("points")
        if not isinstance(raw_points, list) or len(raw_points) > 1000:
            raise ValueError(f"{label}.points must be a list containing at most 1000 [x, y] points")
        closed = _coerce_bool(raw, "closed", requested_object_type != "polyline", label=label)
        if object_type != "polygon" and not closed:
            raise ValueError(f"{label}.{object_type} requires a closed point boundary")
        minimum = 3 if closed else 2
        if len(raw_points) < minimum:
            raise ValueError(f"{label}.points requires at least {minimum} points")
        points: list[list[float]] = []
        for point_index, point in enumerate(raw_points):
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError(f"{label}.points[{point_index}] must be [x, y]")
            try:
                x_value = float(point[0])
                y_value = float(point[1])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{label}.points[{point_index}] must contain finite numbers") from exc
            if not math.isfinite(x_value) or not math.isfinite(y_value):
                raise ValueError(f"{label}.points[{point_index}] must contain finite numbers")
            points.append([x_value, y_value])
        params["points"] = points
        params["closed"] = closed
        if object_type == "slab":
            params["thickness"] = _coerce_positive_number(raw, "thickness", default=200, label=label)
            params["elevation"] = _coerce_number(raw, "elevation", default=0, label=label)
            style_name = _optional_text(raw, "style_name", "")
            if style_name:
                params["style_name"] = style_name
        elif object_type == "roof":
            params["thickness"] = _coerce_positive_number(raw, "thickness", default=200, label=label)
            params["slope"] = _coerce_positive_number(raw, "slope", default=30, label=label)
            if params["slope"] > 89:
                raise ValueError(f"{label}.slope must be <= 89")
            params["overhang"] = _coerce_number(raw, "overhang", default=500, min_value=0, label=label)
            params["elevation"] = _coerce_number_any(
                raw,
                ("elevation", "bearing_height"),
                default=0,
                label=label,
            )
            params["bearing_inset"] = _coerce_number(
                raw,
                "bearing_inset",
                default=0,
                min_value=0,
                label=label,
            )
            params["vertical_miter"] = _coerce_number(
                raw,
                "vertical_miter",
                default=0,
                min_value=0,
                label=label,
            )
            params["miter_type"] = _coerce_int(
                raw,
                "miter_type",
                default=1,
                min_value=1,
                max_value=4,
                label=label,
            )
            params["generate_gable_walls"] = _coerce_bool(
                raw,
                "generate_gable_walls",
                False,
                label=label,
            )
        elif object_type == "space":
            params["height"] = _coerce_positive_number(raw, "height", default=3000, label=label)
            room_id = _optional_text(raw, "room_id", "")
            if room_id:
                params["room_id"] = room_id
    elif object_type == "wall":
        params["start_x"] = _coerce_number_any(raw, ("start_x", "x1"), required=True, label=label)
        params["start_y"] = _coerce_number_any(raw, ("start_y", "y1"), required=True, label=label)
        params["end_x"] = _coerce_number_any(raw, ("end_x", "x2"), required=True, label=label)
        params["end_y"] = _coerce_number_any(raw, ("end_y", "y2"), required=True, label=label)
        if params["start_x"] == params["end_x"] and params["start_y"] == params["end_y"]:
            raise ValueError(f"{label} wall endpoints must not be identical")
        params["height"] = _coerce_positive_number(raw, "height", default=3000, label=label)
        params["thickness"] = _coerce_positive_number(raw, "thickness", default=200, label=label)
        style_name = _optional_text(raw, "style_name", "")
        if style_name:
            params["style_name"] = style_name
        if "join_walls" in raw:
            params["join_walls"] = _coerce_bool(raw, "join_walls", True, label=label)
    elif object_type in {"parametric", "door", "window"}:
        raw_plugin_name = raw.get("plugin_name")
        raw_descriptor_fingerprint = raw.get("descriptor_fingerprint")
        if not isinstance(raw_plugin_name, str):
            raise ValueError(f"{label}.plugin_name must be a non-empty string")
        if not isinstance(raw_descriptor_fingerprint, str):
            raise ValueError(f"{label}.descriptor_fingerprint must be a non-empty string")
        plugin_name = raw_plugin_name.strip()
        descriptor_fingerprint = raw_descriptor_fingerprint.strip()
        if not plugin_name:
            raise ValueError(f"{label}.plugin_name is required; discover it with vw_catalog parametric_schemas")
        if not descriptor_fingerprint:
            raise ValueError(f"{label}.descriptor_fingerprint is required from live schema discovery")
        if object_type in {"door", "window"}:
            expected_plugin_name = object_type.title()
            if plugin_name != expected_plugin_name:
                raise ValueError(
                    f"{label}.plugin_name must be exactly {expected_plugin_name!r} for {object_type}"
                )
        params["plugin_name"] = plugin_name
        params["descriptor_fingerprint"] = descriptor_fingerprint
        params["x1"] = _coerce_number_any(
            raw,
            ("x", "x1"),
            required=object_type in {"door", "window"},
            default=0,
            label=label,
        )
        params["y1"] = _coerce_number_any(
            raw,
            ("y", "y1"),
            required=object_type in {"door", "window"},
            default=0,
            label=label,
        )
        params["rotation"] = _coerce_number(raw, "rotation", default=0, label=label)
        if object_type in {"door", "window"} and "require_wall_host" in raw:
            if not _coerce_bool(raw, "require_wall_host", True, label=label):
                raise ValueError(f"{label}.require_wall_host cannot be false for {object_type}")
        require_wall_host = (
            True
            if object_type in {"door", "window"}
            else _coerce_bool(raw, "require_wall_host", False, label=label)
        )
        raw_wall_uuid = raw.get("wall_uuid", "")
        if raw_wall_uuid is not None and not isinstance(raw_wall_uuid, str):
            raise ValueError(f"{label}.wall_uuid must be a non-empty string")
        wall_uuid = str(raw_wall_uuid or "").strip()
        if require_wall_host and not wall_uuid:
            raise ValueError(f"{label}.wall_uuid is required for hosted {object_type} placement")
        params["require_wall_host"] = require_wall_host
        if wall_uuid:
            params["wall_uuid"] = wall_uuid
        if object_type in {"door", "window"}:
            params["width"] = _coerce_positive_number(
                raw,
                "width",
                label=label,
            )
            params["height"] = _coerce_positive_number(
                raw,
                "height",
                label=label,
            )
            if object_type == "window":
                params["sill_height"] = _coerce_number(
                    raw,
                    "sill_height",
                    required=True,
                    min_value=0,
                    label=label,
                )
            elif "sill_height" in raw:
                raise ValueError(f"{label}.sill_height is supported only for window")
        raw_parameters = raw.get("parameters", [])
        if not isinstance(raw_parameters, list) or len(raw_parameters) > 256:
            raise ValueError(f"{label}.parameters must be a list with at most 256 entries")
        params["parameter_count"] = len(raw_parameters)
        seen_parameters: set[str] = set()
        for parameter_index, parameter in enumerate(raw_parameters, start=1):
            parameter_label = f"{label}.parameters[{parameter_index}]"
            if not isinstance(parameter, dict):
                raise ValueError(f"{parameter_label} must be an object")
            unknown_parameter_keys = sorted(set(parameter) - {"id", "type", "value"})
            if unknown_parameter_keys:
                raise ValueError(f"{parameter_label} has unsupported key(s): {', '.join(unknown_parameter_keys)}")
            parameter_id = _optional_text(parameter, "id", "").strip()
            parameter_type = _optional_text(parameter, "type", "").strip().lower()
            if not parameter_id or parameter_id in seen_parameters:
                raise ValueError(f"{parameter_label}.id must be a unique universal parameter name")
            semantic_parameter_id = re.sub(r"[^a-z0-9]", "", parameter_id.lower())
            dedicated_semantics = {"width", "height"}
            if object_type == "window":
                dedicated_semantics.update({"elevation", "sillheight"})
            if object_type in {"door", "window"} and semantic_parameter_id in dedicated_semantics:
                raise ValueError(
                    f"{parameter_label}.id duplicates the dedicated {object_type} field {parameter_id!r}"
                )
            if parameter_type not in {"integer", "boolean", "real", "string"}:
                raise ValueError(f"{parameter_label}.type must be integer, boolean, real, or string")
            value = parameter.get("value")
            prefix = f"parameter_{parameter_index}_"
            params[prefix + "name"] = parameter_id
            params[prefix + "type"] = parameter_type
            if parameter_type == "integer" and isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
                value = int(value.strip())
            if parameter_type == "boolean" and isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                value = value.strip().lower() == "true"
            if parameter_type == "real" and isinstance(value, str) and _to_float(value) is not None:
                value = _to_float(value)
            if parameter_type == "integer":
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValueError(f"{parameter_label}.value must be an integer")
                params[prefix + "integer"] = value
            elif parameter_type == "boolean":
                if not isinstance(value, bool):
                    raise ValueError(f"{parameter_label}.value must be a boolean")
                params[prefix + "boolean"] = value
            elif parameter_type == "real":
                if not _is_real_number(value):
                    raise ValueError(f"{parameter_label}.value must be a finite number")
                params[prefix + "real"] = float(value)
            else:
                if not isinstance(value, str):
                    raise ValueError(f"{parameter_label}.value must be a string")
                params[prefix + "string"] = value
            seen_parameters.add(parameter_id)
    elif object_type == "symbol":
        definition_name = _optional_text(raw, "definition_name", "").strip()
        if not definition_name:
            raise ValueError(f"{label}.definition_name is required; discover it with vw_catalog(action='symbols')")
        params["definition_name"] = definition_name
        params["x1"] = _coerce_number_any(raw, ("x", "x1"), default=0, label=label)
        params["y1"] = _coerce_number_any(raw, ("y", "y1"), default=0, label=label)
        params["rotation"] = _coerce_number(raw, "rotation", default=0, label=label)
    elif object_type == "text":
        text = _optional_text(raw, "text", "")
        if not text:
            raise ValueError(f"{label}.text is required")
        params["text"] = text
        params["x1"] = _coerce_number_any(raw, ("x", "x1"), default=0, label=label)
        params["y1"] = _coerce_number_any(raw, ("y", "y1"), default=0, label=label)
        params["width"] = _coerce_number(raw, "width", default=0, min_value=0, label=label)
        params["rotation"] = _coerce_number(raw, "rotation", default=0, label=label)
        params["text_size"] = _coerce_number_any(raw, ("text_size", "size"), default=0, min_value=0, label=label)
        params["fixed_size"] = _coerce_bool(raw, "fixed_size", False, label=label)
        params["wrap"] = _coerce_bool(raw, "wrap", params["width"] > 0, label=label)
    elif object_type == "linear_dimension":
        params["start_x"] = _coerce_number_any(raw, ("start_x", "x1"), required=True, label=label)
        params["start_y"] = _coerce_number_any(raw, ("start_y", "y1"), required=True, label=label)
        params["end_x"] = _coerce_number_any(raw, ("end_x", "x2"), required=True, label=label)
        params["end_y"] = _coerce_number_any(raw, ("end_y", "y2"), required=True, label=label)
        if params["start_x"] == params["end_x"] and params["start_y"] == params["end_y"]:
            raise ValueError(f"{label} linear_dimension endpoints must not be identical")
        params["offset"] = _coerce_number_any(raw, ("offset", "dimension_offset"), default=300, label=label)
        params["text_offset"] = _coerce_number(raw, "text_offset", default=0, label=label)
        params["direction_x"] = _coerce_number(raw, "direction_x", default=0, label=label)
        params["direction_y"] = _coerce_number(raw, "direction_y", default=0, label=label)
        params["dimension_type"] = _coerce_int(raw, "dimension_type", default=1, min_value=0, max_value=2, label=label)

    name = _optional_text(raw, "name", "")
    if name_prefix:
        name = f"{name_prefix} {name}".strip() if name else name_prefix
    if name:
        params["name"] = name

    class_name = _optional_text(raw, "class_name", default_class_name)
    if class_name:
        params["class_name"] = class_name

    role = _optional_text(raw, "role", "primitive")
    if role:
        params["role"] = role
    scaled = _scale_geometry_params(params, scale_to_mm)
    if scale_to_mm != 1.0:
        # Built-in defaults (wall height 3000, slab thickness 200, ...) are
        # native millimetres. Only caller-supplied lengths are in
        # coordinate_units; without this a metre plan got 3000 m walls.
        for key in _GEOMETRY_LENGTH_KEYS:
            if key in params and not any(name in raw for name in _GEOMETRY_INPUT_ALIASES.get(key, (key,))):
                scaled[key] = params[key]
    return scaled


def _normalise_operation_property_edits(value: Any, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 100:
        raise ValueError(f"{label} must be a list containing 1 to 100 edits")
    normalised: list[dict[str, Any]] = []
    for index, edit in enumerate(value, start=1):
        edit_label = f"{label}[{index}]"
        if not isinstance(edit, dict):
            raise ValueError(f"{edit_label} must be an object")
        unknown = sorted(set(edit) - {"ref", "expected_type", "expected_layer", "expected_name", "properties"})
        if unknown:
            raise ValueError(f"{edit_label} has unsupported key(s): {', '.join(unknown)}")
        ref = str(edit.get("ref", "") or "").strip()
        if not ref or not ref.startswith(("uuid:", "name:", "handle:", "$")):
            raise ValueError(f"{edit_label}.ref must start with uuid:, name:, handle:, or $ for a prior operation_id")
        properties = edit.get("properties")
        if not isinstance(properties, dict) or not properties or len(properties) > 20:
            raise ValueError(f"{edit_label}.properties must contain 1 to 20 supported properties")
        normalised_properties: dict[str, str] = {}
        for property_name, value in properties.items():
            property_name = str(property_name)
            if property_name not in PROPERTY_NAME_VALUES:
                raise ValueError(
                    f"{edit_label}.properties.{property_name} is unsupported; "
                    f"use one of: {', '.join(sorted(PROPERTY_NAME_VALUES))}"
                )
            normalised_value, value_error = _normalize_property_value(property_name, value)
            if value_error is not None:
                raise ValueError(f"{edit_label}.properties.{property_name}: {value_error}")
            normalised_properties[property_name] = str(normalised_value)
        item: dict[str, Any] = {"ref": ref, "properties": normalised_properties}
        for guard in ("expected_type", "expected_layer", "expected_name"):
            guard_value = str(edit.get(guard, "") or "")
            if guard_value:
                item[guard] = guard_value
        normalised.append(item)
    return normalised


def _normalise_atomic_target(value: Any, *, label: str) -> str:
    target = str(value or "").strip()
    if not target.startswith(("uuid:", "name:", "handle:", "$")):
        raise ValueError(f"{label} must start with uuid:, name:, handle:, or $ for a prior operation_id")
    if target == "$" or target.endswith(":"):
        raise ValueError(f"{label} has an empty reference value")
    if len(target) > 512:
        raise ValueError(f"{label} is limited to 512 characters")
    return target


_DOCUMENTATION_OPERATION_TYPES = frozenset(
    {
        "create_sheet_layer",
        "update_sheet_layer",
        "delete_sheet_layer",
        "create_viewport",
        "update_viewport",
        "delete_viewport",
        "create_viewport_annotation",
        "update_viewport_annotation",
        "delete_viewport_annotation",
    }
)
_DOCUMENTATION_CREATE_TYPES = frozenset(
    {"create_sheet_layer", "create_viewport", "create_viewport_annotation"}
)
_DOCUMENTATION_WIRE_OPS = {
    "create_sheet_layer": "sheet_layer.create",
    "update_sheet_layer": "sheet_layer.update",
    "delete_sheet_layer": "sheet_layer.delete",
    "create_viewport": "viewport.create",
    "update_viewport": "viewport.update",
    "delete_viewport": "viewport.delete",
    "create_viewport_annotation": "viewport_annotation.create",
    "update_viewport_annotation": "viewport_annotation.update",
    "delete_viewport_annotation": "viewport_annotation.delete",
}


def _normalise_documentation_ref(value: Any, *, label: str, allow_local: bool = True) -> str:
    ref = str(value or "").strip()
    valid = ref.startswith("uuid:") or (allow_local and ref.startswith("$") and len(ref) > 1)
    if not valid or ref == "uuid:" or len(ref) > 512:
        suffix = " or $operation_id" if allow_local else ""
        raise ValueError(f"{label} must be an exact uuid: reference{suffix}")
    return ref


def _normalise_target_binding(value: Any, *, require_dirty: bool, allow_unsaved: bool = False) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("target_binding must be an object")
    allowed = {
        "file_path",
        "file_name",
        "document_fingerprint",
        "document_generation",
        "bridge_session_id",
        "dirty",
        "active_layer_uuid",
        "active_layer_name",
    }
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError(f"target_binding has unsupported key(s): {', '.join(unknown)}")
    result: dict[str, Any] = {}
    for key in (
        "file_path",
        "document_fingerprint",
        "bridge_session_id",
        "active_layer_uuid",
        "active_layer_name",
    ):
        item = value.get(key)
        if allow_unsaved and key == "file_path" and item == "":
            result[key] = ""
            continue
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"target_binding.{key} must be a non-empty string")
        result[key] = item.strip()
    if "file_name" in value:
        file_name = value["file_name"]
        if not isinstance(file_name, str) or not file_name.strip():
            raise ValueError("target_binding.file_name must be a non-empty string")
        result["file_name"] = file_name.strip()
    generation = value.get("document_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ValueError("target_binding.document_generation must be a positive integer")
    result["document_generation"] = generation
    if "dirty" in value:
        if not isinstance(value["dirty"], bool):
            raise ValueError("target_binding.dirty must be a boolean")
        result["dirty"] = value["dirty"]
    elif require_dirty:
        raise ValueError("target_binding.dirty is required for documentation writes")
    return result


def _target_binding_wire_params(binding: dict[str, Any]) -> dict[str, Any]:
    params = {
        "expected_file_path": binding["file_path"],
        "expected_document_fingerprint": binding["document_fingerprint"],
        "expected_document_generation": binding["document_generation"],
        "expected_bridge_session_id": binding["bridge_session_id"],
        "expected_active_layer_uuid": binding["active_layer_uuid"],
        "expected_active_layer_name": binding["active_layer_name"],
    }
    if "dirty" in binding:
        params["expected_dirty"] = binding["dirty"]
        params["expected_dirty_is_set"] = True
    return params


def _normalise_documentation_visibility(value: Any, *, label: str) -> int:
    named = {"hidden": -1, "normal": 0, "visible": 0, "grayed": 2, "greyed": 2}
    if isinstance(value, str):
        key = value.strip().lower()
        if key in named:
            return named[key]
    if isinstance(value, int) and not isinstance(value, bool) and value in {-1, 0, 2}:
        return value
    raise ValueError(f"{label} must be hidden, normal, grayed, -1, 0, or 2")


def _require_documentation_text(params: dict[str, Any], key: str, *, label: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label}.{key} must be a non-empty string")
    if len(value) > 1024:
        raise ValueError(f"{label}.{key} is limited to 1024 characters")
    return value.strip()


def _optional_documentation_int(
    params: dict[str, Any], key: str, *, label: str, minimum: int = -32768, maximum: int = 32767
) -> int:
    value = params.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
        raise ValueError(f"{label}.{key} must be an integer between {minimum} and {maximum}")
    return value


def _normalise_visibility_entries(
    value: Any, *, label: str, reference_key: str, allow_local: bool
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value or len(value) > 500:
        raise ValueError(f"{label} must contain 1 to 500 entries")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(value, start=1):
        item_label = f"{label}[{index}]"
        if not isinstance(entry, dict) or set(entry) != {reference_key, "visibility"}:
            raise ValueError(f"{item_label} must contain only {reference_key} and visibility")
        raw_ref = entry.get(reference_key)
        if reference_key == "ref":
            ref = _normalise_documentation_ref(raw_ref, label=f"{item_label}.ref", allow_local=allow_local)
        else:
            ref = _require_documentation_text(entry, reference_key, label=item_label)
        if ref in seen:
            raise ValueError(f"{label} contains duplicate reference {ref!r}")
        seen.add(ref)
        result.append({reference_key: ref, "visibility": _normalise_documentation_visibility(
            entry.get("visibility"), label=f"{item_label}.visibility"
        )})
    return result


def _normalise_documentation_points(value: Any, *, label: str, scale_to_mm: float) -> list[list[float]]:
    if not isinstance(value, list) or len(value) > 1000:
        raise ValueError(f"{label} must be a list with at most 1000 points")
    result: list[list[float]] = []
    for index, point in enumerate(value, start=1):
        if not isinstance(point, (list, tuple)) or len(point) != 2 or not all(_is_real_number(v) for v in point):
            raise ValueError(f"{label}[{index}] must contain exactly two finite numbers")
        result.append([float(point[0]) * scale_to_mm, float(point[1]) * scale_to_mm])
    return result


def _normalise_documentation_operation(
    operation_type: str,
    params: dict[str, Any],
    *,
    label: str,
    scale_to_mm: float,
) -> dict[str, Any]:
    item_label = f"{label}.params"
    if operation_type.endswith("sheet_layer"):
        allowed = {"name", "title", "description", "dpi", "sheet_width", "sheet_height", "visibility"}
        if operation_type.startswith("delete"):
            allowed = {"target", "confirm"}
        elif operation_type.startswith("update"):
            allowed |= {"target"}
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(f"{item_label} has unsupported key(s): {', '.join(unknown)}")
        if operation_type == "delete_sheet_layer":
            target = _normalise_documentation_ref(params.get("target"), label=f"{item_label}.target", allow_local=False)
            if params.get("confirm") != "DELETE_SHEET_LAYER_AND_CONTENTS":
                raise ValueError(f"{item_label}.confirm must equal DELETE_SHEET_LAYER_AND_CONTENTS")
            return {"target": target, "confirm": params["confirm"]}
        result: dict[str, Any] = {}
        for key in ("name", "title"):
            if operation_type.startswith("create") or key in params:
                result[key] = _require_documentation_text(params, key, label=item_label)
        if "description" in params:
            if not isinstance(params["description"], str) or len(params["description"]) > 4096:
                raise ValueError(f"{item_label}.description must be text limited to 4096 characters")
            result["description"] = params["description"]
        if "dpi" in params:
            result["dpi"] = _optional_documentation_int(params, "dpi", label=item_label, minimum=1)
        for public_key, wire_key in (("sheet_width", "sheet_width_mm"), ("sheet_height", "sheet_height_mm")):
            if public_key in params:
                result[wire_key] = _coerce_positive_number(params, public_key, label=item_label) * scale_to_mm
        if "visibility" in params:
            result["visibility"] = _normalise_documentation_visibility(params["visibility"], label=f"{item_label}.visibility")
        if operation_type == "update_sheet_layer":
            result["target"] = _normalise_documentation_ref(
                params.get("target"), label=f"{item_label}.target", allow_local=False
            )
            if len(result) == 1:
                raise ValueError(f"{item_label} must contain at least one sheet layer change")
        return result

    if operation_type.endswith("viewport") and "annotation" not in operation_type:
        delete_keys = {"target", "sheet_layer_ref", "confirm"}
        change_keys = {
            "sheet_layer_ref", "name", "scale", "x", "y", "projection_type", "view_type",
            "render_type", "foreground_render_type", "source_layers", "source_classes",
            "replace_source_layers", "replace_source_classes", "crop_points", "clear_crop",
        }
        allowed = delete_keys if operation_type.startswith("delete") else change_keys | ({"target"} if operation_type.startswith("update") else set())
        unknown = sorted(set(params) - allowed)
        if unknown:
            raise ValueError(f"{item_label} has unsupported key(s): {', '.join(unknown)}")
        result = {
            "sheet_layer_ref": _normalise_documentation_ref(
                params.get("sheet_layer_ref"), label=f"{item_label}.sheet_layer_ref",
                allow_local=operation_type == "create_viewport",
            )
        }
        if operation_type.startswith(("update", "delete")):
            result["target"] = _normalise_documentation_ref(
                params.get("target"), label=f"{item_label}.target", allow_local=False
            )
        if operation_type == "delete_viewport":
            if params.get("confirm") != "DELETE_VIEWPORT_AND_ANNOTATIONS":
                raise ValueError(f"{item_label}.confirm must equal DELETE_VIEWPORT_AND_ANNOTATIONS")
            result["confirm"] = params["confirm"]
            return result
        creating = operation_type == "create_viewport"
        if creating or "name" in params:
            result["name"] = _require_documentation_text(params, "name", label=item_label)
        if creating or "scale" in params:
            result["scale"] = _coerce_positive_number(params, "scale", label=item_label)
        has_x, has_y = "x" in params, "y" in params
        if has_x != has_y or (creating and not has_x):
            raise ValueError(f"{item_label}.x and y must be supplied together")
        if has_x:
            result["x"] = _coerce_number(params, "x", required=True, label=item_label) * scale_to_mm
            result["y"] = _coerce_number(params, "y", required=True, label=item_label) * scale_to_mm
        for key in ("projection_type", "view_type", "render_type", "foreground_render_type"):
            if creating or key in params:
                result[key] = 7 if key == "view_type" and key not in params else _optional_documentation_int(params, key, label=item_label)
                if key == "view_type" and not 3 <= result[key] <= 16:
                    raise ValueError(f"{item_label}.view_type must be a standard view from 3 through 16; custom orientation is not supported")
        for public_key, reference_key in (("source_layers", "ref"), ("source_classes", "name")):
            if creating or public_key in params:
                entries = _normalise_visibility_entries(
                    params.get(public_key), label=f"{item_label}.{public_key}",
                    reference_key=reference_key, allow_local=False,
                )
                result[public_key] = entries
        for key in ("replace_source_layers", "replace_source_classes", "clear_crop"):
            if key in params:
                result[key] = _coerce_bool(params, key, label=item_label)
        if "crop_points" in params:
            points = _normalise_documentation_points(
                params["crop_points"], label=f"{item_label}.crop_points", scale_to_mm=scale_to_mm
            )
            if len(points) < 3:
                raise ValueError(f"{item_label}.crop_points must contain at least three points")
            result["crop_points"] = points
        if result.get("clear_crop") and "crop_points" in result:
            raise ValueError(f"{item_label} cannot clear and replace a viewport crop together")
        if not creating and set(result) == {"sheet_layer_ref", "target"}:
            raise ValueError(f"{item_label} must contain at least one viewport change")
        return result

    delete_keys = {"target", "sheet_layer_ref", "viewport_ref", "confirm"}
    update_keys = {"target", "sheet_layer_ref", "viewport_ref", "name", "class_name", "text", "dx", "dy"}
    create_keys = {
        "sheet_layer_ref", "viewport_ref", "annotation_kind", "class_name", "name", "text", "x1", "y1",
        "x2", "y2", "offset", "text_offset", "dimension_type", "marker_style", "marker_size",
        "marker_angle", "points",
    }
    allowed = delete_keys if operation_type.startswith("delete") else update_keys if operation_type.startswith("update") else create_keys
    unknown = sorted(set(params) - allowed)
    if unknown:
        raise ValueError(f"{item_label} has unsupported key(s): {', '.join(unknown)}")
    result = {
        "sheet_layer_ref": _normalise_documentation_ref(
            params.get("sheet_layer_ref"), label=f"{item_label}.sheet_layer_ref",
            allow_local=operation_type == "create_viewport_annotation",
        ),
        "viewport_ref": _normalise_documentation_ref(
            params.get("viewport_ref"), label=f"{item_label}.viewport_ref",
            allow_local=operation_type == "create_viewport_annotation",
        ),
    }
    if operation_type.startswith(("update", "delete")):
        result["target"] = _normalise_documentation_ref(
            params.get("target"), label=f"{item_label}.target", allow_local=False
        )
    if operation_type == "delete_viewport_annotation":
        if params.get("confirm") != "DELETE_VIEWPORT_ANNOTATION":
            raise ValueError(f"{item_label}.confirm must equal DELETE_VIEWPORT_ANNOTATION")
        result["confirm"] = params["confirm"]
        return result
    if operation_type == "update_viewport_annotation":
        for key in ("name", "class_name", "text"):
            if key in params:
                if not isinstance(params[key], str) or len(params[key]) > 4096:
                    raise ValueError(f"{item_label}.{key} must be text limited to 4096 characters")
                result[key] = params[key]
        has_dx, has_dy = "dx" in params, "dy" in params
        if has_dx != has_dy:
            raise ValueError(f"{item_label}.dx and dy must be supplied together")
        if has_dx:
            result["delta_x"] = _coerce_number(params, "dx", required=True, label=item_label) * scale_to_mm
            result["delta_y"] = _coerce_number(params, "dy", required=True, label=item_label) * scale_to_mm
        if set(result) == {"sheet_layer_ref", "viewport_ref", "target"}:
            raise ValueError(f"{item_label} must contain at least one annotation change")
        return result

    annotation_kind = str(params.get("annotation_kind", "") or "").strip().lower()
    if annotation_kind not in {"text", "dimension", "marker", "redline"}:
        raise ValueError(f"{item_label}.annotation_kind must be text, dimension, marker, or redline")
    result["annotation_kind"] = annotation_kind
    result["class_name"] = _require_documentation_text(params, "class_name", label=item_label)
    if "name" in params:
        result["name"] = _require_documentation_text(params, "name", label=item_label)
    if annotation_kind == "text":
        result["text"] = _require_documentation_text(params, "text", label=item_label)
    if annotation_kind in {"text", "dimension", "marker"}:
        required_coordinates = ("x1", "y1") if annotation_kind == "text" else ("x1", "y1", "x2", "y2")
        for key in required_coordinates:
            result[key] = _coerce_number(params, key, required=True, label=item_label) * scale_to_mm
    if annotation_kind in {"dimension", "marker"} and result["x1"] == result["x2"] and result["y1"] == result["y2"]:
        raise ValueError(f"{item_label} annotation endpoints must not be identical")
    for key in ("offset", "text_offset"):
        if key in params:
            result[key] = _coerce_number(params, key, required=True, label=item_label) * scale_to_mm
    for key in ("dimension_type", "marker_style", "marker_size", "marker_angle"):
        if key in params:
            minimum, maximum = (0, 32767) if key in {"marker_style", "marker_size"} else ((-360, 360) if key == "marker_angle" else (-32768, 32767))
            result[key] = _optional_documentation_int(params, key, label=item_label, minimum=minimum, maximum=maximum)
    if annotation_kind == "redline":
        points = _normalise_documentation_points(
            params.get("points"), label=f"{item_label}.points", scale_to_mm=scale_to_mm
        )
        if len(points) < 3:
            raise ValueError(f"{item_label}.points must contain at least three points")
        result["points"] = points
    return result


def _documentation_wire_operation(operation: dict[str, Any]) -> dict[str, Any]:
    params = dict(operation["params"])
    wire = {"op": _DOCUMENTATION_WIRE_OPS[operation["type"]], **params}
    operation_id = str(operation.get("operation_id", "") or "")
    if operation_id:
        wire["local_ref"] = operation_id
    for public_key, prefix, reference_key in (
        ("source_layers", "source_layer", "ref"),
        ("source_classes", "source_class", "name"),
    ):
        if public_key not in wire:
            continue
        entries = wire.pop(public_key)
        wire[f"{prefix}_count"] = len(entries)
        for index, entry in enumerate(entries, start=1):
            suffix = "ref" if reference_key == "ref" else "name"
            wire[f"{prefix}_{index}_{suffix}"] = entry[reference_key]
            wire[f"{prefix}_{index}_visibility"] = entry["visibility"]
    for public_key, prefix in (("crop_points", "crop_point"), ("points", "point")):
        if public_key not in wire:
            continue
        points = wire.pop(public_key)
        wire[f"{prefix}_count"] = len(points)
        for index, point in enumerate(points, start=1):
            wire[f"{prefix}_{index}_x"] = point[0]
            wire[f"{prefix}_{index}_y"] = point[1]
    return wire


def _forgive_operation(operation: dict[str, Any]) -> dict[str, Any]:
    """op<->type, id->operation_id, flat params, stringified params (weak models)."""
    op = dict(operation)
    if "type" not in op and "op" in op:
        op["type"] = op.pop("op")
    if "operation_id" not in op and isinstance(op.get("id"), str):
        op["operation_id"] = op.pop("id")
    params = op.get("params")
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except ValueError:
            pass
    if params is None:
        params = {k: v for k, v in op.items() if k not in {"type", "operation_id"}}
        op = {k: op[k] for k in ("type", "operation_id") if k in op}
    op["params"] = params
    if isinstance(op.get("type"), str):
        alias = {"set_property": "set_properties", "properties": "set_properties", "move": "transform",
                 "update": "set_properties", "remove": "delete"}
        op["type"] = alias.get(op["type"].strip().lower(), op["type"])
    return op


def _normalise_execute_operations(
    operations: list[dict[str, Any]],
    *,
    coordinate_units: CoordinateUnits = "mm",
    max_operations: int = 250,
) -> list[dict[str, Any]]:
    if not isinstance(operations, list) or not 1 <= len(operations) <= max_operations:
        raise ValueError(f"operations must contain 1 to {max_operations} typed operations")
    scale_to_mm = _coordinate_scale_to_mm(coordinate_units)
    normalised: list[dict[str, Any]] = []
    for index, operation in enumerate(operations, start=1):
        label = f"operations[{index}]"
        if not isinstance(operation, dict):
            raise ValueError(f"{label} must be an object")
        operation = _forgive_operation(operation)
        unknown = sorted(set(operation) - {"type", "operation_id", "params"})
        if unknown:
            raise ValueError(f"{label} has unsupported key(s): {', '.join(unknown)}")
        operation_type = str(operation.get("type", "") or "").strip().lower()
        general_operation_types = {
            "create",
            "set_properties",
            "transform",
            "reshape",
            "update_parametric",
            "duplicate",
            "delete",
            "order",
        }
        if operation_type not in general_operation_types | _DOCUMENTATION_OPERATION_TYPES:
            raise ValueError(
                f"{label}.type must be create, set_properties, transform, reshape, "
                "update_parametric, duplicate, delete, or a typed sheet layer/viewport/annotation lifecycle operation"
            )
        params = operation.get("params")
        if not isinstance(params, dict):
            raise ValueError(f"{label}.params must be an object")
        operation_id = str(operation.get("operation_id", "") or "").strip()
        if operation_id and (len(operation_id) > 128 or not _IDEMPOTENCY_KEY_RE.fullmatch(operation_id)):
            raise ValueError(
                f"{label}.operation_id must match {_IDEMPOTENCY_KEY_RE.pattern} and be at most 128 characters"
            )

        if operation_id and operation_type not in {"create", "duplicate"} | _DOCUMENTATION_CREATE_TYPES:
            raise ValueError(f"{label}.operation_id is supported only for create and duplicate operations")

        if operation_type in _DOCUMENTATION_OPERATION_TYPES:
            if operation_type in _DOCUMENTATION_CREATE_TYPES and not operation_id:
                raise ValueError(f"{label}.operation_id is required for documentation create operations")
            canonical_params = _normalise_documentation_operation(
                operation_type,
                params,
                label=label,
                scale_to_mm=scale_to_mm,
            )
            item = {"type": operation_type, "params": canonical_params}
            if operation_id:
                item["operation_id"] = operation_id
            normalised.append(item)
            continue

        if operation_type == "create":
            canonical_params = _normalise_create_primitive(
                params,
                label=f"{label}.params",
                scale_to_mm=scale_to_mm,
            )
        elif operation_type == "set_properties":
            unknown_params = sorted(set(params) - {"edits"})
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            canonical_params = {
                "edits": _normalise_operation_property_edits(params.get("edits"), label=f"{label}.params.edits")
            }
        elif operation_type == "transform":
            unknown_params = sorted(
                set(params) - {
                    "target", "ref", "dx", "dy", "rotation_deg",
                    "scale_x", "scale_y", "pivot_x", "pivot_y",
                }
            )
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            target = _normalise_atomic_target(
                params.get("target", params.get("ref")),
                label=f"{label}.params.target",
            )
            dx = _coerce_number(params, "dx", default=0, label=f"{label}.params")
            dy = _coerce_number(params, "dy", default=0, label=f"{label}.params")
            rotation_deg = _coerce_number(params, "rotation_deg", default=0, label=f"{label}.params")
            scale_x = _coerce_positive_number(params, "scale_x", default=1, label=f"{label}.params")
            scale_y = _coerce_positive_number(params, "scale_y", default=1, label=f"{label}.params")
            has_pivot_x = "pivot_x" in params
            has_pivot_y = "pivot_y" in params
            if has_pivot_x != has_pivot_y:
                raise ValueError(f"{label}.params.pivot_x and pivot_y must be provided together")
            if dx == 0 and dy == 0 and rotation_deg == 0 and scale_x == 1 and scale_y == 1:
                raise ValueError(f"{label}.params transform must change translation, rotation, or scale")
            canonical_params = {
                "target": target,
                "dx": dx,
                "dy": dy,
                "rotation_deg": rotation_deg,
                "scale_x": scale_x,
                "scale_y": scale_y,
            }
            if has_pivot_x:
                canonical_params["pivot_x"] = _coerce_number(
                    params, "pivot_x", required=True, label=f"{label}.params"
                )
                canonical_params["pivot_y"] = _coerce_number(
                    params, "pivot_y", required=True, label=f"{label}.params"
                )
            canonical_params = _scale_geometry_params(canonical_params, scale_to_mm)
        elif operation_type == "reshape":
            unknown_params = sorted(
                set(params) - {"target", "ref", "start_x", "start_y", "end_x", "end_y", "x1", "y1", "x2", "y2"}
            )
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            canonical_params = {
                "target": _normalise_atomic_target(
                    params.get("target", params.get("ref")),
                    label=f"{label}.params.target",
                ),
                "start_x": _coerce_number_any(params, ("start_x", "x1"), required=True, label=f"{label}.params"),
                "start_y": _coerce_number_any(params, ("start_y", "y1"), required=True, label=f"{label}.params"),
                "end_x": _coerce_number_any(params, ("end_x", "x2"), required=True, label=f"{label}.params"),
                "end_y": _coerce_number_any(params, ("end_y", "y2"), required=True, label=f"{label}.params"),
            }
            if (
                canonical_params["start_x"] == canonical_params["end_x"]
                and canonical_params["start_y"] == canonical_params["end_y"]
            ):
                raise ValueError(f"{label}.params endpoints must not be identical")
            canonical_params = _scale_geometry_params(canonical_params, scale_to_mm)
        elif operation_type == "update_parametric":
            unknown_params = sorted(
                set(params) - {"target", "ref", "plugin_name", "descriptor_fingerprint", "parameters"}
            )
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            parametric = _normalise_create_primitive(
                {
                    "object_type": "parametric",
                    "plugin_name": params.get("plugin_name"),
                    "descriptor_fingerprint": params.get("descriptor_fingerprint"),
                    "parameters": params.get("parameters", []),
                },
                label=f"{label}.params",
            )
            canonical_params = {
                "target": _normalise_atomic_target(
                    params.get("target", params.get("ref")),
                    label=f"{label}.params.target",
                ),
                "object_type": "parametric",
                **{
                    key: value
                    for key, value in parametric.items()
                    if key not in {"x1", "y1", "rotation", "require_wall_host", "role"}
                },
            }
        elif operation_type == "order":
            unknown_params = sorted(set(params) - {"target", "ref", "position"})
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            position = str(params.get("position", "")).strip().lower()
            if position not in {"front", "back", "forward", "backward"}:
                raise ValueError(f"{label}.params.position must be front, back, forward or backward")
            canonical_params = {"target": _normalise_atomic_target(params.get("target", params.get("ref")),
                                                                   label=f"{label}.params.target"),
                                "position": position}
        elif operation_type == "duplicate":
            unknown_params = sorted(set(params) - {"target", "ref", "dx", "dy"})
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            canonical_params = {
                "target": _normalise_atomic_target(
                    params.get("target", params.get("ref")),
                    label=f"{label}.params.target",
                ),
                "dx": _coerce_number(params, "dx", default=0, label=f"{label}.params"),
                "dy": _coerce_number(params, "dy", default=0, label=f"{label}.params"),
            }
            canonical_params["dx"] *= scale_to_mm
            canonical_params["dy"] *= scale_to_mm
        else:
            unknown_params = sorted(set(params) - {"target", "ref"})
            if unknown_params:
                raise ValueError(f"{label}.params has unsupported key(s): {', '.join(unknown_params)}")
            canonical_params = {
                "target": _normalise_atomic_target(
                    params.get("target", params.get("ref")),
                    label=f"{label}.params.target",
                )
            }
            if canonical_params["target"].startswith("$"):
                raise ValueError(f"{label}.params.target delete requires an external uuid:, name:, or handle: ref")

        item: dict[str, Any] = {"type": operation_type, "params": canonical_params}
        if operation_id:
            item["operation_id"] = operation_id
        normalised.append(item)
    families = {
        "documentation" if item["type"] in _DOCUMENTATION_OPERATION_TYPES else "general"
        for item in normalised
    }
    if len(families) != 1:
        raise ValueError("general and documentation operations may not be mixed in one atomic plan")
    if families == {"documentation"}:
        local_types: dict[str, str] = {}
        expected_parent_type = {
            "sheet_layer_ref": "create_sheet_layer",
            "viewport_ref": "create_viewport",
        }
        for index, item in enumerate(normalised, start=1):
            for key, required_type in expected_parent_type.items():
                reference = item["params"].get(key)
                if isinstance(reference, str) and reference.startswith("$"):
                    local_id = reference[1:]
                    if local_types.get(local_id) != required_type:
                        raise ValueError(
                            f"operations[{index}].params.{key} must reference a prior {required_type} operation_id"
                        )
            operation_id = str(item.get("operation_id", "") or "")
            if operation_id:
                if operation_id in local_types:
                    raise ValueError(f"operations[{index}].operation_id is duplicated: {operation_id}")
                local_types[operation_id] = item["type"]
    return normalised


def _operation_plan_hash(operations: list[dict[str, Any]]) -> str:
    encoded = json.dumps(
        operations,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact_operation_receipts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    receipts: list[dict[str, Any]] = []
    for index, entry in enumerate(value, start=1):
        if not isinstance(entry, dict):
            continue
        receipt: dict[str, Any] = {"index": entry.get("index", index)}
        for key in (
            "type",
            "op",
            "operation_id",
            "local_ref",
            "ref",
            "target",
            "target_ref",
            "uuid",
            "handle",
            "verified",
            "plugin_name",
            "wall_host_preserved",
            "parameter_count",
            "layer",
            "position",
            "changed",
        ):
            field = entry.get(key)
            if isinstance(field, (str, bool, int)) and not isinstance(field, float) and field != "":
                receipt[key] = field
        after_endpoints = entry.get("after_endpoints")
        if isinstance(after_endpoints, list):
            receipt["after_endpoints"] = after_endpoints
        # Deleting a wall also deletes the doors/windows inserted in it (native reports them).
        cascaded = entry.get("cascaded_inserts")
        if isinstance(cascaded, list) and cascaded:
            receipt["cascaded_inserts"] = [str(u) for u in cascaded if isinstance(u, str) and u]
        if isinstance(entry.get("parent_uuid"), str) and entry["parent_uuid"]:
            receipt["parent_uuid"] = entry["parent_uuid"]
        # Native duplicate receipts carry the newly created object's stable
        # identity in the nested semantic snapshot. Promote only the compact
        # identity fields so callers can safely target the duplicate in a
        # later transaction without requiring a drawing scan.
        duplicate = entry.get("duplicate")
        if isinstance(duplicate, dict):
            for key in ("uuid", "handle", "type"):
                field = duplicate.get(key)
                if key not in receipt and isinstance(field, str) and field:
                    receipt[key] = field
        receipts.append(receipt)
    return receipts


# --------------------------------------------------------------------------
# Wire compilation (ported from 0.7 vw_execute_operations) and receipts.
# --------------------------------------------------------------------------

def normalise_plan(operations: Any, coordinate_units: str = "mm") -> list[dict[str, Any]]:
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except ValueError:
            raise ValueError("operations must be a JSON list of operations") from None
    if isinstance(operations, dict):
        operations = [operations]
    return _normalise_execute_operations(operations, coordinate_units=coordinate_units)


def is_documentation_plan(normalised: list[dict[str, Any]]) -> bool:
    return bool(normalised) and normalised[0]["type"] in _DOCUMENTATION_OPERATION_TYPES


def compile_documentation(normalised: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_documentation_wire_operation(operation) for operation in normalised]


ORDER_WIRE_OP = "order"  # native stacking-order op {target, position} (PROTOCOL.md 6.6)


def compile_general(normalised: list[dict[str, Any]], max_operations: int = 250) -> list[dict[str, Any]]:
    """Normalised 0.7 operations -> native apply_operations wire ops."""
    wire_operations: list[dict[str, Any]] = []
    for operation in normalised:
        operation_type = operation["type"]
        operation_id = str(operation.get("operation_id", "") or "")
        canonical = dict(operation["params"])
        if operation_type == "create":
            wire_item = {"op": "create", **canonical}
            wire_item.pop("role", None)
            if operation_id:
                wire_item["local_ref"] = operation_id
            wire_operations.append(wire_item)
            continue
        if operation_type == "transform":
            wire_item = {"op": "object.transform", "target": canonical["target"], "delta_x": canonical["dx"],
                         "delta_y": canonical["dy"], "rotation_degrees": canonical["rotation_deg"],
                         "scale_x": canonical["scale_x"], "scale_y": canonical["scale_y"]}
            if "pivot_x" in canonical:
                wire_item["pivot_x"] = canonical["pivot_x"]
                wire_item["pivot_y"] = canonical["pivot_y"]
        elif operation_type == "reshape":
            wire_item = {"op": "object.reshape", "target": canonical["target"], "start_x": canonical["start_x"],
                         "start_y": canonical["start_y"], "end_x": canonical["end_x"], "end_y": canonical["end_y"]}
        elif operation_type == "update_parametric":
            wire_item = {"op": "object.update_parametric", **canonical}
        elif operation_type == "duplicate":
            wire_item = {"op": "object.duplicate", "target": canonical["target"], "delta_x": canonical["dx"],
                         "delta_y": canonical["dy"]}
            if operation_id:
                wire_item["local_ref"] = operation_id
        elif operation_type == "delete":
            wire_item = {"op": "object.delete", "target": canonical["target"], "confirm": "DELETE_OBJECT"}
        elif operation_type == "order":
            wire_item = {"op": ORDER_WIRE_OP, "target": canonical["target"], "position": canonical["position"]}
        else:  # set_properties
            for edit in canonical["edits"]:
                if any(edit.get(guard) for guard in ("expected_type", "expected_layer", "expected_name")):
                    raise ValueError("expected_type/expected_layer/expected_name guards are not supported by "
                                     "native apply_operations; remove them")
                for property_name, value in edit["properties"].items():
                    wire_operations.append({"op": "set_property", "target": str(edit["ref"]),
                                            "property_name": property_name, "value": value})
            continue
        wire_operations.append(wire_item)
    if len(wire_operations) > max_operations:
        raise ValueError(f"the plan expands to {len(wire_operations)} native operations; the limit is "
                         f"{max_operations} per transaction")
    return wire_operations


def wire_params(wire_operations: list[dict[str, Any]], idempotency_key: str, extra: Optional[dict] = None) -> dict:
    params: dict[str, Any] = {"operation_count": len(wire_operations), "idempotency_key": idempotency_key}
    for index, wire_operation in enumerate(wire_operations, start=1):
        params[f"operation_{index}_json"] = json.dumps(wire_operation, ensure_ascii=False, separators=(",", ":"),
                                                       sort_keys=True)
    if extra:
        params.update(extra)
    return params


def summarise_transaction(decoded: Any, expected_wire_count: int) -> dict[str, Any]:
    """Compact receipt for an apply_operations reply (no timing/bridge noise)."""
    if not isinstance(decoded, dict):
        return {"committed": False, "receipts": []}
    transaction = decoded.get("transaction") if isinstance(decoded.get("transaction"), dict) else decoded
    source = transaction.get("operations")
    if not isinstance(source, list):
        source = transaction.get("results")
    if not isinstance(source, list):
        source = transaction.get("created")
    receipts = _compact_operation_receipts(source)
    count = transaction.get("operation_count", transaction.get("applied_count", transaction.get("created_count", len(receipts))))
    if not isinstance(count, int) or isinstance(count, bool):
        count = len(receipts)
    committed = transaction.get("committed") is True or (decoded.get("committed") is True)
    out: dict[str, Any] = {
        "committed": committed and count == expected_wire_count,
        "applied": count,
        "receipts": receipts,
    }
    if decoded.get("replayed"):
        out["replayed"] = True
    sent_back = decoded.get("sent_to_back", transaction.get("sent_to_back"))
    if isinstance(sent_back, int) and not isinstance(sent_back, bool):
        out["sent_to_back"] = sent_back
    joins = decoded.get("wall_joins")
    if not isinstance(joins, list):
        joins = transaction.get("wall_joins")
    if isinstance(joins, list):
        out["wall_joins"] = [j for j in joins if isinstance(j, dict)]
    return out
