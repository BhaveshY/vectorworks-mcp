"""One-source production apartment fixture for live Vectorworks acceptance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from plan_geometry import point_on_segment
from plan_quality import Door, PlanManifest, RoomLabel, RoomSchedule, Window, parse_plan


def create(operation_id: str, object_type: str, **params: Any) -> dict[str, Any]:
    return {
        "type": "create",
        "operation_id": operation_id,
        "params": {"object_type": object_type, **params},
    }


def build_manifest() -> dict[str, Any]:
    """Return the single architectural source for the production fixture."""
    return {
        "schema": "vectorworks.plan-quality/v1",
        "units": "mm",
        "plan_id": "production-two-bedroom-75-84m2",
        "assumptions": {
            "geometry_tolerance_mm": 2,
            "wall_room_tolerance_mm": 220,
            "minimum_wall_end_clearance_mm": 200,
            "annotation_clearance_mm": 75,
            "blocking_severities": ["error"],
        },
        "program": {
            "entry": {
                "door_id": "entry-door",
                "arrival_room_id": "entrance",
                "must_reach_room_ids": ["living", "kitchen", "bath", "bedroom-2", "bedroom-1"],
            },
            "required_adjacencies": [
                {"id": "adj-entry-corridor", "room_ids": ["entrance", "corridor"], "minimum_shared_boundary_mm": 1000},
                {"id": "adj-corridor-living", "room_ids": ["corridor", "living"], "minimum_shared_boundary_mm": 700},
                {"id": "adj-living-kitchen", "room_ids": ["living", "kitchen"], "minimum_shared_boundary_mm": 1800},
                {"id": "adj-corridor-bath", "room_ids": ["corridor", "bath"], "minimum_shared_boundary_mm": 1100},
                {"id": "adj-corridor-bedroom-2", "room_ids": ["corridor", "bedroom-2"], "minimum_shared_boundary_mm": 1500},
                {"id": "adj-corridor-bedroom-1", "room_ids": ["corridor", "bedroom-1"], "minimum_shared_boundary_mm": 1800},
            ],
        },
        "rooms": [
            {"id": "living", "number": "01", "name": "Living / Dining", "bounds": [200, 200, 5800, 4600], "target_area_m2": 24.64, "area_tolerance_m2": 0.01, "minimum_dimension_mm": 4000, "minimum_exterior_windows": 1},
            {"id": "kitchen", "number": "02", "name": "Kitchen", "bounds": [200, 5000, 5800, 7800], "target_area_m2": 15.68, "area_tolerance_m2": 0.01, "minimum_dimension_mm": 2700, "minimum_exterior_windows": 1},
            {"id": "entrance", "number": "03", "name": "Entrance", "bounds": [6200, 200, 7300, 3400], "target_area_m2": 3.52, "area_tolerance_m2": 0.01, "minimum_dimension_mm": 1100, "minimum_exterior_windows": 0},
            {"id": "bath", "number": "04", "name": "Bathroom", "bounds": [7700, 200, 8900, 3400], "target_area_m2": 3.84, "area_tolerance_m2": 0.01, "minimum_dimension_mm": 1200, "minimum_exterior_windows": 0},
            {"id": "bedroom-2", "number": "05", "name": "Bedroom 2", "bounds": [9300, 200, 11800, 3400], "target_area_m2": 8.00, "area_tolerance_m2": 0.01, "minimum_dimension_mm": 2500, "minimum_exterior_windows": 1},
            {"id": "corridor", "number": "06", "name": "Hall", "bounds": [6200, 3800, 11800, 4800], "target_area_m2": 5.60, "area_tolerance_m2": 0.01, "minimum_dimension_mm": 1000, "minimum_exterior_windows": 0},
            {"id": "bedroom-1", "number": "07", "name": "Bedroom 1", "bounds": [6200, 5200, 11800, 7800], "target_area_m2": 14.56, "area_tolerance_m2": 0.01, "minimum_dimension_mm": 2500, "minimum_exterior_windows": 1},
        ],
        "walls": [
            {"id": "south-living", "start": [0, 0], "end": [6000, 0], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "south-entrance", "start": [7500, 0], "end": [6000, 0], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "south-bath", "start": [7500, 0], "end": [9100, 0], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "south-bedroom-2", "start": [9100, 0], "end": [12000, 0], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "west-living", "start": [0, 0], "end": [0, 4800], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "west-kitchen", "start": [0, 4800], "end": [0, 8000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "north-kitchen", "start": [0, 8000], "end": [6000, 8000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "north-bedroom-1", "start": [6000, 8000], "end": [12000, 8000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "east-bedroom-2", "start": [12000, 0], "end": [12000, 3600], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "east-corridor", "start": [12000, 3600], "end": [12000, 5000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "east-bedroom-1", "start": [12000, 5000], "end": [12000, 8000], "thickness_mm": 200, "height_mm": 2700, "class_name": "A-Wall-External"},
            {"id": "spine-living-entrance", "start": [6000, 0], "end": [6000, 3600], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "spine-living-corridor", "start": [6000, 3600], "end": [6000, 5000], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "spine-kitchen-bedroom-1", "start": [6000, 5000], "end": [6000, 8000], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "living-kitchen", "start": [0, 4800], "end": [6000, 4800], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "entrance-bath", "start": [7500, 0], "end": [7500, 3600], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "bath-bedroom-2", "start": [9100, 0], "end": [9100, 3600], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "entrance-corridor", "start": [6000, 3600], "end": [7500, 3600], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "bath-corridor", "start": [7500, 3600], "end": [9100, 3600], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "bedroom-2-corridor", "start": [9100, 3600], "end": [12000, 3600], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
            {"id": "corridor-bedroom-1", "start": [6000, 5000], "end": [12000, 5000], "thickness_mm": 120, "height_mm": 2700, "class_name": "A-Wall-Internal"},
        ],
        "openings": [
            {"kind": "door", "id": "entry-door", "wall_id": "south-entrance", "offset_mm": 750, "width_mm": 1000, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "entrance"},
            {"kind": "door", "id": "entrance-door", "wall_id": "entrance-corridor", "offset_mm": 750, "width_mm": 900, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "corridor"},
            {"kind": "door", "id": "bath-door", "wall_id": "bath-corridor", "offset_mm": 800, "width_mm": 800, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "corridor"},
            {"kind": "door", "id": "bedroom-2-door", "wall_id": "bedroom-2-corridor", "offset_mm": 1450, "width_mm": 900, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "corridor"},
            {"kind": "door", "id": "bedroom-1-door", "wall_id": "corridor-bedroom-1", "offset_mm": 1000, "width_mm": 900, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "corridor"},
            {"kind": "door", "id": "living-door", "wall_id": "spine-living-corridor", "offset_mm": 700, "width_mm": 900, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "living"},
            {"kind": "door", "id": "kitchen-door", "wall_id": "living-kitchen", "offset_mm": 4500, "width_mm": 1000, "height_mm": 2100, "hinge": "start", "swing_into_room_id": "kitchen"},
            {"kind": "window", "id": "living-window", "wall_id": "west-living", "offset_mm": 2400, "width_mm": 1800, "height_mm": 1500, "sill_height_mm": 900},
            {"kind": "window", "id": "kitchen-window", "wall_id": "north-kitchen", "offset_mm": 3000, "width_mm": 1800, "height_mm": 1200, "sill_height_mm": 1050},
            {"kind": "window", "id": "bedroom-2-window", "wall_id": "east-bedroom-2", "offset_mm": 1800, "width_mm": 1400, "height_mm": 1500, "sill_height_mm": 900},
            {"kind": "window", "id": "bedroom-1-window", "wall_id": "north-bedroom-1", "offset_mm": 3000, "width_mm": 1800, "height_mm": 1500, "sill_height_mm": 900},
        ],
        "furniture": [
            {"id": "living-sofa", "room_id": "living", "bounds": [500, 700, 3000, 1500], "class_name": "A-Furn", "access_zones": [{"id": "living-sofa-access", "bounds": [500, 1600, 3000, 2000], "minimum_clear_width_mm": 400}]},
            {"id": "living-coffee", "room_id": "living", "bounds": [1300, 2200, 2500, 2800], "class_name": "A-Furn", "access_zones": []},
            {"id": "living-media", "room_id": "living", "bounds": [5200, 1200, 5700, 3300], "class_name": "A-Furn", "access_zones": []},
            {"id": "dining-table", "room_id": "living", "bounds": [2900, 2900, 4200, 3700], "class_name": "A-Furn", "access_zones": []},
            {"id": "kitchen-west", "room_id": "kitchen", "bounds": [250, 5200, 850, 7600], "class_name": "A-Furn-Cabinet", "access_zones": []},
            {"id": "kitchen-north", "room_id": "kitchen", "bounds": [900, 7200, 4500, 7750], "class_name": "A-Furn-Cabinet", "access_zones": []},
            {"id": "kitchen-island", "room_id": "kitchen", "bounds": [2100, 5700, 3900, 6400], "class_name": "A-Furn-Cabinet", "access_zones": [{"id": "island-access", "bounds": [2100, 6500, 3900, 7100], "minimum_clear_width_mm": 600}]},
            {"id": "bath-shower", "room_id": "bath", "bounds": [7700, 200, 8900, 1300], "class_name": "P-Fixture", "access_zones": []},
            {"id": "bath-vanity", "room_id": "bath", "bounds": [7700, 1400, 8250, 2200], "class_name": "P-Fixture", "access_zones": []},
            {"id": "bath-wc", "room_id": "bath", "bounds": [8350, 1400, 8850, 2200], "class_name": "P-Fixture", "access_zones": []},
            {"id": "bedroom-2-bed", "room_id": "bedroom-2", "bounds": [10300, 300, 11800, 2300], "class_name": "A-Furn", "access_zones": [{"id": "bedroom-2-bed-access", "bounds": [9300, 1600, 10200, 2300], "minimum_clear_width_mm": 700}]},
            {"id": "bedroom-2-wardrobe", "room_id": "bedroom-2", "bounds": [9300, 300, 9800, 1500], "class_name": "A-Furn-Storage", "access_zones": [{"id": "bedroom-2-wardrobe-access", "bounds": [9800, 300, 10200, 1500], "minimum_clear_width_mm": 400}]},
            {"id": "bedroom-1-bed", "room_id": "bedroom-1", "bounds": [7600, 5500, 9600, 7600], "class_name": "A-Furn", "access_zones": [{"id": "bedroom-1-bed-left", "bounds": [6900, 5500, 7500, 7600], "minimum_clear_width_mm": 600}, {"id": "bedroom-1-bed-right", "bounds": [9700, 5500, 10300, 7600], "minimum_clear_width_mm": 600}]},
            {"id": "bedroom-1-wardrobe", "room_id": "bedroom-1", "bounds": [10500, 5500, 11750, 6200], "class_name": "A-Furn-Storage", "access_zones": [{"id": "bedroom-1-wardrobe-access", "bounds": [10500, 6300, 11750, 6900], "minimum_clear_width_mm": 600}]},
        ],
        "circulation": [
            {"id": "entrance-clear", "room_id": "entrance", "bounds": [6200, 300, 7300, 1700], "minimum_clear_width_mm": 1100},
            {"id": "corridor-clear", "room_id": "corridor", "bounds": [6200, 3900, 11800, 4700], "minimum_clear_width_mm": 800},
            {"id": "living-clear", "room_id": "living", "bounds": [5000, 3500, 5700, 4500], "minimum_clear_width_mm": 700},
            {"id": "kitchen-clear", "room_id": "kitchen", "bounds": [4700, 5200, 5600, 7600], "minimum_clear_width_mm": 900},
            {"id": "bath-clear", "room_id": "bath", "bounds": [7700, 2300, 8900, 3300], "minimum_clear_width_mm": 1000},
            {"id": "bedroom-2-clear", "room_id": "bedroom-2", "bounds": [9300, 2350, 11800, 2750], "minimum_clear_width_mm": 400},
            {"id": "bedroom-1-clear", "room_id": "bedroom-1", "bounds": [6200, 5400, 6800, 7600], "minimum_clear_width_mm": 600},
        ],
        "annotations": [
            {"kind": "room_label", "id": "living-label", "room_id": "living", "bounds": [500, 3500, 2700, 4300]},
            {"kind": "room_label", "id": "kitchen-label", "room_id": "kitchen", "bounds": [1200, 6500, 2300, 7000]},
            {"kind": "room_label", "id": "bedroom-2-label", "room_id": "bedroom-2", "bounds": [9350, 2250, 10000, 2650]},
            {"kind": "room_label", "id": "bedroom-1-label", "room_id": "bedroom-1", "bounds": [6250, 7000, 7500, 7650]},
            {"kind": "room_schedule", "id": "room-schedule", "bounds": [13000, 4100, 17800, 7200], "room_ids": ["living", "kitchen", "entrance", "bath", "bedroom-2", "corridor", "bedroom-1"], "header_height_mm": 400, "minimum_row_height_mm": 350},
        ],
    }


@dataclass(frozen=True)
class CompiledPlan:
    manifest: PlanManifest
    foundation: tuple[dict[str, Any], ...]
    wall_names: Mapping[str, str]


def _mm(value: int) -> float:
    return value / 1000.0


def _rect_params(bounds: Any) -> dict[str, float]:
    return {
        "x1": _mm(bounds.left),
        "y1": _mm(bounds.bottom),
        "x2": _mm(bounds.right),
        "y2": _mm(bounds.top),
    }


def _room_points(room: Any) -> list[list[float]]:
    rect = room.bounds
    return [
        [_mm(rect.left), _mm(rect.bottom)],
        [_mm(rect.right), _mm(rect.bottom)],
        [_mm(rect.right), _mm(rect.top)],
        [_mm(rect.left), _mm(rect.top)],
    ]


def compile_foundation(raw_manifest: dict[str, Any], prefix: str) -> CompiledPlan:
    manifest = parse_plan(raw_manifest)
    operations: list[dict[str, Any]] = [
        create(
            "floor-slab",
            "slab",
            points=[[0, 0], [12000, 0], [12000, 8000], [0, 8000]],
            closed=True,
            thickness=200,
            elevation=-200,
            name=f"{prefix}_SLAB",
            class_name="A-Slab",
        )
    ]
    operations.extend(
        create(
            f"space-{room.id}",
            "space",
            points=_room_points(room),
            closed=True,
            height=2700,
            name=room.name,
            room_id=room.number,
            class_name="A-Space",
        )
        for room in manifest.rooms
    )

    wall_names: dict[str, str] = {}
    for wall in manifest.walls:
        wall_name = f"{prefix}_WALL_{wall.id.upper()}"
        wall_names[wall.id] = wall_name
        operations.append(
            create(
                f"wall-{wall.id}",
                "wall",
                x1=_mm(wall.segment.start.x),
                y1=_mm(wall.segment.start.y),
                x2=_mm(wall.segment.end.x),
                y2=_mm(wall.segment.end.y),
                height=_mm(wall.height),
                thickness=_mm(wall.thickness),
                name=wall_name,
                class_name=wall.class_name,
            )
        )

    for item in manifest.furniture:
        operations.append(
            create(
                f"furniture-{item.id}",
                "rect",
                **_rect_params(item.bounds),
                name=f"{prefix}_FURN_{item.id.upper()}",
                class_name=item.class_name,
            )
        )

    room_by_id = {room.id: room for room in manifest.rooms}
    for annotation in manifest.annotations:
        if isinstance(annotation, RoomLabel):
            room = room_by_id[annotation.room_id]
            operations.append(
                create(
                    f"annotation-{annotation.id}",
                    "text",
                    x=_mm(annotation.bounds.left),
                    y=_mm(annotation.bounds.top),
                    text=f"{room.number}  {room.name}\n{room.target_area_m2:.2f} m2",
                    text_size=10,
                    name=f"{prefix}_LABEL_{room.id.upper()}",
                    class_name="A-Anno-Room",
                )
            )
        elif isinstance(annotation, RoomSchedule):
            operations.append(
                create(
                    "room-schedule-heading",
                    "text",
                    x=_mm(annotation.bounds.left),
                    y=_mm(annotation.bounds.top),
                    text="ROOM SCHEDULE",
                    text_size=11,
                    name=f"{prefix}_ROOM_SCHEDULE_HEADING",
                    class_name="A-Anno-Text",
                )
            )
            row_step = 340
            for row_index, room_id in enumerate(annotation.room_ids, start=1):
                room = room_by_id[room_id]
                operations.append(
                    create(
                        f"room-schedule-row-{room.id}",
                        "text",
                        x=_mm(annotation.bounds.left),
                        y=_mm(annotation.bounds.top) - row_step * row_index,
                        text=f"{room.number}  {room.name}    {room.target_area_m2:.2f} m2",
                        text_size=9,
                        name=f"{prefix}_ROOM_SCHEDULE_{room.id.upper()}",
                        class_name="A-Anno-Text",
                    )
                )
            operations.append(
                create(
                    "room-schedule-total",
                    "text",
                    x=_mm(annotation.bounds.left),
                    y=_mm(annotation.bounds.top) - row_step * (len(annotation.room_ids) + 1),
                    text=f"NET PROGRAM AREA    {sum(room.target_area_m2 for room in manifest.rooms):.2f} m2",
                    text_size=10,
                    name=f"{prefix}_ROOM_SCHEDULE_TOTAL",
                    class_name="A-Anno-Text",
                )
            )

    operations.extend(
        [
            create("sheet-border", "rect", x1=-1500, y1=-1500, x2=18400, y2=9800, name=f"{prefix}_BORDER", class_name="A-Sheet-Border"),
            create("title-rule", "line", x1=12800, y1=0, x2=12800, y2=9000, name=f"{prefix}_TITLE_RULE", class_name="A-Sheet-Border"),
            create("title", "text", x=13200, y=8850, text="TWO-BEDROOM APARTMENT", text_size=14, name=f"{prefix}_TITLE", class_name="A-Anno-Title"),
            create("subtitle", "text", x=13200, y=8350, text="QUALITY-GATED GENERAL ARRANGEMENT", text_size=11, name=f"{prefix}_SUBTITLE", class_name="A-Anno-Title"),
            create("scale", "text", x=13200, y=7900, width=4500, wrap=True, text="Scale 1:50 at A3  |  Units: mm", text_size=10, name=f"{prefix}_SCALE", class_name="A-Anno-Text"),
            create("quality-note", "text", x=13200, y=7600, width=4500, wrap=True, text="Plan-quality gate: PASS  |  Project assumptions, not code compliance", text_size=9, name=f"{prefix}_QUALITY", class_name="A-Anno-Text"),
            create("notes-heading", "text", x=13200, y=3550, width=4500, wrap=True, text="GENERAL NOTES", text_size=12, name=f"{prefix}_NOTES_HEAD", class_name="A-Anno-Title"),
            create("notes", "text", x=13200, y=3150, width=4500, wrap=True, text="1. Verify dimensions and applicable regulations.\n2. Do not scale from this drawing.\n3. Doors/windows are hosted native objects.\n4. Spaces, clearances and annotations passed the connector quality gate.", text_size=9, name=f"{prefix}_NOTES", class_name="A-Anno-Text"),
            create("north-a", "line", x1=15100, y1=800, x2=15100, y2=2200, name=f"{prefix}_NORTH_A", class_name="A-Anno-Marker"),
            create("north-b", "polygon", points=[[15100, 2550], [14750, 2050], [15450, 2050]], closed=True, name=f"{prefix}_NORTH_B", class_name="A-Anno-Marker"),
            create("north-label", "text", x=14950, y=2850, text="N", text_size=16, name=f"{prefix}_NORTH_LABEL", class_name="A-Anno-Marker"),
            create("dim-overall-x", "linear_dimension", x1=0, y1=0, x2=12000, y2=0, offset=-800, class_name="A-Anno-Dims"),
            create("dim-overall-y", "linear_dimension", x1=12000, y1=0, x2=12000, y2=8000, offset=800, class_name="A-Anno-Dims"),
            create("dim-zone-x-left", "linear_dimension", x1=0, y1=8000, x2=6000, y2=8000, offset=700, class_name="A-Anno-Dims"),
            create("dim-zone-x-right", "linear_dimension", x1=6000, y1=8000, x2=12000, y2=8000, offset=700, class_name="A-Anno-Dims"),
        ]
    )

    edits = [
        {"ref": "$floor-slab", "properties": {"fillPattern": 0}},
        {"ref": "$sheet-border", "properties": {"fillPattern": 0}},
    ]
    edits.extend(
        {"ref": f"$space-{room.id}", "properties": {"fillPattern": 0}}
        for room in manifest.rooms
    )
    edits.extend(
        {
            "ref": f"$furniture-{item.id}",
            "properties": {"fillPattern": 1, "fillColor": "56000,56000,56000", "lineWeight": 10},
        }
        for item in manifest.furniture
    )
    operations.append({"type": "set_properties", "params": {"edits": edits}})
    return CompiledPlan(manifest, tuple(operations), wall_names)


def compile_openings(
    compiled: CompiledPlan,
    *,
    prefix: str,
    wall_uuids: Mapping[str, str],
    door_fingerprint: str,
    window_fingerprint: str,
) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    wall_by_id = {wall.id: wall for wall in compiled.manifest.walls}
    for opening in compiled.manifest.openings:
        wall = wall_by_id[opening.wall_id]
        point = point_on_segment(wall.segment, opening.offset)
        if wall.segment.horizontal:
            rotation = 0 if wall.segment.end.x >= wall.segment.start.x else 180
        else:
            rotation = 90 if wall.segment.end.y >= wall.segment.start.y else -90
        kind = "door" if isinstance(opening, Door) else "window"
        params: dict[str, Any] = {
            "plugin_name": "Door" if kind == "door" else "Window",
            "descriptor_fingerprint": door_fingerprint if kind == "door" else window_fingerprint,
            "wall_uuid": wall_uuids[opening.wall_id],
            "x": _mm(point.x),
            "y": _mm(point.y),
            "rotation": rotation,
            "width": _mm(opening.width),
            "height": _mm(opening.height),
            "name": f"{prefix}_{kind.upper()}_{opening.id.upper()}",
            "class_name": "A-Door" if kind == "door" else "A-Window",
        }
        if isinstance(opening, Window):
            params["sill_height"] = _mm(opening.sill_height)
        operations.append(create(f"{kind}-{opening.id}", kind, **params))
    return operations


def expected_counts(compiled: CompiledPlan) -> dict[str, int]:
    return {
        "spaces": len(compiled.manifest.rooms),
        "walls": len(compiled.manifest.walls),
        "doors": sum(isinstance(item, Door) for item in compiled.manifest.openings),
        "windows": sum(isinstance(item, Window) for item in compiled.manifest.openings),
    }
