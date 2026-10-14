# E2E findings (2026-10-14)
1. Spaces created with walls cover walls/doors (stacking). Need auto send-to-back + vw_edit op "order" (front/back/forward/backward).
2. vw_edit set: flat props (e.g. {"op":"set","width":1800}) rejected; error example irrelevant.
3. vw_read object for door/window = 10.9 KB (all records) -> compact default, detail opt-in.
4. vw_read object for wall lacks start/end/thickness/height (only bounds) -> add per-type geometry.
5. parametric op requires plugin even with target; friendly names (width/height/sill) not mapped.
6. parametric_schema = 39.7 KB -> compact default (common params) + search filter + detail.
7. Space rename via set name changes object name only; label (Space 'Number' field as created) unchanged; query 'name' misleading. Need room_name semantics + label readback.
8. Unknown resource names (e.g. hatch "Cross Hatch") -> error should include did-you-mean suggestions from document+library names.
9. vw_read(what="resources", search="cross") ignores search; returns full list (5.5 KB). Need name filter (search/query/name contains).
10. vw_capture(view="layer", layer=X) without standard_view came back in ISO (inherited state); must default to top/plan for plan captures unless asked.
11. view="layer" does not isolate the layer (other layers shown/grayed per Layer Options); during capture set layer+class options to "active only"/show only requested layers, then restore.
12. layer/objects fit not applied (fit_to_objects_applied:false, center [0,0]); content cut off.
13. After iso/3D captures the user's view was left in right_iso although restored:true (restore of standard view/projection/render mode broken after 3D).
14. CRITICAL: vw_create top-level "layer":"First" ignored -> objects created on active layer. Support call-level and per-object layer (by name/uuid), report layer in receipts.
15. vw_read(what="viewports", sheet="A-101") rejected (needs target) -> accept sheet alias.
16. Sheet capture (view="sheet") shows design-layer objects mixed with viewports -> isolate the sheet layer.
17. IFC export: after ~minutes Vectorworks shows modal "IFC Export Report" dialog inside the handler (buttons "Save Report...", "OK"; grid control) -> blocks; then job fails "silent IIFCSupport::ExportFile failed". Need: handler-owned report dialogs closed (OK) with text captured; WM_COMMAND(id) did NOT close it, BM_CLICK on the button did -> use BM_CLICK; and a working IFC export path (e.g. vs.IFC_ExportNoUI or proper IFC setup), fast.
18. vw_read(what="query", query="name") ignores query for objects -> treat query as name contains (and text contains) filter.
19. vw_create with 400 objects rejected (250 op cap) -> auto-chunk in host (report undo steps) and/or raise native cap.
20. vw_create receipt for 250 objects = 26.7 KB (created list + ids map duplicate) -> compact receipts (ids map only, or count + first N + resource link) for large plans.
21. import receipt = 33.6 KB (document_mutation before/after dump) -> compact.
22. Captures too small (fit zooms too far out; ~420 px wide after trim) -> fit should fill ~90% of drawing area; target long edge >=1200 px (or render rect at higher resolution).
Also (positive): sloppy inputs (stringified JSON, "3m", {x,y}, uppercase types, oversized limit) handled; multi-doc pinning correct; atomic rollback w/ compensation correct; save_as/close/open, PDF, DWG export/import, viewports, styles, massing work; idle 5 min reconnect OK.

## Re-verification after the fixes (live, Vectorworks 2024, build 09cc7fd)

All 22 findings above are fixed and were re-checked live:
- #1 order op + spaces behind walls
- #2/#5/#7 flat `set`, friendly plug-in names, Space room label
- #3/#4/#6 compact reads with per-type geometry (wall read 618 B)
- #8/#9/#18 did-you-mean, resource search, name/text/room-label search
- #10-#13/#16/#22 plan captures:
  - Top/Plan by default; layer/sheet isolation
  - fit about 90 %
  - restore verified (`restored` means the re-read view matched)
  - sharp output: 1600-3000 px via a temporary layer-scale boost
  - real PNG files
- #14 per-call and per-object layer
- #15 `sheet=` alias
- #17 IFC export works, see below
- #19/#20/#21 2,000-op bridge cap with host chunking, compact receipts

New problems found in this pass, all fixed:
- **Doors/windows deleted on any wall change.** Native creation put the plug-in in the wall without a wall-break record, so Vectorworks dropped it when the wall regenerated (class, line weight, move). Fix: vw_create inserts openings with `vs.AddSymToWall`, then sizes them natively (durable, verified after class change, move and reset). The native path is now best-effort and warns.
- **IFC:**
  - Vectorworks exports nothing until design layers are on a story and the document's IFC export has run once.
  - The host explains the missing-story case, `vw_organize(kind="story", layers=[...])` assigns layers, and the first export drives Vectorworks' own export dialog once (Export button only) before exporting without UI.
  - `IIFCSupport::ExportFile` and the schema/header setup calls preceded Vectorworks crashes (0xC0000005) on the next document; the bridge now uses `IFC_ExportNoUI` only.
- **Home screen crash:** closing the CEF Home screen while it was still loading, then creating a document, crashed Vectorworks in libcef.dll. The bridge now closes Home only after 2 s and waits 1 s afterwards. Verified with 10 close/new cycles.
- **Big documents:**
  - Creates slowed down as the document grew (a 250-op plan took 3.9 s at 12k objects). Simple objects now reuse their handle instead of being looked up again by UUID; the same plan takes 1.06 s at 13.5k objects.
  - A 5-storey, 7,025-object test project builds in 147 s (23-34 s per storey).
  - The write budget scales with plan size, and job results are compacted (a 300 KB reply is now a few KB).
- **Smaller fixes:**
  - viewports listed without a sheet
  - generic `fill:"gradient"` / `line_style:"dashed"`
  - `discard:true` alias
  - readable summary types/bounds
  - region queries (`within`, `near`, `within_px` on the last capture via `image_to_world`)

Results:
- Native acceptance: 92/98 (0 required failures; the 6 are the best-effort native opening path and native-only IFC in an uninitialised document).
- Host e2e: 241/241.
- Existing-project e2e: 16/16 in 17 s of tool time.
