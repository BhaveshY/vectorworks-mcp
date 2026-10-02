# Plan-quality connector design

## Usage

The fast-native MCP surface remains nine tools. Architectural quality is a
host-only action on `vw_read`; it does not ping Vectorworks or dispatch a native
action.

```python
result = await call_tool(
    "vw_read",
    {"action": "plan_quality", "plan": apartment_manifest},
)
assert result["ok"] is True
if not result["data"]["passed"]:
    for issue in result["data"]["issues"]:
        print(issue["code"], issue["object_ids"], issue["suggestion"])
```

A valid but poor design returns `ok: true, data.passed: false`. Malformed input
returns `ok: false, error.code: validation_error`. The result always states
that it is a deterministic project-design check, not regulatory compliance or
professional approval.

The production fixture uses the same manifest as its quality input, drawing
source, schedule source, and postflight expectation:

```python
quality = await call("vw_read", {"action": "plan_quality", "plan": raw_manifest})
require(quality["data"]["passed"])

plan = parse_plan(raw_manifest)
recipe = compile_vectorworks_recipe(plan, prefix=run_id)
await apply(recipe.foundation)                   # one atomic batch, including walls
wall_rows = await read_all_pages(recipe.wall_query)
await apply(recipe.bind_openings(wall_rows))     # one hosted-opening batch
observed = await read_all_pages(recipe.postflight_query)
require(verify_postflight(recipe, observed).passed)
```

## Shape

The public capability is one deep function:

```python
def evaluate_plan_payload(raw: object) -> PlanEvaluation: ...
```

It hides boundary validation, fixed-precision geometry, topology, collision
checks, graph reachability, issue ordering, measurements, and repair guidance.
The connector adapter only converts `PlanEvaluation` into the grouped MCP
envelope. Native wire operations, UUIDs, schema fingerprints, sockets, and
Vectorworks imports never enter the quality domain.

Version 1 deliberately supports axis-aligned rectangular rooms, furniture,
access zones, circulation zones, and annotation boxes, plus horizontal or
vertical wall segments. This covers the production floor-plan workflow with
exact predicates. Arbitrary polygons and curved routes are deferred until they
have a real fixture and a proven geometry kernel.

```python
@dataclass(frozen=True)
class Rect:
    left_um: int
    bottom_um: int
    right_um: int
    top_um: int

@dataclass(frozen=True)
class Room:
    id: str
    number: str
    name: str
    bounds: Rect
    target_area_m2: Decimal
    area_tolerance_m2: Decimal
    minimum_dimension_um: int
    minimum_exterior_windows: int

@dataclass(frozen=True)
class Wall:
    id: str
    start: Point
    end: Point
    thickness_um: int
    height_um: int

@dataclass(frozen=True)
class Door:
    id: str
    wall_id: str
    offset_um: int
    width_um: int
    height_um: int
    hinge: Literal["start", "end"]
    swing_into_room_id: str

@dataclass(frozen=True)
class Window:
    id: str
    wall_id: str
    offset_um: int
    width_um: int
    height_um: int
    sill_height_um: int

@dataclass(frozen=True)
class Furniture:
    id: str
    room_id: str
    bounds: Rect
    access_zones: tuple[AccessZone, ...]

@dataclass(frozen=True)
class AccessZone:
    id: str
    bounds: Rect
    minimum_clear_width_um: int

@dataclass(frozen=True)
class CirculationZone:
    id: str
    room_id: str
    bounds: Rect
    minimum_clear_width_um: int

@dataclass(frozen=True)
class RoomLabel:
    id: str
    room_id: str
    bounds: Rect

@dataclass(frozen=True)
class RoomSchedule:
    id: str
    bounds: Rect
    room_ids: tuple[str, ...]
    header_height_um: int
    minimum_row_height_um: int

@dataclass(frozen=True)
class PlanManifest:
    schema: Literal["vectorworks.plan-quality/v1"]
    units: Literal["mm"]
    plan_id: str
    assumptions: Assumptions
    entry: EntryRequirement
    required_adjacencies: tuple[AdjacencyRequirement, ...]
    rooms: tuple[Room, ...]
    walls: tuple[Wall, ...]
    openings: tuple[Door | Window, ...]
    furniture: tuple[Furniture, ...]
    circulation: tuple[CirculationZone, ...]
    annotations: tuple[RoomLabel | RoomSchedule, ...]
```

