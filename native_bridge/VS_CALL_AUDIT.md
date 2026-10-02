# vs.* call audit (2026-10-02)

Trigger: a live test crashed Vectorworks 2024 (access violation inside
`Vectorworks2024.exe`, called from `python39.dll`) in native `manage_classes set`,
which generated `vs.SetClFillFore(_c, vs.RGBToColorIndex(r, g, b))`. The SDK stub
(`vs.py`) declares `SetClFillFore(className, color)` with `color: COLOR` = one
`(r, g, b)` tuple, components 0..65535. Wrong-typed `vs.*` arguments are a crash
class, not a Python error.

Scope: every `vs.*` call generated or shipped by the project on the 1.0 path:

- Python built inside C++ strings: `native_bridge/src/OrganizationHandlers.cpp`,
  `native_bridge/src/ViewCapture.cpp` (audited by hand below);
- host snippets `vectorworks_mcp/scripts_lib/*.py` (+ `_prelude.py`), inline
  Python in `vectorworks_mcp/tools/edit.py`, the `vw_script` example in
  `vectorworks_mcp/catalog.py`, and the `vw_script` examples in
  `plugins/vectorworks/skills/vectorworks/SKILL.md` — machine-checked by
  `py -3.12 scripts/lint-vs-snippets.py` (same lint as `vw_script`).

Reference: SDK 2024 stub `third_party/VectorworksSDK/2024/SDK/SDKVW(750359)/SDKLib/Include/vs.py`,
cross-checked against the 2025 stub (no signature differences for any function
below). Machine-readable table: `vectorworks_mcp/data/vs_signatures.json`
(`scripts/gen-vs-signatures.py`). Types: COLOR = `(r, g, b)` 0..65535, POINT =
`(x, y)` (or `x, y` expanded), `DYNARRAY of CHAR` = STRING.

Status: **FIXED** = was wrong, corrected in this change; **OK** = matches the
stub; **NOTE** = signature correct, behavioural caveat recorded.

## Bugs found

| # | Call site | Was | Problem | Fix |
|---|---|---|---|---|
| 1 | `OrganizationHandlers.cpp` `HandleClassesExtension` set (`fill_color`) | `vs.SetClFillFore(_c, vs.RGBToColorIndex(r, g, b))` | int palette index into COLOR -> **access violation, Vectorworks terminated** | `vs.SetClFillFore(_c, (r, g, b))` (0..65535) |
| 2 | same (`pen_color`) | `vs.SetClPenFore(_c, vs.RGBToColorIndex(r, g, b))` | same crash class | `vs.SetClPenFore(_c, (r, g, b))` |
| 3 | `scripts_lib/edit_engine.py` mirror | `vs.HMirror(h, copy, p1, p2)` | `HMirror` does not exist (AttributeError; mirror never worked live) | `vs.MirrorN(h, dup, p1, p2, True)` (preserves symbol/PIO matrices) |
| 4 | `scripts_lib/stories.py` list | `vs.GetStory(layer)` | does not exist | `vs.GetStoryOfLayer(layer)` |
| 5 | `scripts_lib/saved_views.py` restore | `vs.RestoreSavedView(name)` | does not exist | `vs.VRestore(name)` |
| 6 | `scripts_lib/saved_views.py` create | `vs.SaveView(name)` | does not exist | `vs.SaveSheet(name, True, True, True)` (as the native bridge) |
| 7 | `scripts_lib/saved_views.py` delete | `vs.DelObject(vs.GetObject(name))` | saved views are not named drawing objects; native uses `VDelete` | existence check via the resource list, then `vs.VDelete(name)` |
| 8 | `scripts_lib/object_detail.py` | `vs.GetCustomObjectInfo(h)` | takes **no** arguments (describes the running plug-in, not `h`); 1 extra HANDLE arg | `vs.GetName(vs.GetParametricRecord(h))` |
| 9 | `tools/edit.py` select-by-uuid | `vs.SetSelect(vs.GetObjectByUuid(u))` | NIL handle passed to a HANDLE param when the uuid is gone | look up, `if _h:` then `SetSelect`; result = number selected |
| 10 | `testing/mock_bridge.py` `FakeVS` | `__getattr__` accepted any name; implemented the made-up `HMirror`, `GetStory`, `SaveView`, `RestoreSavedView` and `GetCustomObjectInfo(h)` | why bugs 3-8 passed e2e | mock now only accepts stub names (AttributeError otherwise) and implements the real API |
| - | type hardening (no bug) | `SetRField`/`NewField`/`ImportImageFile`/`SetStoryElevation` args from JSON | JSON values could arrive as numbers/None | explicit `str(...)` / `float(...)` |

## Full table

