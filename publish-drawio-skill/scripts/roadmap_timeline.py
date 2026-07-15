#!/usr/bin/env python3
"""Deterministic coordinate transforms for every supported roadmap time scale."""
from __future__ import annotations

import datetime as dt


def parse_date(value):
    return dt.date.fromisoformat(str(value))


def _floor(value, scale):
    d = parse_date(value)
    if scale == "week":
        return d - dt.timedelta(days=d.weekday())
    if scale == "quarter":
        return dt.date(d.year, 1 + 3 * ((d.month - 1) // 3), 1)
    if scale == "date":
        return d
    return dt.date(d.year, d.month, 1)


def _next(value, scale):
    if scale == "date":
        return value + dt.timedelta(days=1)
    if scale == "week":
        return value + dt.timedelta(days=7)
    count = 3 if scale == "quarter" else 1
    index = value.year * 12 + value.month - 1 + count
    return dt.date(index // 12, index % 12 + 1, 1)


def _label(value, scale):
    if scale == "week":
        year, week, _ = value.isocalendar()
        return f"{year}-W{week:02d}"
    if scale == "quarter":
        return f"{value.year}-Q{((value.month - 1) // 3) + 1}"
    if scale == "date":
        return value.isoformat()
    return value.strftime("%Y-%m")


def _values(model, scale):
    values = []
    order_mode = scale == "order"
    for section in (model, model.get("baseline") or {}):
        for task in section.get("tasks", []) or []:
            keys = ("start_order", "end_order") if order_mode else ("start", "end")
            values.extend(task[k] if order_mode else parse_date(task[k]) for k in keys if task.get(k) is not None)
        for milestone in section.get("milestones", []) or []:
            key = "order" if order_mode else "date"
            if milestone.get(key) is not None:
                values.append(milestone[key] if order_mode else parse_date(milestone[key]))
            for revision in milestone.get("history", []) or []:
                if revision.get(key) is not None:
                    values.append(revision[key] if order_mode else parse_date(revision[key]))
    return values


class TimelineAxis:
    def __init__(self, model, left=180.0, column_width=170.0):
        self.scale = model.get("time_scale", "month")
        self.left = float(left)
        self.column_width = float(column_width)
        values = _values(model, self.scale)
        if self.scale == "order":
            self.ticks = sorted(set(int(v) for v in values)) or [0]
            if len(self.ticks) == 1:
                self.ticks.append(self.ticks[0] + 1)
            self.labels = [str(v) for v in self.ticks]
        else:
            values = values or [dt.date(1970, 1, 1)]
            cursor, last = _floor(min(values), self.scale), _floor(max(values), self.scale)
            self.ticks = []
            while cursor <= last:
                self.ticks.append(cursor)
                cursor = _next(cursor, self.scale)
            if len(self.ticks) == 1:
                self.ticks.append(_next(self.ticks[0], self.scale))
            self.labels = [_label(v, self.scale) for v in self.ticks]

    @property
    def width(self):
        return self.left + len(self.ticks) * self.column_width + 120

    def x(self, value):
        if self.scale == "order":
            value, low, high = int(value), self.ticks[0], self.ticks[-1]
            ratio = 0.0 if high == low else (value - low) / (high - low)
            return self.left + ratio * self.column_width * (len(self.ticks) - 1)
        value = parse_date(value)
        for index, tick in enumerate(self.ticks):
            nxt = _next(tick, self.scale)
            if value < nxt or index == len(self.ticks) - 1:
                ratio = (value - tick).days / max(1, (nxt - tick).days)
                return self.left + (index + ratio) * self.column_width
        return self.left + (len(self.ticks) - 1) * self.column_width

    def task_span(self, task):
        if self.scale == "order":
            return self.x(task["start_order"]), self.x(task["end_order"])
        return self.x(task["start"]), self.x(task["end"])

    def milestone_x(self, milestone):
        return self.x(milestone["order"] if self.scale == "order" else milestone["date"])
