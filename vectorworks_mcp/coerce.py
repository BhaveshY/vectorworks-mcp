"""Forgiving input parsing for weak models (contract 2.4) and unit handling.

All lengths are normalised to millimetres (the native bridge unit). A value
that carries its own unit ("3m", "3000 mm", 12'6") wins over the call's
``units``; bare numbers use ``units``; built-in defaults are always mm.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Iterable, Optional

from .errors import validation

UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4, "ft": 304.8}
_UNIT_SYNONYMS = {
    "mm": "mm", "millimeter": "mm", "millimeters": "mm", "millimetre": "mm", "millimetres": "mm",
    "cm": "cm", "centimeter": "cm", "centimeters": "cm", "centimetre": "cm", "centimetres": "cm",
    "m": "m", "meter": "m", "meters": "m", "metre": "m", "metres": "m",
    "in": "in", "inch": "in", "inches": "in", '"': "in",
    "ft": "ft", "foot": "ft", "feet": "ft", "'": "ft",
}

_LEN_RE = re.compile(r"^\s*([+-]?(?:\d+(?:[.,]\d*)?|[.,]\d+)(?:[eE][+-]?\d+)?)\s*([a-zA-Z\"']*)\s*$")
_FTIN_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(?:'|ft|feet|foot)\s*-?\s*(\d+(?:\.\d+)?)?\s*(?:\"|in|inch|inches)?\s*$")


def units_name(value: Any, default: str = "mm") -> str:
    if value is None or value == "":
        return default
    key = str(value).strip().lower()
    if key in _UNIT_SYNONYMS:
        return _UNIT_SYNONYMS[key]
    raise validation(f"units must be one of mm, cm, m, in, ft (got {value!r})", "units", "mm")


def scale_for(units: str) -> float:
    return UNIT_TO_MM[units]


def parse_json_maybe(value: Any) -> Any:
    """Stringified JSON arrays/objects are parsed; everything else passes through."""
    if isinstance(value, str):
        text = value.strip()
        if text[:1] in "[{" and text[-1:] in "]}":
            try:
                return json.loads(text)
            except ValueError:
                return value
    return value


def is_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def number(value: Any, field: str, *, example: Any = 0) -> float:
    """A plain (unitless) number; numeric strings accepted."""
    if is_number(value):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", ".") if value.count(",") == 1 and "." not in value else value.strip()
        try:
            result = float(text)
        except ValueError:
            raise validation(f"{field} must be a number (got {value!r})", field, example) from None
        if math.isfinite(result):
            return result
    raise validation(f"{field} must be a number (got {value!r})", field, example)


def integer(value: Any, field: str, *, example: Any = 1) -> int:
    result = number(value, field, example=example)
    if result != int(result):
        raise validation(f"{field} must be a whole number (got {value!r})", field, example)
    return int(result)


def clamp_int(value: Any, field: str, lo: int, hi: int, default: int, warnings: list[str]) -> int:
    if value is None or value == "":
        return default
    result = integer(value, field, example=default)
    if result < lo or result > hi:
        clamped = min(max(result, lo), hi)
        warnings.append(f"{field}={result} was clamped to {clamped} (allowed {lo}..{hi}).")
        return clamped
    return result


def boolean(value: Any, field: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in {"true", "yes", "y", "1", "on"}:
            return True
        if key in {"false", "no", "n", "0", "off", ""}:
            return False
    raise validation(f"{field} must be true or false (got {value!r})", field, True)


def text(value: Any, field: str, *, required: bool = False, max_len: int = 4096) -> str:
    if value is None:
        if required:
            raise validation(f"{field} is required", field, "Name")
        return ""
    if isinstance(value, (dict, list)):
        raise validation(f"{field} must be text", field, "Name")
    result = str(value)
    if required and not result.strip():
        raise validation(f"{field} must not be empty", field, "Name")
    if len(result) > max_len:
        raise validation(f"{field} is limited to {max_len} characters", field)
    return result


def length(value: Any, field: str, scale: float, *, example: Any = "3000mm") -> float:
    """A length in mm. Bare numbers are multiplied by ``scale``."""
    if is_number(value):
        return float(value) * scale
    if isinstance(value, str):
        raw = value.strip()
        m = _FTIN_RE.match(raw)
        if m and ("'" in raw or "ft" in raw.lower() or "feet" in raw.lower() or "foot" in raw.lower()):
            feet = float(m.group(1))
            inches = float(m.group(2) or 0)
            sign = -1.0 if feet < 0 else 1.0
            return (abs(feet) * 304.8 + inches * 25.4) * sign
        m = _LEN_RE.match(raw)
        if m:
            num = float(m.group(1).replace(",", "."))
            unit = m.group(2).lower()
            if not unit:
                return num * scale
            if unit in _UNIT_SYNONYMS:
                return num * UNIT_TO_MM[_UNIT_SYNONYMS[unit]]
    raise validation(f"{field} must be a length such as 3000, \"3m\", \"300 mm\" or \"12'6\\\"\" (got {value!r})", field, example)


def opt_length(src: dict, keys: Iterable[str], field: str, scale: float, default_mm: Optional[float] = None) -> Optional[float]:
    for key in keys:
        if key in src and src[key] is not None and src[key] != "":
            return length(src[key], field, scale)
    return default_mm


def point(value: Any, field: str, scale: float) -> list[float]:
    value = parse_json_maybe(value)
    if isinstance(value, dict):
        x = value.get("x", value.get("X"))
        y = value.get("y", value.get("Y"))
        if x is None or y is None:
            raise validation(f"{field} must be [x, y] or {{\"x\":..,\"y\":..}}", field, [0, 0])
        return [length(x, field + ".x", scale), length(y, field + ".y", scale)]
    if isinstance(value, str):
        parts = [p for p in re.split(r"[;,\s]+", value.strip().strip("()[]")) if p]
        if len(parts) == 2:
            return [length(parts[0], field + "[0]", scale), length(parts[1], field + "[1]", scale)]
        raise validation(f"{field} must be [x, y] or \"x,y\" (got {value!r})", field, [0, 0])
    if isinstance(value, (list, tuple)) and len(value) in (2, 3):
        return [length(value[0], field + "[0]", scale), length(value[1], field + "[1]", scale)]
    raise validation(f"{field} must be a point [x, y] (got {value!r})", field, [0, 0])


def points(value: Any, field: str, scale: float, *, minimum: int = 2, maximum: int = 1000) -> list[list[float]]:
    value = parse_json_maybe(value)
    if isinstance(value, str):
        chunks = [c for c in re.split(r"\s*;\s*|\)\s*,?\s*\(", value.strip().strip("()[]")) if c]
        value = chunks
    if not isinstance(value, (list, tuple)):
        raise validation(f"{field} must be a list of points [[x,y],...]", field, [[0, 0], [1000, 0], [1000, 1000]])
    # flat [x1,y1,x2,y2,...]
    if value and all(is_number(v) for v in value) and len(value) % 2 == 0:
        value = [value[i:i + 2] for i in range(0, len(value), 2)]
    result = [point(p, f"{field}[{i}]", scale) for i, p in enumerate(value)]
    if len(result) < minimum:
        raise validation(f"{field} needs at least {minimum} points", field, [[0, 0], [1000, 0], [1000, 1000]])
    if len(result) > maximum:
        raise validation(f"{field} allows at most {maximum} points", field)
    return result


def choice(value: Any, field: str, allowed: Iterable[str], synonyms: Optional[dict[str, str]] = None,
           default: Optional[str] = None) -> str:
    allowed = list(allowed)
    if value is None or value == "":
        if default is not None:
            return default
        raise validation(f"{field} is required; one of: {', '.join(allowed)}", field, allowed[0])
    key = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    if synonyms and key in synonyms:
        key = synonyms[key]
    lowered = {a.lower(): a for a in allowed}
    if key in lowered:
        return lowered[key]
    raise validation(f"{field}={value!r} is not valid; use one of: {', '.join(allowed)}", field, allowed[0])


def color(value: Any, field: str) -> str:
    """Return 'r,g,b' in 0..65535 (native). Accepts '#RRGGBB', [r,g,b] 0..255, 'r,g,b', names."""
    named = {"black": (0, 0, 0), "white": (255, 255, 255), "red": (255, 0, 0), "green": (0, 128, 0),
             "blue": (0, 0, 255), "yellow": (255, 255, 0), "gray": (128, 128, 128), "grey": (128, 128, 128),
             "orange": (255, 165, 0), "cyan": (0, 255, 255), "magenta": (255, 0, 255)}
    value = parse_json_maybe(value)
    rgb: Optional[tuple[float, ...]] = None
    if isinstance(value, str):
        raw = value.strip().lower()
        if raw in named:
            rgb = named[raw]
        elif re.fullmatch(r"#?[0-9a-f]{6}", raw):
            raw = raw.lstrip("#")
            rgb = tuple(int(raw[i:i + 2], 16) for i in (0, 2, 4))
        else:
            parts = [p for p in re.split(r"[,\s]+", raw.strip("()[]")) if p]
            if len(parts) == 3:
                rgb = tuple(number(p, field) for p in parts)
    elif isinstance(value, (list, tuple)) and len(value) == 3:
        rgb = tuple(number(p, field) for p in value)
    elif isinstance(value, dict) and {"r", "g", "b"} <= set(value):
        rgb = (number(value["r"], field), number(value["g"], field), number(value["b"], field))
    if rgb is None or any(c < 0 for c in rgb):
        raise validation(f"{field} must be a colour like \"#FF0000\", [255,0,0] or \"red\" (got {value!r})", field, "#FF0000")
    if max(rgb) <= 255:
        rgb = tuple(c * 257 for c in rgb)
    if max(rgb) > 65535:
        raise validation(f"{field} components must be 0..255 (or 0..65535)", field, "#FF0000")
    return ",".join(str(int(round(c))) for c in rgb)


def obj_list(value: Any, field: str, *, example: Any, maximum: int = 500) -> list[dict]:
    value = parse_json_maybe(value)
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or not value:
        raise validation(f"{field} must be a non-empty list of objects", field, example)
    out = []
    for i, item in enumerate(value):
        item = parse_json_maybe(item)
        if not isinstance(item, dict):
            raise validation(f"{field}[{i}] must be an object", f"{field}[{i}]", example[0] if isinstance(example, list) else example)
        out.append(item)
    if len(out) > maximum:
        raise validation(f"{field} allows at most {maximum} items per call", field)
    return out


def str_list(value: Any, field: str, maximum: int = 100) -> list[str]:
    value = parse_json_maybe(value)
    if value is None or value == "":
        return []
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",") if v.strip()]
    if not isinstance(value, list):
        raise validation(f"{field} must be a list of strings", field, ["uuid", "type", "name"])
    if len(value) > maximum:
        raise validation(f"{field} allows at most {maximum} entries", field)
    return [str(v) for v in value]


def lower_keys(args: dict) -> dict:
    """Case-insensitive top-level argument names."""
    out = {}
    for key, value in args.items():
        out[str(key).strip().lower() if isinstance(key, str) else key] = value
    return out
