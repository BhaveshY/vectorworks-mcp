"""Deterministic architectural quality checks for orthogonal floor plans.

This module is deliberately independent of MCP, sockets, and Vectorworks. It
parses an untrusted versioned manifest into immutable values and returns a
stable, actionable report. The checks are project-design checks, not building
code compliance or professional approval.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections import defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Literal, Mapping, Sequence

from plan_geometry import (
    Point,
    Rect,
    Segment,
    contains,
    door_swing_envelope,
    overlap_area,
    segment_boundary_overlap,
    shared_boundary_length,
)


DISCLAIMER = (
    "Deterministic caller-configured project design checks; not regulatory "
    "compliance, code review, or professional approval."
)
CHECKS = (
    "room_program",
    "room_overlap",
    "required_adjacency",
    "wall_incidence",
    "opening_clearance",
    "entry_connectivity",
    "exterior_windows",
    "door_swing_clearance",
    "furniture_clearance",
    "circulation_clearance",
    "annotation_clearance",
    "schedule_density",
)
Severity = Literal["error", "warning", "info"]


@dataclass(frozen=True)
class ValidationProblem:
    path: str
    code: str
    message: str


class PlanValidationError(ValueError):
    def __init__(self, problems: Sequence[ValidationProblem]):
        self.problems = tuple(problems)
        super().__init__("Plan manifest is malformed")

    def as_list(self) -> list[dict[str, str]]:
        return [dataclasses.asdict(problem) for problem in self.problems]


@dataclass(frozen=True)
class Assumptions:
    geometry_tolerance: int
    wall_room_tolerance: int
    minimum_wall_end_clearance: int
    annotation_clearance: int
    blocking_severities: frozenset[str]


@dataclass(frozen=True)
class Room:
    id: str
    number: str
    name: str
    bounds: Rect
    target_area_m2: Decimal
    area_tolerance_m2: Decimal
    minimum_dimension: int
    minimum_exterior_windows: int


@dataclass(frozen=True)
class Wall:
    id: str
    segment: Segment
    thickness: int
    height: int
    class_name: str


@dataclass(frozen=True)
class Door:
    id: str
    wall_id: str
    offset: int
    width: int
    height: int
    hinge: Literal["start", "end"]
    swing_into_room_id: str


@dataclass(frozen=True)
class Window:
    id: str
    wall_id: str
    offset: int
    width: int
    height: int
    sill_height: int


Opening = Door | Window


@dataclass(frozen=True)
class AccessZone:
    id: str
    bounds: Rect
    minimum_clear_width: int


@dataclass(frozen=True)
class Furniture:
    id: str
    room_id: str
    bounds: Rect
    class_name: str
    access_zones: tuple[AccessZone, ...]


@dataclass(frozen=True)
class CirculationZone:
    id: str
    room_id: str
    bounds: Rect
    minimum_clear_width: int


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
    header_height: int
    minimum_row_height: int


Annotation = RoomLabel | RoomSchedule


@dataclass(frozen=True)
class EntryRequirement:
    door_id: str
    arrival_room_id: str
    must_reach_room_ids: tuple[str, ...]


@dataclass(frozen=True)
class AdjacencyRequirement:
    id: str
    room_ids: tuple[str, str]
    minimum_shared_boundary: int


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
    openings: tuple[Opening, ...]
    furniture: tuple[Furniture, ...]
    circulation: tuple[CirculationZone, ...]
    annotations: tuple[Annotation, ...]


def _pointer(parent: str, key: object) -> str:
    token = str(key).replace("~", "~0").replace("/", "~1")
    return f"{parent}/{token}" if parent else f"/{token}"


class _Parser:
    def __init__(self) -> None:
        self.problems: list[ValidationProblem] = []

    def problem(self, path: str, code: str, message: str) -> None:
        self.problems.append(ValidationProblem(path or "/", code, message))

    def mapping(self, value: object, path: str, allowed: Iterable[str]) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            self.problem(path, "type.object_required", "Expected an object.")
            return {}
        allowed_set = set(allowed)
        for key in value:
            if not isinstance(key, str) or key not in allowed_set:
                self.problem(_pointer(path, key), "field.unknown", "Unknown field.")
        return value

    def sequence(self, value: object, path: str) -> list[object]:
        if not isinstance(value, list):
            self.problem(path, "type.array_required", "Expected an array.")
            return []
        return value

    def text(self, value: object, path: str, *, required: bool = True) -> str:
        if value is None and not required:
            return ""
        if not isinstance(value, str) or not value.strip():
            self.problem(path, "type.non_empty_string_required", "Expected a non-empty string.")
            return ""
        return value.strip()

    def decimal(self, value: object, path: str, *, minimum: Decimal | None = None) -> Decimal:
        if isinstance(value, bool):
            self.problem(path, "type.number_required", "Expected a finite number.")
            return Decimal(0)
        try:
            number = Decimal(str(value))
        except (InvalidOperation, ValueError):
            self.problem(path, "type.number_required", "Expected a finite number.")
            return Decimal(0)
        if not number.is_finite():
            self.problem(path, "number.not_finite", "Number must be finite.")
            return Decimal(0)
        if minimum is not None and number < minimum:
            self.problem(path, "number.below_minimum", f"Number must be at least {minimum}.")
        return number

    def integer(self, value: object, path: str, *, minimum: int = 0) -> int:
        number = self.decimal(value, path, minimum=Decimal(minimum))
        if number != number.to_integral_value():
            self.problem(path, "number.integer_required", "Expected an integer.")
        return int(number)

    def mm(self, value: object, path: str, *, positive: bool = False) -> int:
        minimum = Decimal("0.001") if positive else None
        number = self.decimal(value, path, minimum=minimum)
        return int((number * 1000).to_integral_value())

    def point(self, value: object, path: str) -> Point:
        values = self.sequence(value, path)
        if len(values) != 2:
            self.problem(path, "geometry.point_arity", "Point must contain exactly two coordinates.")
            values = (values + [0, 0])[:2]
        return Point(self.mm(values[0], _pointer(path, 0)), self.mm(values[1], _pointer(path, 1)))

    def rect(self, value: object, path: str) -> Rect:
        values = self.sequence(value, path)
        if len(values) != 4:
            self.problem(path, "geometry.rect_arity", "Rectangle must be [left,bottom,right,top].")
            values = (values + [0, 0, 0, 0])[:4]
        rect = Rect(*(self.mm(values[index], _pointer(path, index)) for index in range(4)))
        if rect.width <= 0 or rect.height <= 0:
            self.problem(path, "geometry.rect_non_positive", "Rectangle must have positive width and height.")
        return rect


def _require(mapping: Mapping[str, object], key: str, parser: _Parser, path: str) -> object:
    if key not in mapping:
        parser.problem(_pointer(path, key), "field.required", "Required field is missing.")
    return mapping.get(key)


def parse_plan(raw: object) -> PlanManifest:
    parser = _Parser()
    root = parser.mapping(
        raw,
        "",
        {
            "schema",
            "units",
            "plan_id",
            "assumptions",
            "program",
            "rooms",
            "walls",
            "openings",
            "furniture",
            "circulation",
            "annotations",
        },
    )
    schema = parser.text(_require(root, "schema", parser, ""), "/schema")
    if schema and schema != "vectorworks.plan-quality/v1":
        parser.problem("/schema", "schema.unsupported", "Only vectorworks.plan-quality/v1 is supported.")
    units = parser.text(_require(root, "units", parser, ""), "/units")
    if units and units != "mm":
        parser.problem("/units", "units.unsupported", "Version 1 requires millimetres (mm).")
    plan_id = parser.text(_require(root, "plan_id", parser, ""), "/plan_id")

    assumptions_raw = parser.mapping(
        _require(root, "assumptions", parser, ""),
        "/assumptions",
        {
            "geometry_tolerance_mm",
            "wall_room_tolerance_mm",
            "minimum_wall_end_clearance_mm",
            "annotation_clearance_mm",
            "blocking_severities",
        },
    )
    blocking_raw = parser.sequence(
        _require(assumptions_raw, "blocking_severities", parser, "/assumptions"),
        "/assumptions/blocking_severities",
    )
    blocking = frozenset(
        parser.text(value, f"/assumptions/blocking_severities/{index}")
        for index, value in enumerate(blocking_raw)
    )
    invalid_severities = blocking - {"error", "warning", "info"}
    if invalid_severities:
        parser.problem(
            "/assumptions/blocking_severities",
            "severity.invalid",
            f"Unsupported severities: {', '.join(sorted(invalid_severities))}.",
        )
    assumptions = Assumptions(
        geometry_tolerance=parser.mm(
            _require(assumptions_raw, "geometry_tolerance_mm", parser, "/assumptions"),
            "/assumptions/geometry_tolerance_mm",
        ),
        wall_room_tolerance=parser.mm(
            _require(assumptions_raw, "wall_room_tolerance_mm", parser, "/assumptions"),
            "/assumptions/wall_room_tolerance_mm",
        ),
        minimum_wall_end_clearance=parser.mm(
            _require(assumptions_raw, "minimum_wall_end_clearance_mm", parser, "/assumptions"),
            "/assumptions/minimum_wall_end_clearance_mm",
        ),
        annotation_clearance=parser.mm(
            _require(assumptions_raw, "annotation_clearance_mm", parser, "/assumptions"),
            "/assumptions/annotation_clearance_mm",
        ),
        blocking_severities=blocking,
    )

    rooms: list[Room] = []
    for index, item in enumerate(parser.sequence(_require(root, "rooms", parser, ""), "/rooms")):
        path = f"/rooms/{index}"
        value = parser.mapping(
            item,
            path,
            {
                "id",
                "number",
                "name",
                "bounds",
                "target_area_m2",
                "area_tolerance_m2",
                "minimum_dimension_mm",
                "minimum_exterior_windows",
            },
        )
        rooms.append(
            Room(
                id=parser.text(_require(value, "id", parser, path), f"{path}/id"),
                number=parser.text(_require(value, "number", parser, path), f"{path}/number"),
                name=parser.text(_require(value, "name", parser, path), f"{path}/name"),
                bounds=parser.rect(_require(value, "bounds", parser, path), f"{path}/bounds"),
                target_area_m2=parser.decimal(
                    _require(value, "target_area_m2", parser, path),
                    f"{path}/target_area_m2",
                    minimum=Decimal("0.001"),
                ),
                area_tolerance_m2=parser.decimal(
                    _require(value, "area_tolerance_m2", parser, path),
                    f"{path}/area_tolerance_m2",
                    minimum=Decimal(0),
                ),
                minimum_dimension=parser.mm(
                    _require(value, "minimum_dimension_mm", parser, path),
                    f"{path}/minimum_dimension_mm",
                    positive=True,
                ),
                minimum_exterior_windows=parser.integer(
                    _require(value, "minimum_exterior_windows", parser, path),
                    f"{path}/minimum_exterior_windows",
                ),
            )
        )

    walls: list[Wall] = []
    for index, item in enumerate(parser.sequence(_require(root, "walls", parser, ""), "/walls")):
        path = f"/walls/{index}"
        value = parser.mapping(
            item,
            path,
            {"id", "start", "end", "thickness_mm", "height_mm", "class_name"},
        )
        segment = Segment(
            parser.point(_require(value, "start", parser, path), f"{path}/start"),
            parser.point(_require(value, "end", parser, path), f"{path}/end"),
        )
        if (not segment.horizontal and not segment.vertical) or segment.start == segment.end:
            parser.problem(path, "geometry.wall_not_orthogonal", "Wall must be a non-zero horizontal or vertical segment.")
        walls.append(
            Wall(
                id=parser.text(_require(value, "id", parser, path), f"{path}/id"),
                segment=segment,
                thickness=parser.mm(
                    _require(value, "thickness_mm", parser, path), f"{path}/thickness_mm", positive=True
                ),
                height=parser.mm(
                    _require(value, "height_mm", parser, path), f"{path}/height_mm", positive=True
                ),
                class_name=parser.text(_require(value, "class_name", parser, path), f"{path}/class_name"),
            )
        )

    openings: list[Opening] = []
    for index, item in enumerate(parser.sequence(_require(root, "openings", parser, ""), "/openings")):
        path = f"/openings/{index}"
        probe = parser.mapping(item, path, set(item.keys()) if isinstance(item, Mapping) else set())
        kind = parser.text(_require(probe, "kind", parser, path), f"{path}/kind")
        common = {"kind", "id", "wall_id", "offset_mm", "width_mm", "height_mm"}
        allowed = common | ({"hinge", "swing_into_room_id"} if kind == "door" else {"sill_height_mm"})
        value = parser.mapping(item, path, allowed)
        identifier = parser.text(_require(value, "id", parser, path), f"{path}/id")
        wall_id = parser.text(_require(value, "wall_id", parser, path), f"{path}/wall_id")
        offset = parser.mm(_require(value, "offset_mm", parser, path), f"{path}/offset_mm")
        width = parser.mm(_require(value, "width_mm", parser, path), f"{path}/width_mm", positive=True)
        height = parser.mm(_require(value, "height_mm", parser, path), f"{path}/height_mm", positive=True)
        if kind == "door":
            hinge = parser.text(_require(value, "hinge", parser, path), f"{path}/hinge")
            if hinge not in {"start", "end"}:
                parser.problem(f"{path}/hinge", "door.hinge_invalid", "Hinge must be start or end.")
                hinge = "start"
            openings.append(
                Door(
                    identifier,
                    wall_id,
                    offset,
                    width,
                    height,
                    hinge,  # type: ignore[arg-type]
                    parser.text(
                        _require(value, "swing_into_room_id", parser, path), f"{path}/swing_into_room_id"
                    ),
                )
            )
        elif kind == "window":
            openings.append(
                Window(
                    identifier,
                    wall_id,
                    offset,
                    width,
                    height,
                    parser.mm(
                        _require(value, "sill_height_mm", parser, path), f"{path}/sill_height_mm"
                    ),
                )
            )
        else:
            parser.problem(f"{path}/kind", "opening.kind_invalid", "Opening kind must be door or window.")

    furniture: list[Furniture] = []
    for index, item in enumerate(parser.sequence(root.get("furniture", []), "/furniture")):
        path = f"/furniture/{index}"
        value = parser.mapping(item, path, {"id", "room_id", "bounds", "class_name", "access_zones"})
        zones: list[AccessZone] = []
        for zone_index, zone_item in enumerate(parser.sequence(value.get("access_zones", []), f"{path}/access_zones")):
            zone_path = f"{path}/access_zones/{zone_index}"
            zone = parser.mapping(zone_item, zone_path, {"id", "bounds", "minimum_clear_width_mm"})
            zones.append(
                AccessZone(
                    parser.text(_require(zone, "id", parser, zone_path), f"{zone_path}/id"),
                    parser.rect(_require(zone, "bounds", parser, zone_path), f"{zone_path}/bounds"),
                    parser.mm(
                        _require(zone, "minimum_clear_width_mm", parser, zone_path),
                        f"{zone_path}/minimum_clear_width_mm",
                        positive=True,
                    ),
                )
            )
        furniture.append(
            Furniture(
                parser.text(_require(value, "id", parser, path), f"{path}/id"),
                parser.text(_require(value, "room_id", parser, path), f"{path}/room_id"),
                parser.rect(_require(value, "bounds", parser, path), f"{path}/bounds"),
                parser.text(value.get("class_name", "A-Furn"), f"{path}/class_name"),
                tuple(sorted(zones, key=lambda item: item.id)),
            )
        )

    circulation: list[CirculationZone] = []
    for index, item in enumerate(parser.sequence(root.get("circulation", []), "/circulation")):
        path = f"/circulation/{index}"
        value = parser.mapping(item, path, {"id", "room_id", "bounds", "minimum_clear_width_mm"})
        circulation.append(
            CirculationZone(
                parser.text(_require(value, "id", parser, path), f"{path}/id"),
                parser.text(_require(value, "room_id", parser, path), f"{path}/room_id"),
                parser.rect(_require(value, "bounds", parser, path), f"{path}/bounds"),
                parser.mm(
                    _require(value, "minimum_clear_width_mm", parser, path),
                    f"{path}/minimum_clear_width_mm",
                    positive=True,
                ),
            )
        )

    annotations: list[Annotation] = []
    for index, item in enumerate(parser.sequence(root.get("annotations", []), "/annotations")):
        path = f"/annotations/{index}"
        probe = parser.mapping(item, path, set(item.keys()) if isinstance(item, Mapping) else set())
        kind = parser.text(_require(probe, "kind", parser, path), f"{path}/kind")
        if kind == "room_label":
            value = parser.mapping(item, path, {"kind", "id", "room_id", "bounds"})
            annotations.append(
                RoomLabel(
                    parser.text(_require(value, "id", parser, path), f"{path}/id"),
                    parser.text(_require(value, "room_id", parser, path), f"{path}/room_id"),
                    parser.rect(_require(value, "bounds", parser, path), f"{path}/bounds"),
                )
            )
        elif kind == "room_schedule":
            value = parser.mapping(
                item,
                path,
                {"kind", "id", "bounds", "room_ids", "header_height_mm", "minimum_row_height_mm"},
            )
            annotations.append(
                RoomSchedule(
                    parser.text(_require(value, "id", parser, path), f"{path}/id"),
                    parser.rect(_require(value, "bounds", parser, path), f"{path}/bounds"),
                    tuple(
                        parser.text(room_id, f"{path}/room_ids/{room_index}")
                        for room_index, room_id in enumerate(
                            parser.sequence(_require(value, "room_ids", parser, path), f"{path}/room_ids")
                        )
                    ),
                    parser.mm(
                        _require(value, "header_height_mm", parser, path), f"{path}/header_height_mm"
                    ),
                    parser.mm(
                        _require(value, "minimum_row_height_mm", parser, path),
                        f"{path}/minimum_row_height_mm",
                        positive=True,
                    ),
                )
            )
        else:
            parser.problem(f"{path}/kind", "annotation.kind_invalid", "Annotation kind is unsupported.")

    program = parser.mapping(
        _require(root, "program", parser, ""), "/program", {"entry", "required_adjacencies"}
    )
    entry_value = parser.mapping(
        _require(program, "entry", parser, "/program"),
        "/program/entry",
        {"door_id", "arrival_room_id", "must_reach_room_ids"},
    )
    entry = EntryRequirement(
        parser.text(_require(entry_value, "door_id", parser, "/program/entry"), "/program/entry/door_id"),
        parser.text(
            _require(entry_value, "arrival_room_id", parser, "/program/entry"),
            "/program/entry/arrival_room_id",
        ),
        tuple(
            parser.text(value, f"/program/entry/must_reach_room_ids/{index}")
            for index, value in enumerate(
                parser.sequence(
                    _require(entry_value, "must_reach_room_ids", parser, "/program/entry"),
                    "/program/entry/must_reach_room_ids",
                )
            )
        ),
    )
    adjacencies: list[AdjacencyRequirement] = []
    for index, item in enumerate(
        parser.sequence(
            _require(program, "required_adjacencies", parser, "/program"),
            "/program/required_adjacencies",
        )
    ):
        path = f"/program/required_adjacencies/{index}"
        value = parser.mapping(item, path, {"id", "room_ids", "minimum_shared_boundary_mm"})
        room_ids = parser.sequence(_require(value, "room_ids", parser, path), f"{path}/room_ids")
        if len(room_ids) != 2:
            parser.problem(f"{path}/room_ids", "adjacency.room_arity", "Adjacency requires two room IDs.")
            room_ids = (room_ids + ["", ""])[:2]
        adjacencies.append(
            AdjacencyRequirement(
                parser.text(_require(value, "id", parser, path), f"{path}/id"),
                (
                    parser.text(room_ids[0], f"{path}/room_ids/0"),
                    parser.text(room_ids[1], f"{path}/room_ids/1"),
                ),
                parser.mm(
                    _require(value, "minimum_shared_boundary_mm", parser, path),
                    f"{path}/minimum_shared_boundary_mm",
                    positive=True,
                ),
            )
        )

    if not rooms:
        parser.problem("/rooms", "rooms.empty", "At least one room is required.")

    identifiers: dict[str, str] = {}
    for kind, values in (
        ("room", rooms),
        ("wall", walls),
        ("opening", openings),
        ("furniture", furniture),
        ("circulation", circulation),
        ("annotation", annotations),
        ("adjacency", adjacencies),
    ):
        for value in values:
            identifier = value.id
            if identifier in identifiers:
                parser.problem(
                    "/",
                    "id.duplicate",
                    f"ID {identifier!r} is used by both {identifiers[identifier]} and {kind}.",
                )
            identifiers[identifier] = kind

    room_ids = {room.id for room in rooms}
    wall_ids = {wall.id for wall in walls}
    opening_by_id = {opening.id: opening for opening in openings}
    for opening in openings:
        if opening.wall_id not in wall_ids:
            parser.problem("/openings", "reference.wall_missing", f"Opening {opening.id!r} references unknown wall {opening.wall_id!r}.")
        if isinstance(opening, Door) and opening.swing_into_room_id not in room_ids:
            parser.problem(
                "/openings", "reference.room_missing", f"Door {opening.id!r} references unknown swing room {opening.swing_into_room_id!r}."
            )
    for item in (*furniture, *circulation):
        if item.room_id not in room_ids:
            parser.problem("/", "reference.room_missing", f"{item.id!r} references unknown room {item.room_id!r}.")
    for annotation in annotations:
        if isinstance(annotation, RoomLabel) and annotation.room_id not in room_ids:
            parser.problem("/annotations", "reference.room_missing", f"Label {annotation.id!r} references an unknown room.")
        if isinstance(annotation, RoomSchedule):
            missing = sorted(set(annotation.room_ids) - room_ids)
            if missing:
                parser.problem("/annotations", "reference.room_missing", f"Schedule references unknown rooms: {missing}.")
    if entry.door_id not in opening_by_id or not isinstance(opening_by_id.get(entry.door_id), Door):
        parser.problem("/program/entry/door_id", "reference.entry_door_missing", "Entry must reference a door.")
    for room_id in (entry.arrival_room_id, *entry.must_reach_room_ids):
        if room_id not in room_ids:
            parser.problem("/program/entry", "reference.room_missing", f"Entry program references unknown room {room_id!r}.")
    for adjacency in adjacencies:
        missing = sorted(set(adjacency.room_ids) - room_ids)
        if missing:
            parser.problem("/program/required_adjacencies", "reference.room_missing", f"Adjacency references unknown rooms: {missing}.")

    if parser.problems:
        raise PlanValidationError(parser.problems)

    return PlanManifest(
        schema="vectorworks.plan-quality/v1",
        units="mm",
        plan_id=plan_id,
        assumptions=assumptions,
        entry=entry,
        required_adjacencies=tuple(sorted(adjacencies, key=lambda item: item.id)),
        rooms=tuple(sorted(rooms, key=lambda item: item.id)),
        walls=tuple(sorted(walls, key=lambda item: item.id)),
        openings=tuple(sorted(openings, key=lambda item: item.id)),
        furniture=tuple(sorted(furniture, key=lambda item: item.id)),
        circulation=tuple(sorted(circulation, key=lambda item: item.id)),
        annotations=tuple(sorted(annotations, key=lambda item: item.id)),
    )


def _canonical(value: object) -> object:
    if dataclasses.is_dataclass(value):
        return {field.name: _canonical(getattr(value, field.name)) for field in dataclasses.fields(value)}
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_canonical(item) for item in value)
    if isinstance(value, Mapping):
        return {str(key): _canonical(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    return value


def _mm(value: int) -> str:
    return format(Decimal(value) / Decimal(1000), "f")


def _m2(value: int) -> str:
    return format(Decimal(value) / Decimal(1_000_000_000_000), ".3f")


def _measurement(metric: str, actual: str, required: str, unit: str) -> dict[str, str]:
    return {"metric": metric, "actual": actual, "required": required, "unit": unit}


def _issue(
    severity: Severity,
    code: str,
    object_ids: Iterable[str],
    message: str,
    *,
    action: str,
    target_ids: Iterable[str],
    measurements: Iterable[dict[str, str]] = (),
) -> dict[str, Any]:
    return {
        "severity": severity,
        "code": code,
        "object_ids": sorted(set(object_ids)),
        "measurements": list(measurements),
        "suggestion": {
            "action": action,
            "target_ids": sorted(set(target_ids)),
            "message": message,
        },
    }


def _incident_rooms(plan: PlanManifest, wall: Wall) -> tuple[Room, ...]:
    return tuple(
        room
        for room in plan.rooms
        if segment_boundary_overlap(wall.segment, room.bounds, plan.assumptions.wall_room_tolerance)
        > plan.assumptions.geometry_tolerance
    )


def analyze_plan(plan: PlanManifest) -> dict[str, Any]:
    assumptions = plan.assumptions
    room_by_id = {room.id: room for room in plan.rooms}
    wall_by_id = {wall.id: wall for wall in plan.walls}
    opening_by_id = {opening.id: opening for opening in plan.openings}
    issues: list[dict[str, Any]] = []

    for room in plan.rooms:
        actual_area = Decimal(room.bounds.area) / Decimal(1_000_000_000_000)
        difference = abs(actual_area - room.target_area_m2)
        if difference > room.area_tolerance_m2:
            issues.append(
                _issue(
                    "error",
                    "room.area_outside_tolerance",
                    [f"room:{room.id}"],
                    "Resize the room or update the declared program target.",
                    action="resize_room",
                    target_ids=[f"room:{room.id}"],
                    measurements=[
                        _measurement("area", format(actual_area, ".3f"), format(room.target_area_m2, "f"), "m2"),
                        _measurement("area_difference", format(difference, ".3f"), format(room.area_tolerance_m2, "f"), "m2"),
                    ],
                )
            )
        minimum_dimension = min(room.bounds.width, room.bounds.height)
        if minimum_dimension < room.minimum_dimension:
            issues.append(
                _issue(
                    "error",
                    "room.minimum_dimension",
                    [f"room:{room.id}"],
                    "Increase the room's clear minimum dimension.",
                    action="resize_room",
                    target_ids=[f"room:{room.id}"],
                    measurements=[
                        _measurement("minimum_dimension", _mm(minimum_dimension), _mm(room.minimum_dimension), "mm")
                    ],
                )
            )

    for index, left in enumerate(plan.rooms):
        for right in plan.rooms[index + 1 :]:
            overlap = overlap_area(left.bounds, right.bounds)
            if overlap > assumptions.geometry_tolerance**2:
                issues.append(
                    _issue(
                        "error",
                        "room.overlap",
                        [f"room:{left.id}", f"room:{right.id}"],
                        "Separate the room boundaries so their clear areas do not overlap.",
                        action="move_room",
                        target_ids=[f"room:{right.id}"],
                        measurements=[_measurement("overlap_area", _m2(overlap), "0", "m2")],
                    )
                )

    adjacency_tolerance = max(assumptions.wall_room_tolerance * 2, assumptions.geometry_tolerance)
    for adjacency in plan.required_adjacencies:
        left = room_by_id[adjacency.room_ids[0]]
        right = room_by_id[adjacency.room_ids[1]]
        shared = shared_boundary_length(left.bounds, right.bounds, adjacency_tolerance)
        if shared < adjacency.minimum_shared_boundary:
            issues.append(
                _issue(
                    "error",
                    "adjacency.missing_shared_boundary",
                    [f"room:{left.id}", f"room:{right.id}", f"adjacency:{adjacency.id}"],
                    "Move or resize the rooms to provide the required shared boundary.",
                    action="add_adjacency",
                    target_ids=[f"room:{left.id}", f"room:{right.id}"],
                    measurements=[
                        _measurement("shared_boundary", _mm(shared), _mm(adjacency.minimum_shared_boundary), "mm")
                    ],
                )
            )

    incident_by_wall: dict[str, tuple[Room, ...]] = {}
    for wall in plan.walls:
        incident = _incident_rooms(plan, wall)
        incident_by_wall[wall.id] = incident
        if not incident:
            issues.append(
                _issue(
                    "error",
                    "wall.not_incident_to_room",
                    [f"wall:{wall.id}"],
                    "Align the wall with at least one room boundary or adjust the explicit wall-room tolerance.",
                    action="move_wall",
                    target_ids=[f"wall:{wall.id}"],
                )
            )
        elif len(incident) > 2:
            issues.append(
                _issue(
                    "error",
                    "wall.ambiguous_room_incidence",
                    [f"wall:{wall.id}", *(f"room:{room.id}" for room in incident)],
                    "Split the wall so each segment separates at most two rooms.",
                    action="split_wall",
                    target_ids=[f"wall:{wall.id}"],
                )
            )

    openings_by_wall: dict[str, list[Opening]] = defaultdict(list)
    for opening in plan.openings:
        openings_by_wall[opening.wall_id].append(opening)
        wall = wall_by_id[opening.wall_id]
        available_start = opening.offset - opening.width // 2
        available_end = wall.segment.length - (opening.offset + opening.width // 2)
        observed = min(available_start, available_end)
        if observed < assumptions.minimum_wall_end_clearance:
            issues.append(
                _issue(
                    "error",
                    "opening.wall_end_clearance",
                    [f"opening:{opening.id}", f"wall:{wall.id}"],
                    "Move or resize the opening to preserve wall at both ends.",
                    action="relocate_opening",
                    target_ids=[f"opening:{opening.id}"],
                    measurements=[
                        _measurement(
                            "wall_end_clearance", _mm(observed), _mm(assumptions.minimum_wall_end_clearance), "mm"
                        )
                    ],
                )
            )

    for wall_id, openings in openings_by_wall.items():
        ordered = sorted(openings, key=lambda opening: opening.offset)
        for left, right in zip(ordered, ordered[1:]):
            clear_gap = right.offset - right.width // 2 - (left.offset + left.width // 2)
            if clear_gap < assumptions.minimum_wall_end_clearance:
                issues.append(
                    _issue(
                        "error",
                        "opening.overlap_or_too_close",
                        [f"opening:{left.id}", f"opening:{right.id}", f"wall:{wall_id}"],
                        "Separate the openings on their host wall.",
                        action="relocate_opening",
                        target_ids=[f"opening:{right.id}"],
                        measurements=[
                            _measurement(
                                "opening_clear_gap", _mm(clear_gap), _mm(assumptions.minimum_wall_end_clearance), "mm"
                            )
                        ],
                    )
                )

    door_sweeps: dict[str, tuple[tuple[str, Rect], ...]] = {}
    connectivity: dict[str, set[str]] = defaultdict(set)
    exterior_windows: dict[str, int] = defaultdict(int)
    for opening in plan.openings:
        wall = wall_by_id[opening.wall_id]
        incident = incident_by_wall.get(wall.id, ())
        if isinstance(opening, Door):
            incident_ids = {room.id for room in incident}
            possible_sweeps = tuple(
                (
                    room.id,
                    door_swing_envelope(
                        wall.segment,
                        offset=opening.offset,
                        width=opening.width,
                        hinge=opening.hinge,
                        target_room=room.bounds,
                    ),
                )
                for room in incident
            )
            door_sweeps[opening.id] = possible_sweeps
            if opening.swing_into_room_id not in incident_ids:
                issues.append(
                    _issue(
                        "error",
                        "door.swing_room_not_incident",
                        [f"door:{opening.id}", f"room:{opening.swing_into_room_id}", f"wall:{wall.id}"],
                        "Host the door on a wall incident to its swing room or correct the swing-room intent.",
                        action="relocate_opening",
                        target_ids=[f"door:{opening.id}"],
                    )
                )
            else:
                sweep = dict(possible_sweeps)[opening.swing_into_room_id]
                if not contains(
                    room_by_id[opening.swing_into_room_id].bounds,
                    sweep,
                    assumptions.wall_room_tolerance,
                ):
                    issues.append(
                        _issue(
                            "error",
                            "door.swing_outside_room",
                            [f"door:{opening.id}", f"room:{opening.swing_into_room_id}"],
                            "Move the door away from the room corner or reverse its hinge.",
                            action="reverse_door_swing",
                            target_ids=[f"door:{opening.id}"],
                        )
                    )
            if len(incident) == 2:
                left, right = incident
                connectivity[left.id].add(right.id)
                connectivity[right.id].add(left.id)
        elif len(incident) == 1:
            exterior_windows[incident[0].id] += 1
        else:
            issues.append(
                _issue(
                    "error",
                    "window.not_exterior",
                    [f"window:{opening.id}", f"wall:{wall.id}"],
                    "Place the window on a wall incident to exactly one room.",
                    action="relocate_opening",
                    target_ids=[f"window:{opening.id}"],
                )
            )

    entry_door = opening_by_id.get(plan.entry.door_id)
    if isinstance(entry_door, Door):
        incident_ids = {room.id for room in incident_by_wall.get(entry_door.wall_id, ())}
        if plan.entry.arrival_room_id not in incident_ids or len(incident_ids) != 1:
            issues.append(
                _issue(
                    "error",
                    "entry.arrival_room_mismatch",
                    [f"door:{entry_door.id}", f"room:{plan.entry.arrival_room_id}"],
                    "Move the entry door to an exterior wall of the declared arrival room.",
                    action="relocate_opening",
                    target_ids=[f"door:{entry_door.id}"],
                )
            )
    reached = {plan.entry.arrival_room_id}
    queue: deque[str] = deque(reached)
    while queue:
        current = queue.popleft()
        for neighbour in sorted(connectivity.get(current, ())):
            if neighbour not in reached:
                reached.add(neighbour)
                queue.append(neighbour)
    for destination in plan.entry.must_reach_room_ids:
        if destination not in reached:
            issues.append(
                _issue(
                    "error",
                    "entry.destination_unreachable",
                    [f"room:{plan.entry.arrival_room_id}", f"room:{destination}"],
                    "Add a connected sequence of internal doors from the entry room.",
                    action="add_door",
                    target_ids=[f"room:{destination}"],
                )
            )

    for room in plan.rooms:
        observed = exterior_windows.get(room.id, 0)
        if observed < room.minimum_exterior_windows:
            issues.append(
                _issue(
                    "error",
                    "window.exterior_requirement",
                    [f"room:{room.id}"],
                    "Add an exterior window to the room.",
                    action="add_window",
                    target_ids=[f"room:{room.id}"],
                    measurements=[
                        _measurement("exterior_windows", str(observed), str(room.minimum_exterior_windows), "count")
                    ],
                )
            )

    for furniture in plan.furniture:
        room = room_by_id[furniture.room_id]
        if not contains(room.bounds, furniture.bounds, assumptions.geometry_tolerance):
            issues.append(
                _issue(
                    "error",
                    "furniture.outside_room",
                    [f"furniture:{furniture.id}", f"room:{room.id}"],
                    "Move or resize the furniture footprint so it remains in its assigned room.",
                    action="move_furniture",
                    target_ids=[f"furniture:{furniture.id}"],
                )
            )
        for door_id, sweeps in door_sweeps.items():
            for room_id, sweep in sweeps:
                if room_id == furniture.room_id and overlap_area(furniture.bounds, sweep) > assumptions.geometry_tolerance**2:
                    issues.append(
                        _issue(
                            "error",
                            "door.possible_swing_furniture_collision",
                            [f"door:{door_id}", f"furniture:{furniture.id}", f"room:{room_id}"],
                            "Keep both possible native swing sides clear, or move the furniture/opening.",
                            action="move_furniture",
                            target_ids=[f"furniture:{furniture.id}"],
                        )
                    )

    for index, left in enumerate(plan.furniture):
        for right in plan.furniture[index + 1 :]:
            overlap = overlap_area(left.bounds, right.bounds)
            if overlap > assumptions.geometry_tolerance**2:
                issues.append(
                    _issue(
                        "error",
                        "furniture.collision",
                        [f"furniture:{left.id}", f"furniture:{right.id}"],
                        "Separate the furniture footprints.",
                        action="move_furniture",
                        target_ids=[f"furniture:{right.id}"],
                        measurements=[_measurement("overlap_area", _m2(overlap), "0", "m2")],
                    )
                )

    furniture_by_id = {item.id: item for item in plan.furniture}
    for furniture in plan.furniture:
        room = room_by_id[furniture.room_id]
        for zone in furniture.access_zones:
            if min(zone.bounds.width, zone.bounds.height) < zone.minimum_clear_width:
                issues.append(
                    _issue(
                        "error",
                        "access.too_narrow",
                        [f"access:{zone.id}", f"furniture:{furniture.id}"],
                        "Enlarge the declared access zone.",
                        action="resize_access_zone",
                        target_ids=[f"access:{zone.id}"],
                        measurements=[
                            _measurement(
                                "clear_width",
                                _mm(min(zone.bounds.width, zone.bounds.height)),
                                _mm(zone.minimum_clear_width),
                                "mm",
                            )
                        ],
                    )
                )
            if not contains(room.bounds, zone.bounds, assumptions.geometry_tolerance):
                issues.append(
                    _issue(
                        "error",
                        "access.outside_room",
                        [f"access:{zone.id}", f"room:{room.id}"],
                        "Move or resize the access zone so it remains inside the room.",
                        action="resize_access_zone",
                        target_ids=[f"access:{zone.id}"],
                    )
                )
            for other_id, other in furniture_by_id.items():
                if other_id != furniture.id and overlap_area(zone.bounds, other.bounds) > assumptions.geometry_tolerance**2:
                    issues.append(
                        _issue(
                            "error",
                            "access.blocked",
                            [f"access:{zone.id}", f"furniture:{other.id}"],
                            "Move the obstructing furniture out of the access zone.",
                            action="move_furniture",
                            target_ids=[f"furniture:{other.id}"],
                        )
                    )

    for zone in plan.circulation:
        room = room_by_id[zone.room_id]
        clear_width = min(zone.bounds.width, zone.bounds.height)
        if clear_width < zone.minimum_clear_width:
            issues.append(
                _issue(
                    "error",
                    "circulation.too_narrow",
                    [f"circulation:{zone.id}", f"room:{room.id}"],
                    "Widen the circulation zone.",
                    action="reroute_circulation",
                    target_ids=[f"circulation:{zone.id}"],
                    measurements=[
                        _measurement("clear_width", _mm(clear_width), _mm(zone.minimum_clear_width), "mm")
                    ],
                )
            )
        if not contains(room.bounds, zone.bounds, assumptions.geometry_tolerance):
            issues.append(
                _issue(
                    "error",
                    "circulation.outside_room",
                    [f"circulation:{zone.id}", f"room:{room.id}"],
                    "Keep the circulation zone within its traversable room.",
                    action="reroute_circulation",
                    target_ids=[f"circulation:{zone.id}"],
                )
            )
        for furniture in plan.furniture:
            if overlap_area(zone.bounds, furniture.bounds) > assumptions.geometry_tolerance**2:
                issues.append(
                    _issue(
                        "error",
                        "circulation.blocked",
                        [f"circulation:{zone.id}", f"furniture:{furniture.id}"],
                        "Move the furniture or reroute the clear circulation zone.",
                        action="move_furniture",
                        target_ids=[f"furniture:{furniture.id}"],
                    )
                )

    labels = [annotation for annotation in plan.annotations if isinstance(annotation, RoomLabel)]
    for label in labels:
        room = room_by_id[label.room_id]
        if not contains(room.bounds, label.bounds, assumptions.geometry_tolerance):
            issues.append(
                _issue(
                    "error",
                    "annotation.outside_room",
                    [f"annotation:{label.id}", f"room:{room.id}"],
                    "Move the room label inside its room.",
                    action="move_annotation",
                    target_ids=[f"annotation:{label.id}"],
                )
            )
        clearance_box = label.bounds.expanded(assumptions.annotation_clearance)
        for furniture in plan.furniture:
            if overlap_area(clearance_box, furniture.bounds) > assumptions.geometry_tolerance**2:
                issues.append(
                    _issue(
                        "error",
                        "annotation.furniture_collision",
                        [f"annotation:{label.id}", f"furniture:{furniture.id}"],
                        "Move the label away from furniture and its annotation clearance.",
                        action="move_annotation",
                        target_ids=[f"annotation:{label.id}"],
                    )
                )
        for door_id, sweeps in door_sweeps.items():
            for room_id, sweep in sweeps:
                if room_id == label.room_id and overlap_area(clearance_box, sweep) > assumptions.geometry_tolerance**2:
                    issues.append(
                        _issue(
                            "error",
                            "annotation.door_swing_collision",
                            [f"annotation:{label.id}", f"door:{door_id}", f"room:{room_id}"],
                            "Move the label outside every possible native door swing envelope.",
                            action="move_annotation",
                            target_ids=[f"annotation:{label.id}"],
                        )
                    )
    for index, left in enumerate(labels):
        for right in labels[index + 1 :]:
            if overlap_area(left.bounds, right.bounds) > assumptions.geometry_tolerance**2:
                issues.append(
                    _issue(
                        "error",
                        "annotation.label_collision",
                        [f"annotation:{left.id}", f"annotation:{right.id}"],
                        "Separate the annotation boxes.",
                        action="move_annotation",
                        target_ids=[f"annotation:{right.id}"],
                    )
                )

    for annotation in plan.annotations:
        if isinstance(annotation, RoomSchedule):
            available = annotation.bounds.height - annotation.header_height
            row_height = available // max(1, len(annotation.room_ids))
            if row_height < annotation.minimum_row_height:
                issues.append(
                    _issue(
                        "error",
                        "schedule.compressed",
                        [f"annotation:{annotation.id}"],
                        "Increase the schedule height or reduce the number of rows.",
                        action="expand_schedule",
                        target_ids=[f"annotation:{annotation.id}"],
                        measurements=[
                            _measurement(
                                "row_height", _mm(row_height), _mm(annotation.minimum_row_height), "mm"
                            )
                        ],
                    )
                )

    issue_order = {name: index for index, name in enumerate(CHECKS)}

    def sort_key(issue: Mapping[str, Any]) -> tuple[object, ...]:
        family = str(issue["code"]).split(".", 1)[0]
        family_alias = {
            "adjacency": "required_adjacency",
            "entry": "entry_connectivity",
            "window": "exterior_windows",
            "door": "door_swing_clearance",
            "furniture": "furniture_clearance",
            "access": "furniture_clearance",
            "circulation": "circulation_clearance",
            "annotation": "annotation_clearance",
            "schedule": "schedule_density",
            "opening": "opening_clearance",
            "wall": "wall_incidence",
            "room": "room_program",
        }.get(family, family)
        return (issue_order.get(family_alias, len(issue_order)), issue["code"], tuple(issue["object_ids"]))

    issues.sort(key=sort_key)
    digest = hashlib.sha256(
        json.dumps(_canonical(plan), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()
    counts = {severity: sum(issue["severity"] == severity for issue in issues) for severity in ("error", "warning", "info")}
    return {
        "schema": "vectorworks.plan-quality-report/v1",
        "manifest_digest": f"sha256:{digest}",
        "passed": not any(issue["severity"] in assumptions.blocking_severities for issue in issues),
        "disclaimer": DISCLAIMER,
        "checks": list(CHECKS),
        "counts": counts,
        "issues": issues,
    }


def evaluate_plan_payload(raw: object) -> dict[str, Any]:
    """Parse and analyze one raw plan payload, raising only for malformed input."""
    return analyze_plan(parse_plan(raw))
