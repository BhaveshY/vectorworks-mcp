"""Compile explicit 2D section height bands; no drawing-name heuristics."""
import math

from . import coerce as C
from .builders import norm_ref
from .errors import validation


def compile_section(src, field, scale):
    def optional_objects(key):
        value = src.get(key, [])
        return [] if value == [] else C.obj_list(value, field + "." + key, example=[{}], maximum=500)

    bands = C.obj_list(src.get("bands"), field + ".bands", example=[{"bottom": 0, "top": 3500, "delta": 500}], maximum=100)
    out = []
    for i, band in enumerate(bands):
        b = {k: C.length(band.get(k), f"{field}.bands[{i}].{k}", scale) for k in ("bottom", "top", "delta")}
        if not all(math.isfinite(v) for v in b.values()) or b["top"] <= b["bottom"] or b["top"] - b["bottom"] + b["delta"] <= 0:
            raise validation("Height bands must have positive old and new heights and finite coordinates", field + ".bands", [])
        out.append(b)
    out.sort(key=lambda b: b["bottom"])
    if not out or any(a["top"] > b["bottom"] for a, b in zip(out, out[1:])):
        raise validation("Supply non-overlapping clear-height bands", field + ".bands", [])
    labels = []
    for label in optional_objects("labels"):
        l = {"target": norm_ref(label.get("target"), field + ".labels.target"),
             "elevation": C.length(label.get("elevation"), field + ".labels.elevation", scale),
             "datum": C.length(label.get("datum", 0), field + ".labels.datum", scale),
             "origin": C.length(label.get("origin", 0), field + ".labels.origin", scale),
             "decimal_separator": C.text(label.get("decimal_separator", "."), field + ".labels.decimal_separator"),
             "zero_sign": C.text(label.get("zero_sign", "+"), field + ".labels.zero_sign"),
             "template": C.text(label.get("template", "{level}"), field + ".labels.template"),
             "precision": C.integer(label.get("precision", 2), field + ".labels.precision")}
        if not 0 <= l["precision"] <= 6 or not all(math.isfinite(l[k]) for k in ("elevation", "datum", "origin")):
            raise validation("Label precision must be 0..6 and elevations finite", field + ".labels", [])
        if l["decimal_separator"] not in (".", ","):
            raise validation("Label decimal_separator must be '.' or ','", field + ".labels.decimal_separator", ",")
        if l["zero_sign"] not in ("+", "±"):
            raise validation("Label zero_sign must be '+' or '±'", field + ".labels.zero_sign", "±")
        if "{level}" not in l["template"] and "{absolute}" not in l["template"]:
            raise validation("Label template needs {level} and/or {absolute} (formatted in metres)", field + ".labels.template", "{level} = {absolute} m NN")
        labels.append(l)
    expected = src.get("expected_geometry")
    if not isinstance(expected, dict) or not expected or any(not isinstance(v, str) or len(v) != 64 for v in expected.values()):
        raise validation("Supply expected_geometry={target: revision} from vw_read(what='geometry') to prevent stale/replayed edits", field + ".expected_geometry", {})
    fixed = []
    for anchor in optional_objects("fixed_points"):
        fixed.append({"target": norm_ref(anchor.get("target"), field + ".fixed_points.target"),
                      "vertex": C.integer(anchor.get("vertex"), field + ".fixed_points.vertex")})
    stairs = []
    for stair in optional_objects("stairs"):
        bottom = C.length(stair.get("bottom"), field + ".stairs.bottom", scale)
        top = C.length(stair.get("top"), field + ".stairs.top", scale)
        if not math.isfinite(bottom) or not math.isfinite(top) or top <= bottom:
            raise validation("Stair flight top must exceed bottom", field + ".stairs", [])
        stairs.append({"target": norm_ref(stair.get("target"), field + ".stairs.target"), "bottom": bottom, "top": top})
    rigid = []
    for binding in optional_objects("rigid"):
        elevation = C.length(binding.get("elevation"), field + ".rigid.elevation", scale)
        if not math.isfinite(elevation):
            raise validation("Rigid binding elevation must be finite", field + ".rigid", [])
        rigid.append({"target": norm_ref(binding.get("target"), field + ".rigid.target"), "elevation": elevation})
    return {"bands": out, "labels": labels, "fixed_points": fixed, "stairs": stairs,
            "rigid": rigid,
            "expected_geometry": {norm_ref(k, field + ".expected_geometry"): v for k,v in expected.items()},
            "preserve_text": [norm_ref(r, field + ".preserve_text") for r in C.str_list(src.get("preserve_text"), field + ".preserve_text")]}
