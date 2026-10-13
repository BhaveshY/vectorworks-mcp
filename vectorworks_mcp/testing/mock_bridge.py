"""Stdlib mock of the native bridge, protocol v2 (capability_revision 6), for
host e2e tests. ``--legacy`` emulates a pre-1.0 (revision 5) bridge.

    python -m vectorworks_mcp.testing.mock_bridge --port 0 --state-dir DIR --token TOKEN

Prints ``READY <port>`` on stdout once listening and writes the discovery file
``DIR/instances/<pid>.json`` (v2 mode). Test hook action ``mock_set`` changes
ui_state / adds latency. ``run_script`` executes Python against a small fake
``vs`` module bound to the mock document, so the host's snippet library can be
exercised for Python-level errors.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import re
import socket
import socketserver
import struct
import threading
import time
import traceback
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional

TYPE_IDS = {"line": 2, "rect": 3, "oval": 4, "polygon": 5, "arc": 6, "text": 10, "symbol": 15, "group": 11,
            "wall": 68, "linear_dimension": 63, "slab": 86, "roof": 86, "space": 86, "door": 86, "window": 86,
            "parametric": 86, "circle": 4, "image": 14}
MAIN_ACTIONS = {"get_document_info", "list_documents", "activate_document", "new_document", "close_document",
                "open_document", "save_document", "export_pdf", "export_dwg", "export_ifc", "import_dwg", "import_ifc",
                "export_image", "export_vectorworks_document", "layers", "get_layers", "manage_classes",
                "drawing_summary", "query_objects", "selection", "apply_operations", "transaction_status",
                "describe_parametric_schema", "get_view", "set_view", "capture_view", "saved_views", "stories", "symbol",
                "worksheet", "resources", "get_sheet_layers", "get_viewports", "get_viewport_annotations", "run_script"}
NO_DOC_ACTIONS = {"list_documents", "new_document", "open_document"}
MOCK_LIBRARY = {66: ["Aluminum HF", "Brick HF"], 96: ["Clouds", "Dash 2"], 109: ["Callouts 12 pt"],
                120: ["Brown-Yellow GR"]}
MOCK_FONTS = ["Arial", "Times New Roman"]
ASYNC_ACTIONS = {"export_pdf", "export_dwg", "import_dwg", "export_ifc", "import_ifc", "open_document",
                 "export_vectorworks_document", "run_script", "capture_view"}
LEGACY_MISSING = {"list_documents", "activate_document", "new_document", "close_document", "layers", "saved_views",
                  "stories", "run_script", "export_ifc", "import_ifc", "job_status", "cancel_job", "dismiss_home"}


class BridgeError(Exception):
    def __init__(self, message: str, code: Optional[str] = None, retryable: Optional[bool] = None,
                 commit_state: Optional[str] = None, details: Optional[dict] = None) -> None:
        super().__init__(message)
        self.code, self.retryable, self.commit_state, self.details = code, retryable, commit_state, details


def _uuid() -> str:
    return str(uuid.uuid4()).upper()


class Doc:
    def __init__(self, name: str, path: str = "") -> None:
        self.fingerprint = "doc-" + uuid.uuid4().hex[:12]
        self.name = name
        self.path = path
        self.dirty = False
        self.generation = 1
        self.layers = [{"uuid": _uuid(), "name": "Design Layer-1", "kind": "design", "visible": "show", "scale": 50.0,
                        "elevation": 0.0, "opacity": 100}]
        self.active_layer = self.layers[0]["name"]
        self.classes = {"None": {"visibility": "show", "active": True}, "Dimension": {"visibility": "show", "active": False}}
        self.objects: dict[str, dict] = {}
        self.order: list[str] = []
        self.selection: set[str] = set()
        self.saved_views = ["Plan"]
        self.stories: list[dict] = []
        self.records: dict[str, list[str]] = {"Costs": ["Cost", "Supplier"]}
        self.worksheets = {"Room Schedule": {"rows": 5, "columns": 3, "cells": {(1, 1): "Room", (1, 2): "Area"}}}
        self.symbols = ["Chair", "Table"]
        self.resources: dict[int, list[str]] = {66: ["Hatch A", "Hatch B"], 96: ["Dash"]}
        self.view = {"standard_view": 7, "projection": 6, "render_mode": 0, "zoom": 1.0, "center": [0.0, 0.0]}

    def info(self, session: str) -> dict:
        layer = next(l for l in self.layers if l["name"] == self.active_layer)
        return {"filename": self.name, "filepath": self.path, "layers": [l["name"] for l in self.layers if l["kind"] == "design"],
                "layer_count": len([l for l in self.layers if l["kind"] == "design"]), "total_objects": len(self.objects),
                "dirty": self.dirty, "document_fingerprint": self.fingerprint, "document_generation": self.generation,
                "bridge_session_id": session, "active_layer_uuid": layer["uuid"], "active_layer_name": layer["name"],
                "binding": {"file_path": self.path, "file_name": self.name, "document_fingerprint": self.fingerprint,
                            "document_generation": self.generation, "bridge_session_id": session, "dirty": self.dirty,
                            "active_layer_uuid": layer["uuid"], "active_layer_name": layer["name"]}}

    def brief(self, active: bool = False) -> dict:
        return {"fingerprint": self.fingerprint, "name": self.name, "path": self.path, "dirty": self.dirty, "active": active}

    def touch(self) -> None:
        self.dirty = True
        self.generation += 1

    def add(self, otype: str, **fields: Any) -> dict:
        obj = {"uuid": _uuid(), "type": otype, "type_id": TYPE_IDS.get(otype, 0), "name": fields.pop("name", ""),
               "layer": self.active_layer, "class": fields.pop("class_name", "None") or "None", "records": {}}
        obj.update(fields)
        obj["bounds"] = _bounds(obj)
        self.objects[obj["uuid"]] = obj
        self.order.append(obj["uuid"])
        return obj

    def record(self, obj: dict, fields: Optional[set] = None) -> dict:
        out = {k: v for k, v in obj.items() if k not in {"records"}}
        out["handle"] = "h" + obj["uuid"][:8]
        if fields:
            out = {k: v for k, v in out.items() if k in fields}
        return out


def _bounds(obj: dict) -> Optional[dict]:
    xs, ys = [], []
    for kx, ky in (("x1", "y1"), ("x2", "y2"), ("start_x", "start_y"), ("end_x", "end_y")):
        if kx in obj and ky in obj:
            xs.append(obj[kx]); ys.append(obj[ky])
    for p in obj.get("points") or []:
        xs.append(p[0]); ys.append(p[1])
    if obj.get("radius") and "x1" in obj:
        r = obj["radius"]; xs += [obj["x1"] - r, obj["x1"] + r]; ys += [obj["y1"] - r, obj["y1"] + r]
    if not xs:
        return None
    return {"top_left": [min(xs), max(ys)], "bottom_right": [max(xs), min(ys)]}


def _join_walls(new_walls: list, d: "Doc", no_join: set) -> list:
    """Mock of the native wall joining: coincident endpoints (1 mm) -> L, endpoint on a centreline -> T."""
    joins = []
    walls = [o for o in d.objects.values() if o.get("type") == "wall" and o["uuid"] not in no_join]
    done = set()
    for w in new_walls:
        for other in walls:
            if other["uuid"] == w["uuid"] or frozenset((w["uuid"], other["uuid"])) in done:
                continue
            ends_w = [(w["start_x"], w["start_y"]), (w["end_x"], w["end_y"])]
            ends_o = [(other["start_x"], other["start_y"]), (other["end_x"], other["end_y"])]
            hit = None
            for a in ends_w:
                for b in ends_o:
                    if abs(a[0] - b[0]) <= 1 and abs(a[1] - b[1]) <= 1:
                        hit = ("L", a)
            if hit is None:
                for first, second in ((w, other), (other, w)):
                    sx, sy, ex, ey = second["start_x"], second["start_y"], second["end_x"], second["end_y"]
                    length2 = (ex - sx) ** 2 + (ey - sy) ** 2
                    for px, py in ((first["start_x"], first["start_y"]), (first["end_x"], first["end_y"])):
                        t = ((px - sx) * (ex - sx) + (py - sy) * (ey - sy)) / length2 if length2 else -1
                        if 0 < t < 1:
                            cx, cy = sx + t * (ex - sx), sy + t * (ey - sy)
                            if abs(cx - px) <= 1 and abs(cy - py) <= 1:
                                hit = ("T", (px, py))
            if hit:
                done.add(frozenset((w["uuid"], other["uuid"])))
                joins.append({"first_uuid": w["uuid"], "second_uuid": other["uuid"], "kind": hit[0],
                              "point": [hit[1][0], hit[1][1]], "joined": True})
    return joins


def _png(path: str, width: int, height: int) -> int:
    row = bytes([0]) + bytes((x * 255 // max(1, width - 1)) for x in range(width) for _ in range(3))
    raw = row * height
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b"")
    Path(path).write_bytes(png)
    return len(png)


class Model:
    def __init__(self, legacy: bool, token: str) -> None:
        self.legacy = legacy
        self.token = token
        self.session = "mock-session-" + uuid.uuid4().hex[:8]
        self.docs = [Doc("Untitled 1")]
        self.active = 0
        self.ui_state = "idle"
        self.latency = 0.0
        self.main_lock = threading.Lock()
        self.jobs: dict[str, dict] = {}
        self.transactions: dict[str, dict] = {}
        self.port = 0
        self.lock = threading.Lock()
        self.clients = 0
        self.max_clients = 16
        self.calls: list[str] = []
        self.drop_reply_once: Optional[str] = None
        self.modal_dialog: Optional[dict] = None   # {title, class, kind: home|update_available|other}
        self.dismissed: list[dict] = []
        self.auto_dismiss = os.environ.get("VW_MCP_AUTO_DISMISS_HOME", "1").strip().lower() not in {"0", "false", "no"}

    def dismiss_dialog(self) -> Optional[dict]:
        """Close the Home screen / update dialog (never other dialogs). Returns the dismissal record."""
        dialog = self.modal_dialog
        if not dialog or dialog.get("kind") not in {"home", "update_available"}:
            return None
        self.modal_dialog = None
        if self.ui_state == "modal":
            self.ui_state = "idle"
        record = {"kind": dialog["kind"], "title": dialog.get("title", ""),
                  "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.dismissed = (self.dismissed + [record])[-10:]
        return record

    @property
    def doc(self) -> Optional[Doc]:
        return self.docs[self.active] if self.docs else None


class FakeVS:
    """Just enough of the vs.* API for the host snippets (handles = uuids).

    Only names that exist in the SDK vs.py stub (data/vs_signatures.json) may be
    called; anything else raises AttributeError like the real module, so a
    snippet using a made-up function (e.g. vs.HMirror) fails in the mock too.
    """

    def __init__(self, model: Model) -> None:
        self._m = model
        self._lists: dict[int, list[str]] = {}
        self._list_types: dict[int, int] = {}
        self.calls: list[str] = []

    @property
    def _d(self) -> Doc:
        return self._m.doc

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        from .. import vs_lint
        if name not in vs_lint._load():
            raise AttributeError(f"module 'vs' has no attribute '{name}'")

        def generic(*args, **kwargs):
            self.calls.append(name)
            return None
        return generic

    # handles
    def GetTypeN(self, h):
        if h is None:
            return 0
        if h in self._d.objects:
            return self._d.objects[h]["type_id"]
        if any(l["uuid"] == h for l in self._d.layers):
            return 31
        if h in self._d.records or h.startswith(("view:", "story:", "pio:", "res:")):
            return 47
        return 0

    def GetObjectByUuid(self, u):
        return u.upper() if u.upper() in self._d.objects else None

    def GetObjectUuid(self, h):
        return h if h in self._d.objects else ""

    def GetObject(self, name):
        for o in self._d.objects.values():
            if o.get("name") == name:
                return o["uuid"]
        for l in self._d.layers:
            if l["name"] == name:
                return l["uuid"]
        if name in self._d.records:
            return name
        if name in self._d.saved_views:
            return "view:" + name
        if any(s["name"] == name for s in self._d.stories):
            return "story:" + name
        return None

    def GetName(self, h):
        if h in self._d.objects:
            return self._d.objects[h].get("name", "")
        for l in self._d.layers:
            if l["uuid"] == h:
                return l["name"]
        return str(h).split(":", 1)[-1]

    def SetName(self, h, n):
        if h in self._d.objects:
            self._d.objects[h]["name"] = n

    def GetClass(self, h):
        return self._d.objects[h]["class"]

    def GetLayer(self, h):
        name = self._d.objects[h]["layer"]
        return next(l["uuid"] for l in self._d.layers if l["name"] == name)

    def SetParent(self, h, layer_handle):
        self._d.objects[h]["layer"] = self.GetName(layer_handle)

    def MirrorN(self, h, dup, p1, p2, preserve_matrix):
        obj = self._d.objects[h]
        if dup:
            new = self._d.add(obj["type"], **{k: v for k, v in obj.items() if k not in {"uuid", "type", "records", "bounds", "type_id"}})
            return new["uuid"]
        obj["mirrored"] = [list(p1), list(p2)]
        return h

    def DSelectAll(self):
        self._d.selection.clear()

    def SetSelect(self, h):
        if h in self._d.objects:
            self._d.selection.add(h)

    def Group(self):
        g = self._d.add("group", name="")
        g["members"] = sorted(self._d.selection)
        for u in self._d.selection:
            self._d.objects[u]["group"] = g["uuid"]
        self._last = g["uuid"]

    def LSActLayer(self):
        return getattr(self, "_last", None)

    def FSActLayer(self):
        return next(iter(self._d.selection), None)

    def Ungroup(self):
        for u in list(self._d.selection):
            if self._d.objects[u]["type"] == "group":
                for m in self._d.objects[u].get("members", []):
                    self._d.objects[m].pop("group", None)
                del self._d.objects[u]

    def NumRecords(self, h):
        return len(self._d.objects[h]["records"])

    def GetRecord(self, h, i):
        return list(self._d.objects[h]["records"])[i - 1]

    def NumFields(self, rh):
        return len(self._d.records.get(rh, []))

    def GetFldName(self, rh, i):
        return self._d.records[rh][i - 1]

    def GetRField(self, h, rec, field):
        return self._d.objects[h]["records"].get(rec, {}).get(field, "")

    def SetRecord(self, h, rec):
        self._d.objects[h]["records"].setdefault(rec, {})

    def SetRField(self, h, rec, field, value):
        if field not in self._d.records.get(rec, []):
            raise RuntimeError(f"record {rec} has no field {field}")
        self._d.objects[h]["records"].setdefault(rec, {})[field] = value

    def NewField(self, rec, field, default, ftype, style):
        self._d.records.setdefault(rec, [])
        if field not in self._d.records[rec]:
            self._d.records[rec].append(field)

    def DelRecord(self, h, rec):
        self._d.objects[h]["records"].pop(rec, None)

    def BuildResourceList(self, rtype, folder, sub):
        names = {47: list(self._d.records), 16: list(self._d.symbols)}.get(int(rtype)) or \
            list(self._d.resources.get(int(rtype), []))
        list_id = len(self._lists) + 1
        self._lists[list_id] = names
        self._list_types[list_id] = int(rtype)
        return list_id, len(names)

    def BuildResourceListN(self, rtype, path):
        return self.BuildResourceList(rtype, 0, "")

    def GetNameFromResourceList(self, list_id, i):
        return self._lists[list_id][i - 1]

    def ImportResToCurFileN(self, list_id, index, cb):
        return "res"

    def SaveSheet(self, name, save_view, save_class, save_layer):
        self._d.saved_views.append(name)

    def VRestore(self, name):
        if name not in self._d.saved_views:
            raise RuntimeError("no such saved view")

    def VDelete(self, name):
        if name in self._d.saved_views:
            self._d.saved_views.remove(name)


    def GetParametricRecord(self, h):
        return "pio:" + self._d.objects[h].get("plugin_name", self._d.objects[h]["type"].title())

    def CreateStory(self, name, suffix):
        self._d.stories.append({"name": name, "suffix": suffix, "elevation": 0.0})
        return True

    def SetStoryElevation(self, st, elev):
        for s in self._d.stories:
            if "story:" + s["name"] == st:
                s["elevation"] = elev

    def FLayer(self):
        return self._d.layers[0]["uuid"]

    def NextLayer(self, h):
        ids = [l["uuid"] for l in self._d.layers]
        i = ids.index(h)
        return ids[i + 1] if i + 1 < len(ids) else None

    def GetStoryOfLayer(self, h):
        return ("story:" + self._d.stories[0]["name"]) if self._d.stories else None

    def GetStorySuffix(self, st):
        return next((s["suffix"] for s in self._d.stories if "story:" + s["name"] == st), "")

    def GetStoryElevation(self, st):
        return next((s["elevation"] for s in self._d.stories if "story:" + s["name"] == st), 0.0)

    def ImportImageFile(self, path, at):
        return self._d.add("image", name=os.path.basename(path), x1=at[0], y1=at[1])["uuid"]

    # ----- units / layers (viewport + massing snippets) -------------------------
    def GetUnits(self):
        return (1, 0, 0, 25.4, "mm", " sq m")

    def GetLName(self, h):
        return next((l["name"] for l in self._d.layers if l["uuid"] == h), "")

    def GetLayerByName(self, name):
        return next((l["uuid"] for l in self._d.layers if l["name"] == name), None)

    def ActLayer(self):
        return self.GetLayerByName(self._d.active_layer)

    def Layer(self, name):
        if self.GetLayerByName(name) is None:
            self._d.layers.append({"uuid": _uuid(), "name": name, "kind": "design", "visible": "show", "scale": 50.0,
                                   "elevation": 0.0, "opacity": 100})
        self._d.active_layer = name

    def CreateLayer(self, name, kind):
        layer = {"uuid": _uuid(), "name": name, "kind": "sheet" if int(kind) == 2 else "design", "visible": "show",
                 "scale": 1.0 if int(kind) == 2 else 50.0, "elevation": 0.0, "opacity": 100}
        self._d.layers.append(layer)
        self._d.active_layer = name
        return layer["uuid"]

    def _layer(self, h):
        return next((l for l in self._d.layers if l["uuid"] == h), None)

    def GetLVis(self, h):
        return {"show": 0, "gray": 2, "hide": -1}.get(self._layer(h).get("visible"), 0)

    def GetLScale(self, h):
        return float(self._layer(h).get("scale") or 1.0)

    def FInLayer(self, h):
        name = self.GetLName(h)
        items = [u for u in self._d.order if u in self._d.objects and self._d.objects[u]["layer"] == name
                 and not self._d.objects[u].get("in_group")]
        self._iter = items
        return items[0] if items else None

    def NextObj(self, h):
        items = getattr(self, "_iter", [])
        i = items.index(h) if h in items else len(items)
        return items[i + 1] if i + 1 < len(items) else None

    def FInGroup(self, g):
        items = list(self._d.objects[g].get("members") or []) if g in self._d.objects else []
        self._iter = items
        return items[0] if items else None

    def ClassNum(self):
        return len(self._d.classes)

    def ClassList(self, i):
        return list(self._d.classes)[int(i) - 1]

    def GetCVis(self, name):
        return {"show": 0, "gray": 2, "hide": -1}.get(self._d.classes.get(name, {}).get("visibility"), 0)

    # ----- object variables + geometry ---------------------------------------------
    def _ov(self, h):
        return self._d.objects[h].setdefault("ov", {})

    def GetObjectVariableInt(self, h, index):
        layer = self._layer(h)
        if layer is not None and int(index) == 154:
            return 2 if layer["kind"] == "sheet" else 1
        return int(self._ov(h).get(int(index), 0))

    GetObjectVariableLongInt = GetObjectVariableInt

    def GetObjectVariableReal(self, h, index):
        return float(self._ov(h).get(int(index), 0.0))

    def GetObjectVariableBoolean(self, h, index):
        return bool(self._ov(h).get(int(index), False))

    def GetObjectVariableString(self, h, index):
        return str(self._ov(h).get(int(index), ""))

    def _set_ov(self, h, index, value):
        self._ov(h)[int(index)] = value

    SetObjectVariableInt = SetObjectVariableLongInt = SetObjectVariableReal = _set_ov
    SetObjectVariableBoolean = SetObjectVariableString = _set_ov

    def _new(self, otype, **fields):
        obj = self._d.add(otype, **fields)
        obj["type_id"] = {"extrude": 24, "viewport": 122, "group": 11, "column": 86}.get(otype, obj["type_id"])
        self._last = obj["uuid"]
        return obj

    def LNewObj(self):
        return getattr(self, "_last", None)

    def Rect(self, *args):
        flat = [c for a in args for c in (a if isinstance(a, (tuple, list)) else (a,))]  # POINTs or x1, y1, x2, y2
        self._new("rect", x1=float(flat[0]), y1=float(flat[1]), x2=float(flat[2]), y2=float(flat[3]))

    def BeginPoly(self):
        self._poly = []

    def AddPoint(self, p):
        self._poly.append([float(p[0]), float(p[1])])

    def EndPoly(self):
        self._new("polygon", points=self._poly, closed=True)

    def MoveTo(self, p):
        self._pen = [float(p[0]), float(p[1])]

    def LineTo(self, p):
        self._new("line", x1=self._pen[0], y1=self._pen[1], x2=float(p[0]), y2=float(p[1]))

    def TextOrigin(self, p):
        self._text_at = [float(p[0]), float(p[1])]

    def CreateText(self, text):
        at = getattr(self, "_text_at", [0.0, 0.0])
        self._new("text", text=text, x1=at[0], y1=at[1], text_size=10.0)

    def GetText(self, h):
        return self._d.objects[h].get("text", "")

    def LinearDim(self, a, b, offset, dim_type, arrow, text_flag, text_offset):
        self._new("linear_dimension", start_x=float(a[0]), start_y=float(a[1]), end_x=float(b[0]), end_y=float(b[1]),
                  offset=float(offset))

    def HExtrude(self, profile, bottom, top):
        prof = self._d.objects.pop(profile)
        self._d.order.remove(profile)
        return self._new("extrude", points=prof["points"], bottom=float(bottom), top=float(top))["uuid"]

    def Get3DInfo(self, h):
        o = self._d.objects[h]
        return (0.0, 0.0, o.get("top", 0.0) - o.get("bottom", 0.0))

    def CreateCustomObjectN(self, name, p, rotation, show_pref):
        if name != "Column2":
            return None
        obj = self._new("column", plugin_name=name, x1=float(p[0]), y1=float(p[1]), rotation=float(rotation))
        self._d.records.setdefault(name, ["OA Height", "Shaft Width", "Shaft Depth", "Diameter", "Use Capital",
                                          "Use Base", "ShaftType", "Shaft Type"])
        obj["records"][name] = {}
        return obj["uuid"]

    def GetBBox(self, h):
        o = self._d.objects[h]
        if o["type"] == "viewport":
            x, y = o["ov"].get(1024, 0.0), o["ov"].get(1025, 0.0)
            return (x - 50.0, y + 40.0), (x + 50.0, y - 40.0)
        b = o.get("bounds") or {"top_left": [0, 0], "bottom_right": [0, 0]}
        return tuple(b["top_left"]), tuple(b["bottom_right"])

    def HMove(self, h, dx, dy):
        o = self._d.objects[h]
        if o["type"] == "viewport":
            o["ov"][1024] = o["ov"].get(1024, 0.0) + float(dx)
            o["ov"][1025] = o["ov"].get(1025, 0.0) + float(dy)

    # ----- graphic attributes ---------------------------------------------------------
    def _attr(self, h):
        return self._d.objects[h].setdefault("attrs", {})

    def SetClass(self, h, name):
        self._d.objects[h]["class"] = name

    def SetPenFore(self, h, color):
        self._attr(h)["pen"] = list(color)

    def SetFillFore(self, h, color):
        self._attr(h)["fill_color"] = list(color)

    def SetLW(self, h, lw):
        self._attr(h)["lw"] = int(lw)

    def SetOpacityN(self, h, pen, fill):
        self._attr(h)["opacity"] = int(fill)
        return True

    def SetLSN(self, h, value):
        self._attr(h)["ls"] = int(value)

    def GetLSN(self, h):
        return self._attr(h).get("ls", 2)

    def SetFPat(self, h, value):
        self._attr(h)["fpat"] = int(value)
        self._attr(h).pop("hatch", None)

    def GetFPat(self, h):
        return self._attr(h).get("fpat", 1)

    def SetVectorFill(self, h, name):
        if name not in self._d.resources.get(66, []):
            return False
        self._attr(h)["hatch"] = name
        return True

    def GetVectorFill(self, h):
        name = self._attr(h).get("hatch")
        return (name is not None, name or "")

    def Name2Index(self, name):
        for rtype, names in self._d.resources.items():
            if name in names:
                return 1000 + rtype * 10 + names.index(name)
        return 0

    def BuildResourceList2(self, rtype, folder, sub, default_content):
        names = list(self._d.resources.get(int(rtype), []))
        if folder:
            names += [n for n in MOCK_LIBRARY.get(int(rtype), []) if n not in names]
        list_id = len(self._lists) + 1
        self._lists[list_id] = names
        self._list_types[list_id] = int(rtype)
        return list_id, len(names)

    def ImportResToCurFileN(self, list_id, index, cb):
        name = self._lists[list_id][int(index) - 1]
        bucket = self._d.resources.setdefault(self._list_types.get(list_id, 0), [])
        if name not in bucket:
            bucket.append(name)
        return "res:" + name

    def SetTextStyleRef(self, h, ref):
        self._attr(h)["text_style_ref"] = int(ref)

    def GetTextStyleRef(self, h):
        return self._attr(h).get("text_style_ref", 0)

    def GetFontID(self, name):
        return MOCK_FONTS.index(name) + 1 if name in MOCK_FONTS else 1

    def GetFontName(self, fid):
        return MOCK_FONTS[int(fid) - 1]

    def SetTextFont(self, h, start, count, fid):
        self._attr(h)["font"] = int(fid)

    def GetTextFont(self, h, pos):
        return self._attr(h).get("font", 1)

    def SetTextSize(self, h, start, count, size):
        self._d.objects[h]["text_size"] = float(size)

    def GetTextSize(self, h, pos):
        return float(self._d.objects[h].get("text_size", 10.0))

    def SetTextStyle(self, h, start, count, style):
        # Like Vectorworks 2024: 0 clears the face, any other value toggles those bits.
        self._attr(h)["face"] = 0 if int(style) == 0 else self._attr(h).get("face", 0) ^ int(style)

    def GetTextStyle(self, h, pos):
        return self._attr(h).get("face", 0)

    def SetTextJust(self, h, value):
        self._attr(h)["just"] = int(value)

    def GetTextJust(self, h):
        return self._attr(h).get("just", 1)

    def SetTextVerticalAlign(self, h, value):
        self._attr(h)["valign"] = int(value)

    def GetTextVerticalAlign(self, h):
        return self._attr(h).get("valign", 1)

    def _set_marker(self, key, h, style, angle, size, width, basis, thickness, visible):
        if self._d.objects[h]["type"] not in {"line", "polygon", "arc", "wall"}:
            return False
        self._attr(h)[key] = (int(style), bool(visible))
        return True

    def SetObjBeginningMarker(self, h, *args):
        return self._set_marker("m0", h, *args)

    def SetObjEndMarker(self, h, *args):
        return self._set_marker("m1", h, *args)

    def GetObjBeginningMarker(self, h):
        style, vis = self._attr(h).get("m0", (0, False))
        return (True, style, 15, 0.125, 0.0, 0, 0.0, vis)

    def GetObjEndMarker(self, h):
        style, vis = self._attr(h).get("m1", (0, False))
        return (True, style, 15, 0.125, 0.0, 0, 0.0, vis)

    # ----- viewports ------------------------------------------------------------------
    def CreateVP(self, sheet):
        layer = self._layer(sheet)
        if layer is None or layer["kind"] != "sheet":
            return None
        obj = self._new("viewport", vp_layers={}, vp_classes={}, crop=None, annotations=[])
        obj["layer"] = layer["name"]
        obj["ov"] = {1003: 1.0, 1007: 0, 1000: 0, 1001: 0, 1024: 0.0, 1025: 0.0}
        return obj["uuid"]

    def SetVPLayerVisibility(self, vp, layer, vis):
        self._d.objects[vp]["vp_layers"][self.GetLName(layer)] = int(vis)
        return True

    def GetVPLayerVisibility(self, vp, layer):
        return (True, self._d.objects[vp]["vp_layers"].get(self.GetLName(layer), -1))

    def SetVPClassVisibility(self, vp, name, vis):
        self._d.objects[vp]["vp_classes"][name] = int(vis)
        return True

    def GetVPClassVisibility(self, vp, name):
        return (True, self._d.objects[vp]["vp_classes"].get(name, -1))

    def UpdateVP(self, vp):
        self._d.objects[vp]["ov"][1004] = False

    def SetVPCropObject(self, vp, crop):
        self._d.objects[vp]["crop"] = crop
        self._d.objects[crop]["in_group"] = vp
        return True

    def GetVPCropObject(self, vp):
        return self._d.objects[vp].get("crop")

    def VPHasCropObject(self, vp):
        return bool(self._d.objects[vp].get("crop"))

    def AddVPAnnotationObject(self, vp, h):
        self._d.objects[vp]["annotations"].append(h)
        self._d.objects[h]["in_group"] = vp
        return True

    def IsVPGroupContainedObject(self, h, kind):
        return bool(self._d.objects[h].get("in_group"))

    def GetVPGroup(self, vp, kind):
        key = "group%d" % int(kind)
        o = self._d.objects[vp]
        if key not in o:
            o[key] = self._d.add("group", name="")["uuid"]
            self._d.objects[o[key]]["in_group"] = vp
        self._d.objects[o[key]]["members"] = list(o["annotations"]) if int(kind) == 2 else []
        return o[key]

    def DelObject(self, h):
        if isinstance(h, str) and h.startswith("view:"):
            self._d.saved_views.remove(h[5:])
            return
        obj = self._d.objects.pop(h, None)
        if obj is None:
            return
        for vp in self._d.objects.values():
            if vp.get("crop") == h:
                vp["crop"] = None
        if obj["type"] == "viewport":
            for u in obj.get("annotations", []) + [obj.get("crop")]:
                self._d.objects.pop(u, None)

    def ResetObject(self, h):
        pass


def _norm_name(value: Any) -> str:
    return re.sub(r"[ _\-]", "", str(value or "")).lower()


def _type_match(obj: dict, object_type: str) -> bool:
    """Native object_type filter: node type name, door/window/space, or a plug-in name (case/space-insensitive)."""
    if not object_type:
        return True
    if obj.get("type") == object_type:
        return True
    return bool(obj.get("plugin_name")) and _norm_name(obj["plugin_name"]) == _norm_name(object_type)


def _criteria_match(obj: dict, criteria: str) -> bool:
    if not criteria or criteria.strip().upper() in {"ALL", "(ALL)"}:
        return True
    for value in re.findall(r"\bPON\s*=\s*'((?:[^']|'')*)'", criteria):
        if (obj.get("plugin_name") or "") != value.replace("''", "'"):
            return False
    criteria = re.sub(r"\bPON\s*=\s*'(?:[^']|'')*'", "", criteria)
    for key, value in re.findall(r"\b([NCTL])\s*=\s*'?((?:[^'()]|'')*)'?", criteria):
        value = value.replace("''", "'")
        if key == "N" and obj.get("name") != value:
            return False
        if key == "C" and obj.get("class") != value:
            return False
        if key == "L" and obj.get("layer") != value:
            return False
        if key == "T" and obj.get("type", "").upper() != value.upper():
            return False
    return True


class Handler:
    def __init__(self, model: Model) -> None:
        self.m = model

    # ----- dispatch ------------------------------------------------------
    def dispatch(self, req: dict) -> dict:
        rid = req.get("id")
        action = req.get("action")
        params = req.get("params") or {}
        m = self.m
        m.calls.append(action)
        if req.get("auth_token") != m.token:
            return {"id": rid, "success": False, "error": "native bridge authentication failed", "error_code": "auth_failed"}
        if m.legacy and action in LEGACY_MISSING | {"mock_set_noop"}:
            return {"id": rid, "success": False, "error": f"unknown native bridge action: {action}"}
        try:
            if action == "ping":
                return self.ok(rid, self.ping())
            if action == "capabilities":
                return self.ok(rid, self.capabilities())
            if action == "stop":
                return self.ok(rid, {"connection_closed": True, "bridge_running": True}, close=True)
            if action == "job_status":
                job = m.jobs.get(params.get("job_id"))
                if job is None:
                    raise BridgeError("unknown job: " + str(params.get("job_id")), "not_found")
                return self.ok(rid, {k: v for k, v in job.items() if k != "thread"})
            if action == "cancel_job":
                job = m.jobs.get(params.get("job_id"))
                if job is None:
                    raise BridgeError("unknown job", "not_found")
                if job["state"] == "queued":
                    job["state"] = "cancelled"
                return self.ok(rid, {"job_id": job["job_id"], "state": job["state"], "cancellable": job["state"] != "running"})
            if action == "dismiss_home":
                record = None
                if m.modal_dialog and m.modal_dialog.get("kind") in {"home", "update_available"}:
                    time.sleep(0.05)
                    record = m.dismiss_dialog()
                blocked = m.modal_dialog is not None
                result = {"dismissed": record is not None, "home_dismissed": record is not None,
                          "dismissed_dialogs": [{k: record[k] for k in ("kind", "title")}] if record else [],
                          "still_shown": False, "modal_dialog": dict(m.modal_dialog) if m.modal_dialog else None,
                          "auto_dismiss_enabled": m.auto_dismiss, "ui_state": m.ui_state if m.doc else "no_document"}
                if blocked:
                    result["blocked_by"] = "other_dialog_open"
                return self.ok(rid, result)
            if action == "mock_set":
                if "modal_dialog" in params:
                    m.modal_dialog = params["modal_dialog"] or None
                    m.ui_state = "modal" if m.modal_dialog else "idle"
                if "auto_dismiss" in params:
                    m.auto_dismiss = bool(params["auto_dismiss"])
                if "drop_reply_once" in params:
                    m.drop_reply_once = params["drop_reply_once"]
                for key in ("ui_state", "latency"):
                    if key in params:
                        setattr(m, key, params[key])
                if params.get("switch_to_new_document"):
                    m.docs.append(Doc(f"Untitled {len(m.docs) + 1}"))
                    m.active = len(m.docs) - 1
                return self.ok(rid, {"ui_state": m.ui_state, "latency": m.latency})
            if action not in MAIN_ACTIONS:
                return {"id": rid, "success": False, "error": f"unknown native bridge action: {action}"}
            if params.get("async") and action in ASYNC_ACTIONS and not m.legacy:
                return self.ok(rid, self.start_job(action, params))
            return self.ok(rid, self.main(action, params, req.get("timeout_ms")))
        except BridgeError as exc:
            reply = {"id": rid, "success": False, "error": str(exc)}
            if not m.legacy:
                for key, value in (("error_code", exc.code), ("retryable", exc.retryable),
                                   ("commit_state", exc.commit_state), ("details", exc.details)):
                    if value is not None:
                        reply[key] = value
            return reply
        except Exception as exc:  # noqa: BLE001
            return {"id": rid, "success": False, "error": f"VW Error (mock): {exc}"}

    def ok(self, rid, result, close: bool = False) -> dict:
        out = {"id": rid, "success": True, "result": result}
        if close:
            out["_close"] = True
        return out

    def ping(self) -> dict:
        m = self.m
        doc = m.doc
        base = {"pong": True, "version": "mock", "bridge_kind": "native_sdk_bridge_mock", "dispatch_mode": "native_sdk",
                "native_bridge": True, "cad_api_safe": True, "transport_only": False, "native_phase": 4,
                "main_context_pump_ready": True, "main_context_idle_ms": 5,
                "capability_fingerprint": "mockfp", "object_read_features": ["text_content", "paged_object_reads", "bound_apply_operations"]
                + ([] if m.legacy else ["wall_inserts", "object_type_plugin_names", "uuid_lookup"]),
                "create_object_types": ["arc", "box", "circle", "line", "oval", "polygon", "polyline", "rect", "rectangle", "wall",
                                        "door", "window", "text", "dimension", "linear_dimension", "slab", "roof", "space",
                                        "parametric", "symbol"],
                "implemented_actions": sorted(self.actions())}
        if m.legacy:
            base.update({"capability_revision": 5, "bridge_session_id": m.session})
            return base
        base.update({"protocol": 2, "capability_revision": 6, "bridge_version": "1.0.0-mock", "build_id": "mock-0000",
                     "pid": os.getpid(), "port": m.port, "session_id": m.session, "vectorworks_version": "2024",
                     "ui_state": m.ui_state if doc else "no_document", "handler_running_ms": 0, "queue_depth": 0,
                     "active_document": {k: v for k, v in doc.brief().items() if k != "active"} if doc else None,
                     "open_document_count": len(m.docs),
                     "script_enabled": os.environ.get("VW_MCP_ALLOW_SCRIPT") != "0",
                     "script_engine": "poisoned" if getattr(m, "script_poisoned", False) else "ok",
                     "modal_dialog": dict(m.modal_dialog) if m.modal_dialog else None,
                     "auto_dismiss_dialogs": m.auto_dismiss, "dismissed_dialogs": list(m.dismissed)})
        return base

    def actions(self) -> set:
        names = MAIN_ACTIONS | {"ping", "capabilities", "stop", "job_status", "cancel_job", "dismiss_home"}
        return names - LEGACY_MISSING if self.m.legacy else names

    def capabilities(self) -> dict:
        caps = {"capability_revision": 5 if self.m.legacy else 6, "capability_fingerprint": "mockfp"}
        if not self.m.legacy:
            caps["actions"] = [{"name": a, "mutates": a not in {"ping", "capabilities", "get_document_info", "query_objects"},
                                "long_running": a in ASYNC_ACTIONS, "supports_async": a in ASYNC_ACTIONS}
                               for a in sorted(self.actions())]
        return caps

    def start_job(self, action: str, params: dict) -> dict:
        job_id = "job-" + uuid.uuid4().hex[:8]
        job = {"job_id": job_id, "action": action, "state": "queued", "started_at": time.time(), "elapsed_ms": 0}
        self.m.jobs[job_id] = job

        def run():
            time.sleep(0.3)
            if job["state"] == "cancelled":
                return
            job["state"] = "running"
            try:
                p = dict(params)
                p.pop("async", None)
                job["result"] = self.main(action, p, None)
                job["state"] = "done"
            except BridgeError as exc:
                job.update({"state": "failed", "error": str(exc), "error_code": exc.code})
            except Exception as exc:  # noqa: BLE001
                job.update({"state": "failed", "error": str(exc)})
            job["elapsed_ms"] = int((time.time() - job["started_at"]) * 1000)

        threading.Thread(target=run, daemon=True).start()
        return {"job_id": job_id, "state": "queued"}

    def main(self, action: str, params: dict, timeout_ms: Optional[int]) -> Any:
        m = self.m
        busy_wait = (params.get("busy_wait_ms") or 3000) / 1000.0
        deadline = time.time() + busy_wait
        auto_closed = m.dismiss_dialog() if m.auto_dismiss and m.modal_dialog else None
        while m.ui_state not in {"idle", "no_document"}:
            if time.time() > deadline:
                details = {"ui_state": m.ui_state}
                if m.modal_dialog:
                    details["modal_dialog"] = dict(m.modal_dialog)
                raise BridgeError(f"Vectorworks is busy ({m.ui_state})", "vectorworks_busy", True, "not_started", details)
            time.sleep(0.05)
        if auto_closed:
            result = self._main_locked(action, params, timeout_ms)
            if isinstance(result, dict):
                result = dict(result, home_dismissed=True,
                              dismissed_dialogs=[{"kind": auto_closed["kind"], "title": auto_closed["title"]}])
            return result
        return self._main_locked(action, params, timeout_ms)

    def _main_locked(self, action: str, params: dict, timeout_ms: Optional[int]) -> Any:
        m = self.m
        with m.main_lock:
            if m.latency:
                if timeout_ms is not None and m.latency * 1000 > timeout_ms and action in ASYNC_ACTIONS:
                    job = self.start_job(action, params)
                    raise BridgeError("still running", "still_running", True, "unknown", {"job_id": job["job_id"]})
                time.sleep(m.latency)
            if m.doc is None and action not in NO_DOC_ACTIONS:
                raise BridgeError("no active document", "no_active_document", True, "not_started")
            self.check_document(params)
            return getattr(self, "a_" + action)(params)

    def check_document(self, params: dict) -> None:
        m = self.m
        expected = params.get("expect_document_fingerprint")
        if expected and m.doc is not None and m.doc.fingerprint != expected:
            target = next((i for i, d in enumerate(m.docs) if d.fingerprint == expected), None)
            if params.get("activate_document") and target is not None:
                m.active = target
                return
            raise BridgeError("the active document changed", "document_changed", True, "not_started",
                              {"active_document": m.doc.brief(True),
                               "open_documents": [d.brief(i == m.active) for i, d in enumerate(m.docs)]})
        if params.get("expected_document_fingerprint") and m.doc and params["expected_document_fingerprint"] != m.doc.fingerprint:
            raise BridgeError("target binding changed: document fingerprint differs")
        if params.get("expected_document_generation") and m.doc and params["expected_document_generation"] != m.doc.generation:
            raise BridgeError("target binding changed: document generation differs")

    # ----- documents -------------------------------------------------------
    def a_get_document_info(self, p):
        return self.m.doc.info(self.m.session)

    def a_list_documents(self, p):
        return {"documents": [d.brief(i == self.m.active) | {"file_ref": i} for i, d in enumerate(self.m.docs)]}

    def a_activate_document(self, p):
        for i, d in enumerate(self.m.docs):
            if p.get("fingerprint") in (d.fingerprint,) or p.get("name") == d.name or (p.get("path") and p.get("path") == d.path):
                self.m.active = i
                return {"active_document": d.brief(True)}
        raise BridgeError("document not found", "not_found")

    def a_new_document(self, p):
        d = Doc(f"Untitled {len(self.m.docs) + 1}")
        self.m.docs.append(d)
        self.m.active = len(self.m.docs) - 1
        return {"active_document": d.brief(True)}

    def a_close_document(self, p):
        for i, d in enumerate(self.m.docs):
            if d.fingerprint == p.get("fingerprint"):
                if d.dirty and p.get("discard_changes") != "discard":
                    raise BridgeError("document has unsaved changes", "confirmation_required", False, "not_started")
                self.m.docs.pop(i)
                self.m.active = max(0, min(self.m.active, len(self.m.docs) - 1))
                return {"closed": d.fingerprint}
        raise BridgeError("document not found", "not_found")

    def a_open_document(self, p):
        path = p.get("file_path") or p.get("path")
        if not path or not os.path.exists(path):
            raise BridgeError("input file not found", "input_not_found")
        for i, d in enumerate(self.m.docs):
            if d.path == path:
                self.m.active = i
                return {"active_document": d.brief(True), "commit_state": "committed"}
        d = Doc(os.path.basename(path), path)
        self.m.docs.append(d)
        self.m.active = len(self.m.docs) - 1
        return {"active_document": d.brief(True), "commit_state": "committed"}

    def a_save_document(self, p):
        d = self.m.doc
        path = p.get("file_path") or p.get("path") or d.path
        if not path:
            path = str(Path(os.environ.get("TEMP", ".")) / (d.name + ".vwx"))
        Path(path).write_bytes(b"MOCKVWX")
        d.path, d.name, d.dirty = path, os.path.basename(path), False
        return {"saved": True, "path": path, "commit_state": "committed"}

    def _export(self, p, data: bytes):
        path = p.get("file_path") or p.get("path")
        if not path or not os.path.isdir(os.path.dirname(path)):
            raise BridgeError("output folder does not exist", "invalid_path")
        if os.path.exists(path) and not p.get("replace"):
            raise BridgeError("output exists", "confirmation_required")
        Path(path).write_bytes(data)
        return {"path": path, "bytes": len(data), "commit_state": "committed"}

    def a_export_pdf(self, p):
        return self._export(p, b"%PDF-1.4 mock " + json.dumps({k: p.get(k) for k in ("range", "sheet", "dpi")}).encode())

    def a_export_dwg(self, p):
        return self._export(p, b"AC1032 mock")

    def a_export_ifc(self, p):
        return self._export(p, b"ISO-10303-21; mock")

    def a_export_image(self, p):
        path = p.get("file_path") or p.get("path")
        return {"path": path, "bytes": _png(path, 64, 48)}

    def a_export_vectorworks_document(self, p):
        return self._export(p, b"MOCKVWX")

    def _import(self, p):
        d = self.m.doc
        obj = d.add("group", name="Imported " + os.path.basename(p.get("file_path") or ""))
        d.touch()
        return {"imported": True, "object": d.record(obj)}

    a_import_dwg = _import
    a_import_ifc = _import

    # ----- organisation ------------------------------------------------------
    def a_get_layers(self, p):
        return [{"name": l["name"], "visible": l["visible"] == "show", "uuid": l["uuid"]} for l in self.m.doc.layers if l["kind"] == "design"]

    def a_layers(self, p):
        d = self.m.doc
        action = p.get("action", "list")
        if action == "list":
            items = []
            for l in d.layers:
                if p.get("kind") and l["kind"] != p["kind"]:
                    continue
                items.append(dict(l, active=l["name"] == d.active_layer,
                                  object_count=sum(1 for o in d.objects.values() if o["layer"] == l["name"])))
            return {"layers": items}
        if action == "create":
            if any(l["name"] == p["name"] for l in d.layers):
                raise BridgeError("a layer with this name exists", "invalid_request")
            layer = {"uuid": _uuid(), "name": p["name"], "kind": p.get("kind", "design"), "visible": "show",
                     "scale": p.get("scale", 50.0), "elevation": p.get("elevation", 0.0), "opacity": 100}
            d.layers.append(layer)
            d.touch()
            return {"layer": layer}
        layer = next((l for l in d.layers if p.get("target") in (l["name"], l["uuid"])), None)
        if layer is None:
            raise BridgeError("layer not found", "not_found")
        if action == "set":
            for key, wire in (("name", "name"), ("visibility", "visible"), ("scale", "scale"), ("elevation", "elevation"),
                              ("opacity", "opacity")):
                if key in p:
                    layer[wire] = p[key]
            if p.get("active"):
                d.active_layer = layer["name"]
            d.touch()
            return {"layer": layer}
        if action == "delete":
            if p.get("confirm") != "delete":
                raise BridgeError("confirm required", "confirmation_required")
            count = sum(1 for o in d.objects.values() if o["layer"] == layer["name"])
            if count and not p.get("force"):
                raise BridgeError(f"layer has {count} objects; pass force", "invalid_request")
            d.layers.remove(layer)
            d.touch()
            return {"deleted": layer["name"]}
        raise BridgeError("bad layers action", "invalid_request")

    def a_manage_classes(self, p):
        d = self.m.doc
        action = p.get("action", "list")
        if action == "list":
            return {"classes": [{"name": n, **v} for n, v in d.classes.items()]}
        if action == "create":
            d.classes.setdefault(p["class_name"], {"visibility": "show", "active": False})
            d.touch()
            return {"class_name": p["class_name"], "created": True}
        if action == "delete":
            if p.get("confirm") != "DELETE_CLASS":
                raise BridgeError("confirm DELETE_CLASS required")
            d.classes.pop(p["class_name"], None)
            d.touch()
            return {"deleted": True}
        if action == "set":
            if p["name"] not in d.classes:
                raise BridgeError("class not found", "not_found")
            d.classes[p["name"]].update({k: v for k, v in p.items() if k not in {"action", "name"}})
            d.touch()
            return {"class": p["name"], **d.classes[p["name"]]}
        raise BridgeError("bad action")

    def a_stories(self, p):
        d = self.m.doc
        if p.get("action") == "list":
            return {"stories": d.stories}
        if p.get("action") == "create":
            d.stories.append({"name": p["name"], "elevation": p.get("elevation", 0.0)})
            return {"created": p["name"]}
        story = next((s for s in d.stories if s["name"] == p.get("name")), None)
        if story is None:
            raise BridgeError("story not found", "not_found")
        story.update({k: v for k, v in p.items() if k in {"elevation"}})
        return {"updated": p["name"]}

    def a_saved_views(self, p):
        d = self.m.doc
        action = p.get("action", "list")
        if action == "list":
            return {"saved_views": [{"name": n} for n in d.saved_views]}
        if action == "create":
            d.saved_views.append(p["name"])
            return {"created": p["name"]}
        if p.get("name") not in d.saved_views:
            raise BridgeError("saved view not found", "not_found")
        if action == "delete":
            d.saved_views.remove(p["name"])
            return {"deleted": p["name"]}
        return {"restored": p["name"]}

    # ----- reads -------------------------------------------------------------
    def a_drawing_summary(self, p):
        d = self.m.doc
        objs = [o for o in d.objects.values() if (not p.get("layer") or o["layer"] == p["layer"]) and
                _type_match(o, p.get("object_type") or "")]
        def count(key):
            out: dict = {}
            for o in objs:
                out[o[key]] = out.get(o[key], 0) + 1
            return out
        return {"total_objects": len(objs), "counts_by_type": count("type"), "counts_by_layer": count("layer"),
                "counts_by_class": count("class"), "timing": {"handler_ms": 0.1}}

    def a_query_objects(self, p):
        d = self.m.doc
        mode = p.get("mode", "objects")
        fields = {p.get(f"field_{i}") for i in range(1, int(p.get("field_count", 0)) + 1)} or None
        if fields:
            fields |= {"uuid"}
        items = []
        wanted = p.get("uuids") if p.get("uuids") is not None else p.get("uuid")
        if wanted is not None:
            wanted = [w for w in (wanted if isinstance(wanted, list) else str(wanted).split(",")) if w]
            wanted = [str(w).strip()[5:] if str(w).strip().startswith("uuid:") else str(w).strip() for w in wanted]
            found = [d.objects[w.upper()] for w in wanted if w.upper() in d.objects]
            found = [o for o in found if _type_match(o, p.get("object_type") or "")
                     and (not p.get("layer") or o["layer"] == p["layer"])]
            return {"items": [d.record(o, fields) for o in found], "has_more": False, "offset": 0,
                    "missing_uuids": [w for w in wanted if w.upper() not in d.objects],
                    "binding": d.info(self.m.session)["binding"]}
        for u in d.order:
            o = d.objects.get(u)
            if o is None:
                continue
            if mode == "selection" and u not in d.selection:
                continue
            if mode == "criteria" and not _criteria_match(o, p.get("criteria", "ALL")):
                continue
            if not _type_match(o, p.get("object_type") or ""):
                continue
            if p.get("layer") and o["layer"] != p["layer"]:
                continue
            items.append(o)
        offset, limit = int(p.get("offset", 0)), int(p.get("limit", 100))
        page = items[offset:offset + limit]
        return {"items": [d.record(o, fields) for o in page], "has_more": len(items) > offset + limit, "offset": offset,
                "binding": d.info(self.m.session)["binding"]}

    def a_selection(self, p):
        d = self.m.doc
        action = p.get("action", "get")
        if action == "clear":
            d.selection.clear()
            return {"cleared": True}
        if action == "select":
            for o in d.objects.values():
                if _criteria_match(o, p.get("criteria", "")):
                    d.selection.add(o["uuid"])
            return {"selected": len(d.selection)}
        return [d.record(d.objects[u]) for u in d.selection]

    def a_describe_parametric_schema(self, p):
        name = p.get("plugin_name")
        params = {"Door": [{"id": "DoorWidth", "display_name": "Leaf Width (Read Only)", "field_style": 7, "value": "826"},
                           {"id": "Width", "display_name": "Width", "field_style": 7, "value": "915"},
                           {"id": "OpenAngle", "display_name": "Open Angle", "field_style": 3, "value": "90"},
                           {"id": "Config", "field_style": 8, "value": "Swing"}],
                  "Window": [{"id": "SashOp", "field_style": 8, "value": "Fixed"},
                             {"id": "Width", "display_name": "Width", "field_style": 7, "value": "900"}],
                  "Space": [{"id": "Name", "field_style": 4, "value": ""}],
                  "Stair": [{"id": "TotalRise", "display_name": "Total Rise", "field_style": 7, "value": "3000"},
                            {"id": "NumRisers", "display_name": "Number of Risers", "field_style": 1, "value": "17"},
                            {"id": "StairType", "display_name": "Stair Type", "field_style": 8, "value": "Straight"}]}.get(name)
        if params is None:
            raise BridgeError(f"plug-in not found: {name}", "not_found")
        return {"universal_plugin_name": name, "descriptor_fingerprint": "fp-" + name.lower(), "parameters": params}

    def _check_parametric(self, plugin: str, op: dict) -> dict:
        """Mirror the native readback: read-only or wrongly typed values fail 'parametric readback mismatch'."""
        try:
            schema = {e["id"]: e for e in self.a_describe_parametric_schema({"plugin_name": plugin})["parameters"]}
        except BridgeError:
            schema = {}
        values = {}
        for n in range(1, int(op.get("parameter_count", 0)) + 1):
            pid, ptype = op[f"parameter_{n}_name"], op[f"parameter_{n}_type"]
            value = op.get(f"parameter_{n}_{ptype}")
            entry = schema.get(pid)
            if entry is not None:
                read_only = "read only" in str(entry.get("display_name", "")).lower()
                numeric = entry.get("field_style") in {1, 3, 7, 9, 10, 11, 13}
                text = entry.get("field_style") in {4, 8}
                if read_only or (numeric and ptype not in {"real", "integer"}) or (text and ptype != "string"):
                    raise BridgeError(f"Vectorworks parametric readback mismatch: {pid}", "sdk_operation_failed",
                                      False, "rolled_back")
            values[pid] = value
        return values

    def a_transaction_status(self, p):
        rec = self.m.transactions.get(p.get("idempotency_key"))
        if rec is None:
            return {"state": "unknown"}
        return {"state": "committed", "recorded_result": rec}

    def a_symbol(self, p):
        d = self.m.doc
        names = [s for s in d.symbols if (p.get("query") or "").lower() in s.lower()]
        offset, limit = int(p.get("offset", 0)), int(p.get("limit", 1000))
        return {"symbols": [{"name": n} for n in names[offset:offset + limit]],
                "next_cursor": str(offset + limit) if offset + limit < len(names) else None}

    def a_worksheet(self, p):
        d = self.m.doc
        if p.get("action", "list") == "list":
            return {"worksheets": [{"name": n, "rows": w["rows"], "columns": w["columns"]} for n, w in d.worksheets.items()]}
        ws = d.worksheets.get(p.get("worksheet_name"))
        if ws is None:
            raise BridgeError("worksheet was not found by exact name", "not_found")
        key = (int(p["row"]), int(p["column"]))
        if p["action"] == "read":
            value = ws["cells"].get(key, "")
            return {"worksheet": p["worksheet_name"], "row": key[0], "column": key[1], "formula": value, "displayed_value": value}
        before = ws["cells"].get(key, "")
        ws["cells"][key] = p.get("formula", "")
        d.touch()
        return {"verified": True, "before": {"formula": before}, "after": {"formula": ws["cells"][key]}}

    def a_resources(self, p):
        d = self.m.doc
        return {"symbols": [{"name": s} for s in d.symbols],
                "worksheets": [{"name": n} for n in d.worksheets]}

    def a_get_sheet_layers(self, p):
        d = self.m.doc
        items = [{"uuid": l["uuid"], "name": l["name"]} for l in d.layers if l["kind"] == "sheet"]
        return {"items": items, "next_cursor": None, "binding": d.info(self.m.session)["binding"]}

    def a_get_viewports(self, p):
        d = self.m.doc
        sheet = next((l["name"] for l in d.layers if l["uuid"] == p.get("sheet_layer_uuid")), None)
        if sheet is None:
            raise BridgeError("sheet layer not found", "not_found")
        items = [{"uuid": o["uuid"], "name": o["name"], "scale": o["ov"].get(1003)} for o in d.objects.values()
                 if o["type"] == "viewport" and o["layer"] == sheet]
        return {"items": items, "next_cursor": None}

    def a_get_viewport_annotations(self, p):
        return {"items": [], "next_cursor": None}

    # ----- view ---------------------------------------------------------------
    def a_get_view(self, p):
        d = self.m.doc
        layer = next(l for l in d.layers if l["name"] == d.active_layer)
        return dict(d.view, active_layer=d.active_layer, active_layer_kind=layer["kind"], screen_px=[1600, 900], dpi=96)

    def a_set_view(self, p):
        d = self.m.doc
        previous = self.a_get_view({})
        for key in ("standard_view", "projection", "render_mode", "zoom", "center"):
            if key in p:
                d.view[key] = p[key]
        if p.get("layer"):
            d.active_layer = p["layer"]
        if p.get("fit"):
            d.view["fit"] = p["fit"]
        if p.get("fit_to_objects"):
            d.selection.clear()
        if self.m.legacy:
            return {"standard_view": d.view["standard_view"], "projection": d.view["projection"],
                    "render_mode": d.view["render_mode"], "fit_to_objects_applied": bool(p.get("fit_to_objects"))}
        return {"previous": previous, "current": self.a_get_view({})}

    def a_capture_view(self, p):
        path = p.get("path") or p.get("file_path")
        if not path or not os.path.isdir(os.path.dirname(path)):
            raise BridgeError("output folder does not exist", "invalid_path")
        width, height = (1600, 1000) if self.m.legacy else (2400, 1600)
        size = _png(path, width, height)
        return {"path": path, "width": width, "height": height, "bytes": size, "format": p.get("format", "png"),
                "restored": bool(p.get("restore")) and not self.m.legacy,
                "view_used": {k: p.get(k) for k in ("fit", "standard_view", "render_mode", "layer") if p.get(k) is not None}}

    # ----- writes ------------------------------------------------------------
    def a_apply_operations(self, p):
        m = self.m
        d = m.doc
        key = p.get("idempotency_key")
        count = int(p.get("operation_count", 0))
        ops = [json.loads(p[f"operation_{i}_json"]) for i in range(1, count + 1)]
        if key in m.transactions:
            return dict(m.transactions[key], replayed=True)
        snapshot = copy.deepcopy((d.objects, d.order))
        local: dict[str, str] = {}
        receipts = []
        touched: set[str] = set()
        plan_join = p.get("join_walls", True) is not False
        new_walls: list = []
        no_join: set[str] = set()

        def resolve(ref: str) -> dict:
            if ref.startswith("$"):
                if ref[1:] not in local:
                    raise BridgeError("target references an unknown local_ref: " + ref[1:])
                return d.objects[local[ref[1:]]]
            if ref.startswith("uuid:") and ref[5:].upper() in d.objects:
                return d.objects[ref[5:].upper()]
            if ref.startswith("name:"):
                hits = [o for o in d.objects.values() if o.get("name") == ref[5:]]
                if len(hits) == 1:
                    return hits[0]
            raise BridgeError("stale or unknown reference: " + ref, "stale_reference")

        try:
            for i, op in enumerate(ops, 1):
                kind = op.get("op")
                if kind == "create":
                    params = {k: v for k, v in op.items() if k not in {"op", "local_ref", "object_type"}}
                    otype = op["object_type"]
                    if otype in {"door", "window"} or (otype == "parametric" and params.get("wall_uuid")):
                        wall = params.get("wall_uuid", "")
                        host = resolve("$" + wall[1:] if wall.startswith("$") else "uuid:" + wall)
                        if host["type"] != "wall":
                            raise BridgeError("wall_uuid does not reference a wall")
                        params["wall_uuid"] = host["uuid"]
                        params["parent_uuid"] = host["uuid"]
                        params["host"] = "wall"
                    if otype in {"door", "window"}:
                        if params.get("descriptor_fingerprint") != "fp-" + otype:
                            raise BridgeError("descriptor_fingerprint mismatch")
                        params["plugin_name"] = otype.title()
                    if otype in {"door", "window", "parametric"}:
                        values = self._check_parametric(params.get("plugin_name", ""), params)
                        params = {k: v for k, v in params.items() if not k.startswith("parameter_")}
                        if values:
                            params["parameters"] = values
                    if otype == "wall":
                        params.pop("join_walls", None)
                        if op.get("join_walls", True) and plan_join:
                            new_walls.append(None)  # placeholder, filled below
                    obj = d.add(otype, **params)
                    if otype == "wall" and new_walls and new_walls[-1] is None:
                        new_walls[-1] = obj
                    elif otype == "wall":
                        no_join.add(obj["uuid"])
                    if op.get("local_ref"):
                        local[op["local_ref"]] = obj["uuid"]
                    receipt = {"index": i, "op": "create", "local_ref": op.get("local_ref"), "uuid": obj["uuid"],
                               "type": otype, "verified": True}
                    if params.get("parent_uuid"):
                        receipt["parent_uuid"] = params["parent_uuid"]
                    receipts.append(receipt)
                    touched.add(obj["uuid"])
                elif kind == "set_property":
                    obj = resolve(op["target"])
                    name = op["property_name"]
                    obj["class" if name == "class" else name] = op["value"]
                    touched.add(obj["uuid"])
                    receipts.append({"index": i, "op": kind, "uuid": obj["uuid"], "verified": True})
                elif kind == "object.transform":
                    obj = resolve(op["target"])
                    for kx, ky in (("x1", "y1"), ("x2", "y2"), ("start_x", "start_y"), ("end_x", "end_y")):
                        if kx in obj:
                            obj[kx] += op.get("delta_x", 0)
                            obj[ky] += op.get("delta_y", 0)
                    obj["rotation"] = obj.get("rotation", 0) + op.get("rotation_degrees", 0)
                    obj["bounds"] = _bounds(obj)
                    touched.add(obj["uuid"])
                    receipts.append({"index": i, "op": kind, "uuid": obj["uuid"], "verified": True})
                elif kind == "object.reshape":
                    obj = resolve(op["target"])
                    obj.update({k: op[k] for k in ("start_x", "start_y", "end_x", "end_y")})
                    receipts.append({"index": i, "op": kind, "uuid": obj["uuid"], "verified": True,
                                     "after_endpoints": [[op["start_x"], op["start_y"]], [op["end_x"], op["end_y"]]]})
                elif kind == "object.duplicate":
                    obj = resolve(op["target"])
                    new = d.add(obj["type"], **{k: v for k, v in obj.items() if k not in {"uuid", "type", "records", "bounds", "type_id"}})
                    for kx, ky in (("x1", "y1"), ("x2", "y2"), ("start_x", "start_y"), ("end_x", "end_y")):
                        if kx in new:
                            new[kx] += op.get("delta_x", 0)
                            new[ky] += op.get("delta_y", 0)
                    if op.get("local_ref"):
                        local[op["local_ref"]] = new["uuid"]
                    receipts.append({"index": i, "op": kind, "local_ref": op.get("local_ref"), "verified": True,
                                     "duplicate": {"uuid": new["uuid"], "type": new["type"]}})
                elif kind == "object.delete":
                    obj = resolve(op["target"])
                    inserts = [o["uuid"] for o in d.objects.values() if o.get("parent_uuid") == obj["uuid"]]
                    if obj["uuid"] in touched or any(u in touched for u in inserts):
                        raise BridgeError(f"operations[{i}] deletes an object that this plan created or changed; "
                                          "split it into two calls", "invalid_request", False, "not_started")
                    for u in inserts:
                        del d.objects[u]
                    del d.objects[obj["uuid"]]
                    receipt = {"index": i, "op": kind, "target": obj["uuid"], "verified": True}
                    if inserts:
                        receipt["cascaded_inserts"] = inserts
                    receipts.append(receipt)
                elif kind == "object.update_parametric":
                    obj = resolve(op["target"])
                    obj.setdefault("parameters", {})
                    for n in range(1, int(op.get("parameter_count", 0)) + 1):
                        ptype = op[f"parameter_{n}_type"]
                        obj["parameters"][op[f"parameter_{n}_name"]] = op[f"parameter_{n}_{ptype}"]
                    touched.add(obj["uuid"])
                    receipts.append({"index": i, "op": kind, "uuid": obj["uuid"], "verified": True})
                else:
                    raise BridgeError(f"operations[{i}].op is unsupported: {kind}")
            wall_joins = _join_walls([w for w in new_walls if w], d, no_join) if plan_join else []
        except Exception:
            d.objects, d.order = snapshot
            raise
        d.touch()
        result = {"committed": True, "verified": True, "operation_count": count, "operations": receipts,
                  "wall_joins": wall_joins, "timing": {"handler_ms": 1.0}}
        result["transaction"] = {"committed": True, "operation_count": count, "operations": receipts,
                                 "wall_joins": wall_joins}
        if key:
            m.transactions[key] = result
        return result

    def a_run_script(self, p):
        m = self.m
        d = m.doc
        if os.environ.get("VW_MCP_ALLOW_SCRIPT") == "0":
            raise BridgeError("scripting disabled", "script_disabled")
        restart = {"script_engine": "poisoned", "exception_code": "0xC0000005",
                   "next_step": "save your work and restart Vectorworks"}
        if getattr(m, "script_poisoned", False):
            raise BridgeError("the Vectorworks scripting engine is disabled after an earlier script crash (0xC0000005); "
                              "save your work and restart Vectorworks", "script_engine_unavailable", False,
                              "not_started", restart)
        if "__vw_mcp_mock_crash__" in str(p.get("code", "")):
            # Emulates the native SEH guard catching an access violation in a vs.* call.
            m.script_poisoned = True
            raise BridgeError("the script crashed inside Vectorworks (access violation 0xC0000005 in the Python "
                              "engine); scripting is disabled for the rest of this Vectorworks session and the "
                              "document state is unknown; save your work and restart Vectorworks", "script_crashed",
                              False, "unknown", restart)
        snapshot = copy.deepcopy(m.docs)
        vs = FakeVS(m)
        scope: dict = {"vs": vs}
        out, err = io.StringIO(), io.StringIO()
        started = time.time()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exec(compile(p.get("code", ""), "<mcp-script>", "exec"), scope)
        except Exception as exc:  # noqa: BLE001
            if p.get("transaction", True):
                m.docs = snapshot
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "stdout": out.getvalue(),
                    "stderr": err.getvalue() + traceback.format_exc(), "rolled_back": bool(p.get("transaction", True)),
                    "duration_ms": int((time.time() - started) * 1000)}
        if p.get("transaction", True) and d is not None:
            d.touch()
        result = scope.get("result")
        try:
            json.dumps(result)
        except (TypeError, ValueError):
            result = repr(result)
        return {"ok": True, "result": result, "stdout": out.getvalue(), "stderr": err.getvalue(), "rolled_back": False,
                "duration_ms": int((time.time() - started) * 1000)}


def serve(port: int, token: str, legacy: bool, state_dir: Optional[str]) -> None:
    model = Model(legacy, token)
    handler = Handler(model)

    class TCPHandler(socketserver.BaseRequestHandler):
        def handle(self):
            with model.lock:
                model.clients += 1
                over = model.clients > model.max_clients
            try:
                if over:
                    self._send({"id": None, "success": False, "error": "too many clients", "error_code": "too_many_clients",
                                "retryable": True})
                    return
                sock = self.request
                sock.settimeout(120)
                while True:
                    header = self._recv(4)
                    if header is None:
                        return
                    size = struct.unpack(">I", header)[0]
                    body = self._recv(size)
                    if body is None:
                        return
                    try:
                        req = json.loads(body.decode("utf-8"))
                    except ValueError:
                        self._send({"id": None, "success": False, "error": "invalid JSON"})
                        continue
                    reply = handler.dispatch(req)
                    if model.drop_reply_once and req.get("action") == model.drop_reply_once:
                        model.drop_reply_once = None
                        return  # executed, but the reply is "lost"
                    close = reply.pop("_close", False)
                    self._send(reply)
                    if close:
                        return
            except (OSError, socket.timeout):
                return
            finally:
                with model.lock:
                    model.clients -= 1

        def _recv(self, n):
            data = b""
            while len(data) < n:
                chunk = self.request.recv(n - len(data))
                if not chunk:
                    return None
                data += chunk
            return data

        def _send(self, obj):
            body = json.dumps(obj).encode("utf-8")
            self.request.sendall(struct.pack(">I", len(body)) + body)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = False

    server = Server(("127.0.0.1", port), TCPHandler)
    model.port = server.server_address[1]
    discovery: Optional[Path] = None
    if state_dir and not legacy:
        directory = Path(state_dir) / "instances"
        directory.mkdir(parents=True, exist_ok=True)
        discovery = directory / f"{os.getpid()}.json"
        tmp = discovery.with_suffix(".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "port": model.port, "protocol": 2, "capability_revision": 6,
                                   "bridge_version": "1.0.0-mock", "build_id": "mock-0000", "vectorworks_version": "2024",
                                   "session_id": model.session, "started_at": time.time()}), encoding="utf-8")
        os.replace(tmp, discovery)
    print(f"READY {model.port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        if discovery is not None:
            with contextlib.suppress(OSError):
                discovery.unlink()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token", default="mock-token")
    parser.add_argument("--legacy", action="store_true")
    parser.add_argument("--state-dir", default="")
    args = parser.parse_args()
    try:
        serve(args.port, args.token, args.legacy, args.state_dir or None)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
