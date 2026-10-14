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
