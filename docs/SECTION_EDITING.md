# Drawn architectural section editing

The existing ten tools now cover nested 2D section geometry through the native
bridge's script transaction engine. No new DLL is required. This workflow edits
drawn sections: lines, rectangles, straight polygons/polylines, linear dimensions
and text inside groups. It does not regenerate BIM section viewports or redesign
a parametric Stair object; use the existing model/parametric tools for those.

## Read and bind the geometry

Start with `vw_status()`, identify the document and section group, then:

```text
vw_read(what="geometry", target="<section-group-uuid>", detail=true, document="<document>")
```

Results include the root and nested group members, parent UUIDs, class/layer,
points, polyline vertex types/radii, dimension anchors/value/offset and full text.
All returned lengths are mm. Follow `page.next_cursor` until `page.has_more` is
false. `revision` covers the whole root, even on a partial page. Compact reads
limit points to 50 with explicit `vertices_truncated`; `detail=true` returns
complete vertices. Limits: 20,000 vertices per page, 10,000 per object, 200,000
per scope, 5,000 objects and 32 group levels. Oversized scopes fail explicitly.

Scope the section and adjacent elevation together, or pass several `targets`
with a revision per root. Do not include unrelated drawing objects.

## Change clear heights

`section_heights` must be the sole change in a call. Each band is an interval in
original drawing Y coordinates. Its clear height changes by `delta`. Between
bands, geometry translates by the accumulated delta without stretching, keeping
slab/roof thickness. Below the bands it stays put; above them it translates by
the total delta. Bands cannot overlap; old and new heights must be positive.

```text
vw_edit(document="<document>", changes=[{
  "op":"section_heights", "target":"<group-uuid>",
  "expected_geometry":{"<group-uuid>":"<revision from geometry read>"},
  "bands":[
    {"bottom":0,"top":3500,"delta":500},
    {"bottom":3880,"top":6380,"delta":500},
    {"bottom":6760,"top":9260,"delta":500},
    {"bottom":9640,"top":12140,"delta":500},
    {"bottom":12520,"top":15020,"delta":500},
    {"bottom":15400,"top":17900,"delta":500}
  ],
  "labels":[{"target":"<roof-text-uuid>","elevation":18900,"datum":110690,
             "template":"{level} = {absolute} m NN","precision":2}],
  "stairs":[{"target":"<ground-flight-uuid>","bottom":0,"top":3880}],
  "fixed_points":[{"target":"<overall-dimension-uuid>","vertex":1}],
  "preserve_text":["<section-title-text-uuid>"]
}], preview=true)
```

Add every level text and stair flight; the example abbreviates these lists.
Other texts must appear in `preserve_text`: content stays unchanged, position
follows the band map. A label moves by the change at its old `elevation`, keeping
its offset. `{level}` and `{absolute}` are formatted in metres; absolute is the
mapped elevation plus `datum`. With a shifted drawing origin, adjust the datum.
Replacing label content preserves object identity, but not inline rich text
runs; use simple level labels.

`stairs` binds a line, straight polygon/polyline, or group of these between old
landing elevations. The flight transforms uniformly between the new landings,
retaining equal riser spacing, tread count and X coordinates. This is drafting,
not a stair code/headroom solver. Fixed points are 1-based vertices (dimension:
1=start, 2=end) and cannot override a bound stair. Patrick's overall dimension
starts at 710 mm: that anchor stays fixed while the roof rises from 18,900 to
21,900 mm, so the actual dimension becomes 21,190 mm.

Dimensions change their witness anchors and Vectorworks recomputes their value;
offset and standard remain. Section edits reject hidden-value/non-linear
dimensions, curved polylines, holes, locked objects, transformed working planes,
symbols, PIOs and viewports before mutation. Ordinary XY geometry is required.
Vertex count/topology remain unchanged; bands are for floor-aligned geometry.

`dry_run=true` validates/compiles without reading the drawing; execution performs
live preflight. Each section call is one undo transaction with coordinate
readback. Any exception rolls it back. Revision mismatches reject stale/replayed
requests. After an uncertain result, inspect geometry; do not refresh a revision
simply to replay the same height delta.

## Individual reshapes

`vw_edit` op `reshape` accepts full `points` for polygons/polylines, preserving
vertex count, closure and vertex types/radii. Holes are refused. `start`/`end`
work for lines, walls and linear dimensions. Objects are retained and resulting
coordinates checked. Keeping a curve's radius while changing its control points
is not arbitrary curve fitting.

## Reproduce acceptance

```powershell
py -3.12 scripts/e2e_section.py --output C:\Evidence\section-new-run
```

Use a new directory. The runner creates its own document through source MCP
stdio, reproduces six storey changes, checks geometry, slab thickness, equal
risers, dimensions, labels, adjacent elevation, replay rejection and rollback
after mutation. It captures before/after, saves a VWX, closes/reopens that file,
and compares geometry again. It leaves the saved test file open. `--installed`
uses the installed launcher instead. Reports and call logs include timings.
No user document is changed and Vectorworks is never restarted.

The fixture follows Patrick's screenshots; his original VWX was not supplied.
Acceptance on his file remains separate. Live verification currently covers
Vectorworks 2024; 2025 is not live-verified for these new geometry operations.