| Function | Call site(s) | Expected signature (stub) | Status |
|---|---|---|---|
| `SetClFillFore` | OrganizationHandlers.cpp class set | `SetClFillFore(className: STRING, color: COLOR)` | FIXED (#1) |
| `SetClPenFore` | OrganizationHandlers.cpp class set | `SetClPenFore(className: STRING, color: COLOR)` | FIXED (#2) |
| `RGBToColorIndex` | (was) OrganizationHandlers.cpp | `RGBToColorIndex(red, green, blue: LONGINT) -> color: INTEGER` | removed; never valid for a COLOR param |
| `SetClLW` | OrganizationHandlers.cpp | `SetClLW(className: STRING, LW: INTEGER)` (mils, int literal) | OK |
| `SetClUseGraphic` | OrganizationHandlers.cpp | `SetClUseGraphic(className: STRING, use: BOOLEAN)` | OK |
| `ShowClass` / `HideClass` / `GrayClass` | OrganizationHandlers.cpp | `(className: STRING)` | OK |
| `NameClass` | OrganizationHandlers.cpp | `NameClass(className: STRING)` (creates if missing; guarded by `ValidClass`) | OK |
| `GetCVis` | OrganizationHandlers.cpp list + set readback | `GetCVis(className: STRING) -> INTEGER` (-1/0/2) | OK |
| `ActiveClass` | OrganizationHandlers.cpp | `ActiveClass() -> STRING` | OK |
| `GetLayerByName` | OrganizationHandlers.cpp extras + set | `GetLayerByName(layerName: STRING) -> HANDLE`; NIL checked (`if not _h`) | OK |
| `GetLayerElevation` | OrganizationHandlers.cpp | `GetLayerElevation(h: HANDLE) -> (baseElev, thickness)`; used as `_e[0]`, `_e[1]` | OK |
| `SetLayerElevation` | OrganizationHandlers.cpp | `SetLayerElevation(h: HANDLE, baseElev: REAL, thickness: REAL)` | OK |
| `HGetLayerTransp` | OrganizationHandlers.cpp | `HGetLayerTransp(hLayer: HANDLE) -> REAL` | OK |
| `HSetLayerTransp` | OrganizationHandlers.cpp | `HSetLayerTransp(hLayer: HANDLE, transparency: REAL)` (0..100, clamped) | OK |
| `SaveSheet` | ViewCapture.cpp create/restore point; saved_views.py | `SaveSheet(name: STRING, saveView, saveClass, saveLayer: BOOLEAN)` | OK / FIXED (#6) |
| `VDelete` | ViewCapture.cpp; saved_views.py | `VDelete(name: STRING)` | OK / FIXED (#7) |
| `VRestore` | saved_views.py | `VRestore(name: STRING)` | FIXED (#5) |
| `MirrorN` | edit_engine.py | `MirrorN(h: HANDLE, dup: BOOLEAN, p1: POINT, p2: POINT, preserveMatrix: BOOLEAN) -> HANDLE` | FIXED (#3) |
| `GetStoryOfLayer` | stories.py | `GetStoryOfLayer(layer: HANDLE) -> HANDLE` (NIL if none) | FIXED (#4) |
| `GetParametricRecord` | object_detail.py | `GetParametricRecord(h: HANDLE) -> HANDLE` | FIXED (#8) |
| `GetCustomObjectInfo` | (was) object_detail.py | `GetCustomObjectInfo() -> (BOOLEAN, objectName, objectHand, recordHand, wallHand)` | removed (#8) |
| `GetObject` | _prelude, edit_engine, records, stories | `GetObject(name: STRING) -> HANDLE` | OK |
| `GetObjectByUuid` | _prelude, tools/edit.py | `GetObjectByUuid(UUID: STRING) -> HANDLE` | OK / FIXED (#9) |
| `GetObjectUuid` | _prelude | `GetObjectUuid(h: HANDLE) -> STRING` | OK |
| `GetTypeN` | _prelude `_nil`, edit_engine, object_detail | `GetTypeN(h: HANDLE) -> INTEGER`; `_nil` short-circuits `None` first | OK |
| `GetName` | _prelude, object_detail, records, stories, SKILL | `GetName(h: HANDLE) -> STRING` | OK |
| `SetName` | edit_engine, stories | `SetName(h: HANDLE, name: STRING)` | OK |
| `GetClass` | _prelude | `GetClass(h: HANDLE) -> STRING` | OK |
| `GetLayer` | _prelude | `GetLayer(h: HANDLE) -> HANDLE` | OK |
| `GetBBox` | object_detail | `GetBBox(h: HANDLE) -> (p1, p2)`; unpacked as `p1, p2` | OK |
| `NumRecords` / `GetRecord` | object_detail, records | `NumRecords(h) -> INTEGER`; `GetRecord(h: HANDLE, cnt: INTEGER) -> HANDLE` (1-based) | OK |
| `NumFields` / `GetFldName` | object_detail, records | `NumFields(h) -> INTEGER`; `GetFldName(h: HANDLE, index: INTEGER) -> STRING` | OK |
| `GetRField` | object_detail, records | `GetRField(h: HANDLE, record: STRING, field: STRING) -> STRING` | OK |
| `SetRecord` | edit_engine | `SetRecord(h: HANDLE, record: STRING)` | OK |
| `SetRField` | edit_engine | `SetRField(h: HANDLE, record, field, value: STRING)` | OK (value now `str()`) |
| `NewField` | edit_engine | `NewField(recName, fieldName, fieldValue: STRING, fType: INTEGER, fFlag: INTEGER)` | OK (4 = text) |
| `DelRecord` | edit_engine | `DelRecord(h: HANDLE, name: STRING)` | OK |
| `ResetObject` | edit_engine | `ResetObject(objectHandle: HANDLE)` | OK |
| `SetParent` | edit_engine | `SetParent(obj: HANDLE, container: HANDLE) -> BOOLEAN` (target checked type 31) | OK |
| `DSelectAll` / `SetSelect` | edit_engine, tools/edit.py | `DSelectAll()`; `SetSelect(h: HANDLE)` | OK |
| `Group` | edit_engine | `Group()` | NOTE: groups the selection **on the active layer only**; targets on other layers are left out |
| `LSActLayer` | edit_engine | `LSActLayer() -> HANDLE` (last selected on the active layer = the new group) | NOTE: same active-layer caveat |
| `Ungroup` | edit_engine | `Ungroup()` (acts on the selection; each target selected alone first) | OK |
| `BuildResourceList` | records (47), resource_list, saved_views (103) | `BuildResourceList(type: INTEGER, folderIndex: INTEGER, subFolderName: STRING) -> (LONGINT, numItems)` | OK (type 103 for saved views not live-verified) |
| `BuildResourceListN` | resource_import | `BuildResourceListN(type: INTEGER, fullPath: STRING) -> (LONGINT, numItems)` | OK |
| `GetNameFromResourceList` | records, resource_*, saved_views | `GetNameFromResourceList(listID: LONGINT, index: LONGINT) -> STRING` | OK |
| `ImportResToCurFileN` | resource_import | `ImportResToCurFileN(listID: LONGINT, index: LONGINT, callback: PROCEDURE) -> HANDLE` | NOTE: callback `lambda s: 1` (conflict policy) not live-verified |
| `ImportImageFile` | import_image | `ImportImageFile(filePath: STRING, importPt: POINT) -> HANDLE` | OK |
| `CreateStory` | stories | `CreateStory(name: STRING, suffix: STRING) -> BOOLEAN` | OK |
| `SetStoryElevation` | stories | `SetStoryElevation(story: HANDLE, elevation: REAL) -> BOOLEAN` | OK; NOTE: REAL is in document units although the arg is named `elevation_mm` |
| `GetStoryElevation` / `GetStorySuffix` | stories | `(story: HANDLE) -> REAL` / `-> STRING` | OK |
| `FLayer` / `NextLayer` | stories, SKILL | `FLayer() -> HANDLE`; `NextLayer(h: HANDLE) -> HANDLE` | OK |
| `FSActLayer` | catalog example, SKILL | `FSActLayer() -> HANDLE` | OK |
| `GetLName` | SKILL example | `GetLName(h: HANDLE) -> STRING` | OK |
| `SetClass` / `ForEachObject` | SKILL example | `SetClass(h: HANDLE, class: STRING)`; `ForEachObject(callback: PROCEDURE, c: CRITERIA)` | OK |
| `AlrtDialog` / `StrDialog` | SKILL (named as forbidden) | modal dialogs | OK (prose only; the lint rejects them) |

## Dialogs, selection state, handle-vs-name

- No shipped snippet opens a dialog. The lint rejects 51 modal/interactive
  functions (predefined alerts/dialogs, `GetPt`/`GetRect`/`TrackObject`,
  `RunLayoutDialog*`, `GetFile`/`PutFile`/`GetFolder`, ...) because they block
  the Vectorworks main thread and therefore the bridge.
- Selection-dependent calls (`Group`, `Ungroup`, `LSActLayer`) are always
  preceded by `DSelectAll` + explicit `SetSelect` and followed by `DSelectAll`.
- Every HANDLE parameter receives a handle (`GetObject`/`GetObjectByUuid`/
  `GetLayerByName`/`GetRecord`/...), every name parameter a string; class
  functions are the name-based family (`*Cl*`, `GetCVis`). No string literal
  is passed to a HANDLE parameter anywhere.

## Out of scope

`legacy/vw_listener.py` (frozen 0.7 listener, not on the 1.0 path) has 11
lint hits (e.g. `vs.SetFillForeColor`, `vs.CreateTextBlock`, `vs.AlrtDialog`),
mostly behind `hasattr(vs, ...)` guards. Not changed.