All numeric input is normalized once to integer micrometres. IDs are unique,
references resolve during parsing, rectangles have positive area, walls are
orthogonal, and unknown keys are rejected. Derived facts—room area, wall-room
incidence, exterior walls, connected rooms, door swing envelope, schedule
content, and expected native counts—are not accepted as synchronized copies.

The analyzer returns stable issues ordered by rule, code, and object ID:

```python
@dataclass(frozen=True)
class QualityIssue:
    severity: Literal["error", "warning", "info"]
    code: str
    object_ids: tuple[str, ...]
    measurements: tuple[Measurement, ...]
    suggestion: RepairSuggestion

@dataclass(frozen=True)
class QualityReport:
    schema: Literal["vectorworks.plan-quality-report/v1"]
    manifest_digest: str
    passed: bool
    disclaimer: str
    checks: tuple[str, ...]
    issues: tuple[QualityIssue, ...]
```

`passed` is true exactly when no issue severity occurs in the manifest's
`blocking_severities`. Repeated analysis of the same normalized manifest
produces the same digest and `data` payload.

The initial rule set covers:

- room area, minimum dimension, overlap, and required adjacency;
- wall incidence, opening end clearance, and opening overlap;
- designated entry arrival and room reachability;
- required exterior windows;
- door swing envelope containment and conservative two-sided collision checks
  until native swing/hinge readback is available;
- furniture containment, footprint collision, and access-zone clearance;
- circulation width, containment, and obstruction;
- room-label containment/collision and room-schedule density.

## Module map

```text
server.py
  -> plan_quality.py::evaluate_plan_payload
       -> plan_geometry.py

scripts/run-production-apartment-test.py
  -> production_apartment.py::compile_vectorworks_recipe
       -> plan_quality.py domain values
       -> existing vw_apply / vw_read grouped tools
```

- `server.py` owns only MCP schema/envelope adaptation and the early host-only
  branch.
- `plan_quality.py` owns parsing, immutable domain values, rule ordering,
  reports, and repair codes.
- `plan_geometry.py` owns exact rectangle/segment predicates. Rules do not
  implement ad-hoc overlap math.
- `production_apartment.py` owns the one-way translation into existing native
  operation dictionaries and honest postflight expectations.

The normal quality call crosses three files at most. No native bridge file or
handler matrix changes.

## Synthesis decision

Candidate A is the base (arena score 29/30 versus 25/30). It won on derived
topology, measured repair actions, deterministic reporting, and the shorter
fixture path. Candidate B contributed per-room area tolerances, explicit
access-zone clear widths, and an explicit list of completed checks in the
report.

Rejected from Candidate B: authoritative caller-declared exterior wall kinds,
duplicated door connectivity and sweep polygons, extra transaction stages, and
plain-string suggestions. Declared intent may be compared with derived geometry
but cannot replace it.

Before implementation, the synthesis also corrected Candidate A by retaining
`units` in the normalized model, defining `passed` from
`blocking_severities`, and avoiding a second raw-manifest parse in the fixture.

## Tradeoffs accepted

- We accept a strict orthogonal v1 in exchange for exact, regression-testable
  behavior now.
- We accept caller-supplied annotation boxes in exchange for deterministic
  collision checks without unavailable native text metrics.
- We accept exactly two creation transactions when openings are hosted in
  exchange for stable wall UUID binding.
- We accept that postflight proves fewer semantic facts than preflight in
  exchange for never inferring data the native read surface does not expose.
- We accept checking both possible internal-door swing sides in exchange for
  safe furniture placement while the native bridge cannot set or read swing.
- We accept explicit project thresholds in exchange for avoiding hidden or
  jurisdiction-dependent compliance claims.

## Alternatives considered

- A tenth `vw_plan_quality` tool expands the mandated surface without hiding
  more capability.
- A caller-authored rule DSL leaks geometry, rule ordering, and severity policy.
- Native postflight analysis requires a build/restart and still lacks the
  required intent/readback fields.
- More assertions beside the existing fixture lists preserve the duplication
  that caused the defects.

## Open questions and risks

- Exact native Space areas can differ from clear program bounds; live
  postflight therefore needs a distinct explicit tolerance.
- Annotation boxes must match the compiler's fixed text-width convention.
- Plans exceeding the 250-operation atomic limit need an explicit future
  compilation policy; v1 rejects an oversized foundation before mutation.

## Next implementation step

Implement the pure domain and geometry modules behind failing-plan and
corrected-plan golden tests, then wire the zero-dispatch grouped action.
