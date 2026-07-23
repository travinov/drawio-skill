"""Pure, deterministic geometry helpers shared by draw.io layout checks."""
from __future__ import annotations

import math


Point = tuple[float, float]
Segment = tuple[Point, Point]
Rect = tuple[float, float, float, float]


def canonical_segment(a: Point, b: Point) -> Segment:
    """Return a segment with endpoints in stable lexical grid order."""
    return (a, b) if a <= b else (b, a)


def route_segments(points: list[Point]) -> list[Segment]:
    """Return non-zero consecutive segments from a route."""
    return [
        (first, second)
        for first, second in zip(points, points[1:])
        if first != second
    ]


def _cross(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def collinear_overlap(first: Segment, second: Segment, *, epsilon: float = 1e-6) -> float:
    """Return the positive shared length of two collinear segments."""
    (a, b), (c, d) = first, second
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = math.hypot(dx, dy)
    if length <= epsilon:
        return 0.0
    if abs(_cross(a, b, c)) > epsilon * length or abs(_cross(a, b, d)) > epsilon * length:
        return 0.0
    ux, uy = dx / length, dy / length
    first_min, first_max = 0.0, length
    second_a = (c[0] - a[0]) * ux + (c[1] - a[1]) * uy
    second_b = (d[0] - a[0]) * ux + (d[1] - a[1]) * uy
    shared = min(first_max, max(second_a, second_b)) - max(first_min, min(second_a, second_b))
    return shared if shared > epsilon else 0.0


def shared_route_length(first: list[Point], second: list[Point]) -> float:
    """Return the union length of collinear pieces two explicit routes share."""
    groups = []
    for left in route_segments(first):
        for right in route_segments(second):
            overlap = collinear_overlap(left, right)
            if not overlap:
                continue
            (a, b), (c, d) = left, right
            dx, dy = b[0] - a[0], b[1] - a[1]
            length = math.hypot(dx, dy)
            ux, uy = dx / length, dy / length
            if ux < -1e-6 or (abs(ux) <= 1e-6 and uy < 0):
                ux, uy = -ux, -uy
            offset = -uy * a[0] + ux * a[1]
            first_a = a[0] * ux + a[1] * uy
            first_b = b[0] * ux + b[1] * uy
            second_a = c[0] * ux + c[1] * uy
            second_b = d[0] * ux + d[1] * uy
            interval = (max(min(first_a, first_b), min(second_a, second_b)),
                        min(max(first_a, first_b), max(second_a, second_b)))
            for group in groups:
                if abs(group[0] - ux) <= 1e-6 and abs(group[1] - uy) <= 1e-6 and abs(group[2] - offset) <= 1e-6:
                    group[3].append(interval)
                    break
            else:
                groups.append([ux, uy, offset, [interval]])
    total = 0.0
    for _, _, _, intervals in groups:
        end = None
        for start, stop in sorted(intervals):
            if end is None:
                current_start, end = start, stop
            elif start <= end + 1e-6:
                end = max(end, stop)
            else:
                total += end - current_start
                current_start, end = start, stop
        if end is not None:
            total += end - current_start
    return total


def rects_overlap(first: Rect, second: Rect, *, clearance: float = 0.0) -> bool:
    """True when two rectangles overlap after expanding them by clearance."""
    ax, ay, aw, ah = first
    bx, by, bw, bh = second
    return (
        ax - clearance < bx + bw + clearance
        and bx - clearance < ax + aw + clearance
        and ay - clearance < by + bh + clearance
        and by - clearance < ay + ah + clearance
    )


def _point_in_or_on_rect(point: Point, rect: Rect) -> bool:
    x, y, width, height = rect
    return x <= point[0] <= x + width and y <= point[1] <= y + height


def segment_hits_rect(segment: Segment, rect: Rect, *, clearance: float = 0.0) -> bool:
    """True when a segment touches or enters a rectangle (with clearance)."""
    x, y, width, height = rect
    expanded = (x - clearance, y - clearance, width + 2 * clearance, height + 2 * clearance)
    a, b = segment
    if _point_in_or_on_rect(a, expanded) or _point_in_or_on_rect(b, expanded):
        return True
    corners = [
        (expanded[0], expanded[1]),
        (expanded[0] + expanded[2], expanded[1]),
        (expanded[0] + expanded[2], expanded[1] + expanded[3]),
        (expanded[0], expanded[1] + expanded[3]),
    ]
    return any(
        _segments_intersect(a, b, start, end)
        for start, end in zip(corners, corners[1:] + corners[:1])
    )


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    def sign(value: float) -> int:
        return 0 if abs(value) <= 1e-9 else (1 if value > 0 else -1)

    o1, o2 = sign(_cross(a, b, c)), sign(_cross(a, b, d))
    o3, o4 = sign(_cross(c, d, a)), sign(_cross(c, d, b))
    if o1 != o2 and o3 != o4:
        return True
    return any(
        _point_on_segment(point, start, end)
        for point, start, end in ((c, a, b), (d, a, b), (a, c, d), (b, c, d))
    )


def _point_on_segment(point: Point, start: Point, end: Point) -> bool:
    return (
        abs(_cross(start, end, point)) <= 1e-9
        and min(start[0], end[0]) - 1e-9 <= point[0] <= max(start[0], end[0]) + 1e-9
        and min(start[1], end[1]) - 1e-9 <= point[1] <= max(start[1], end[1]) + 1e-9
    )


def bend_count(points: list[Point]) -> int:
    """Count non-straight internal turns in an explicit route."""
    segments = route_segments(points)
    return sum(
        1
        for first, second in zip(segments, segments[1:])
        if abs(_cross(first[0], first[1], second[1])) > 1e-6
    )


def manhattan_length(points: list[Point]) -> float:
    """Return the sum of axis distances through a route."""
    return sum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in route_segments(points))


def detour_ratio(points: list[Point]) -> float:
    """Return route Manhattan length divided by endpoint Manhattan distance."""
    if len(points) < 2:
        return 0.0
    direct = abs(points[-1][0] - points[0][0]) + abs(points[-1][1] - points[0][1])
    length = manhattan_length(points)
    if direct == 0:
        return 1.0 if length == 0 else math.inf
    return length / direct


def is_manhattan(points: list[Point], *, epsilon: float = 1e-6) -> bool:
    """True when every non-zero route segment is horizontal or vertical."""
    return all(
        abs(second[0] - first[0]) <= epsilon or abs(second[1] - first[1]) <= epsilon
        for first, second in route_segments(points)
    )
