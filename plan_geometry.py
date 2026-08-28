"""Exact fixed-point geometry used by the host-side plan-quality engine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, order=True)
class Point:
    x: int
    y: int


@dataclass(frozen=True)
class Rect:
    left: int
    bottom: int
    right: int
    top: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.top - self.bottom

    @property
    def area(self) -> int:
        return self.width * self.height

    @property
    def centre(self) -> Point:
        return Point((self.left + self.right) // 2, (self.bottom + self.top) // 2)

    def expanded(self, amount: int) -> "Rect":
        return Rect(
            self.left - amount,
            self.bottom - amount,
            self.right + amount,
            self.top + amount,
        )


@dataclass(frozen=True)
class Segment:
    start: Point
    end: Point

    @property
    def horizontal(self) -> bool:
        return self.start.y == self.end.y

    @property
    def vertical(self) -> bool:
        return self.start.x == self.end.x

    @property
    def length(self) -> int:
        if self.horizontal:
            return abs(self.end.x - self.start.x)
        if self.vertical:
            return abs(self.end.y - self.start.y)
        raise ValueError("segment must be horizontal or vertical")


def contains(outer: Rect, inner: Rect, tolerance: int = 0) -> bool:
    return (
        inner.left >= outer.left - tolerance
        and inner.bottom >= outer.bottom - tolerance
        and inner.right <= outer.right + tolerance
        and inner.top <= outer.top + tolerance
    )


def overlap_size(left: Rect, right: Rect) -> tuple[int, int]:
    return (
        max(0, min(left.right, right.right) - max(left.left, right.left)),
        max(0, min(left.top, right.top) - max(left.bottom, right.bottom)),
    )


def overlap_area(left: Rect, right: Rect) -> int:
    width, height = overlap_size(left, right)
    return width * height


def shared_boundary_length(left: Rect, right: Rect, tolerance: int) -> int:
    """Return shared edge length for separated/touching orthogonal room boxes."""
    x_overlap, y_overlap = overlap_size(left, right)
    horizontal_gap = min(abs(left.right - right.left), abs(right.right - left.left))
    vertical_gap = min(abs(left.top - right.bottom), abs(right.top - left.bottom))
    shared = 0
    if horizontal_gap <= tolerance:
        shared = max(shared, y_overlap)
    if vertical_gap <= tolerance:
        shared = max(shared, x_overlap)
    return shared


def segment_boundary_overlap(segment: Segment, rect: Rect, tolerance: int) -> int:
    """Length of a wall segment incident to a rectangle boundary within tolerance."""
    if segment.horizontal:
        if min(abs(segment.start.y - rect.bottom), abs(segment.start.y - rect.top)) > tolerance:
            return 0
        segment_left = min(segment.start.x, segment.end.x)
        segment_right = max(segment.start.x, segment.end.x)
        return max(0, min(segment_right, rect.right) - max(segment_left, rect.left))
    if segment.vertical:
        if min(abs(segment.start.x - rect.left), abs(segment.start.x - rect.right)) > tolerance:
            return 0
        segment_bottom = min(segment.start.y, segment.end.y)
        segment_top = max(segment.start.y, segment.end.y)
        return max(0, min(segment_top, rect.top) - max(segment_bottom, rect.bottom))
    return 0


def point_on_segment(segment: Segment, offset: int) -> Point:
    if offset < 0 or offset > segment.length:
        raise ValueError("offset lies outside segment")
    if segment.horizontal:
        direction = 1 if segment.end.x >= segment.start.x else -1
        return Point(segment.start.x + direction * offset, segment.start.y)
    if segment.vertical:
        direction = 1 if segment.end.y >= segment.start.y else -1
        return Point(segment.start.x, segment.start.y + direction * offset)
    raise ValueError("segment must be horizontal or vertical")


def door_swing_envelope(
    wall: Segment,
    *,
    offset: int,
    width: int,
    hinge: str,
    target_room: Rect,
) -> Rect:
    """Return a conservative 90-degree swing square directed into target_room."""
    centre = point_on_segment(wall, offset)
    half = width // 2
    if wall.horizontal:
        wall_direction = 1 if wall.end.x >= wall.start.x else -1
        hinge_x = centre.x - wall_direction * half if hinge == "start" else centre.x + wall_direction * half
        leaf_x = hinge_x + wall_direction * width if hinge == "start" else hinge_x - wall_direction * width
        room_direction = 1 if target_room.centre.y >= wall.start.y else -1
        return Rect(
            min(hinge_x, leaf_x),
            min(wall.start.y, wall.start.y + room_direction * width),
            max(hinge_x, leaf_x),
            max(wall.start.y, wall.start.y + room_direction * width),
        )

    wall_direction = 1 if wall.end.y >= wall.start.y else -1
    hinge_y = centre.y - wall_direction * half if hinge == "start" else centre.y + wall_direction * half
    leaf_y = hinge_y + wall_direction * width if hinge == "start" else hinge_y - wall_direction * width
    room_direction = 1 if target_room.centre.x >= wall.start.x else -1
    return Rect(
        min(wall.start.x, wall.start.x + room_direction * width),
        min(hinge_y, leaf_y),
        max(wall.start.x, wall.start.x + room_direction * width),
        max(hinge_y, leaf_y),
    )
