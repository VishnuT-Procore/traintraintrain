"""Timetables: when a platform may receive a deploy.

A timetable is a set of timezone-aware recurring service hours, minus line closures.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from ._compat import pick

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
SEARCH_HORIZON_DAYS = 90

_DURATION_RE = re.compile(r"(\d+)\s*([dhms])")
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(value) -> timedelta:
    """Parse '90s', '30m', '2h', '1h30m', '1d' or a bare number of seconds."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, (int, float)):
        return timedelta(seconds=value)
    text = str(value).strip().lower()
    if text.isdigit():
        return timedelta(seconds=int(text))
    parts = _DURATION_RE.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        raise ValueError(f"invalid duration: {value!r}")
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in parts))


def format_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    out = []
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if secs >= size:
            out.append(f"{secs // size}{unit}")
            secs %= size
    return "".join(out) or "0s"


def _parse_days(value) -> list[int]:
    if value in (None, "all", "*"):
        return list(range(7))
    if isinstance(value, str):
        value = [value]
    result: set[int] = set()
    for item in value:
        item = str(item).strip().lower()
        if "-" in item:  # range like mon-fri (may wrap, e.g. fri-mon)
            a, b = (x.strip()[:3] for x in item.split("-", 1))
            if a not in DAYS or b not in DAYS:
                raise ValueError(f"invalid day range: {item!r}")
            k, j = DAYS.index(a), DAYS.index(b)
            while True:
                result.add(k)
                if k == j:
                    break
                k = (k + 1) % 7
        else:
            if item[:3] not in DAYS:
                raise ValueError(f"invalid day: {item!r}")
            result.add(DAYS.index(item[:3]))
    return sorted(result)


def _parse_hhmm(value) -> time:
    if isinstance(value, int):  # YAML may parse 10:00 as sexagesimal int
        return time(value // 60, value % 60)
    h, m = str(value).split(":")
    return time(int(h), int(m))


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    def __contains__(self, t: datetime) -> bool:
        return self.start <= t < self.end


@dataclass(frozen=True)
class ServiceHours:
    days: tuple[int, ...]
    start: time
    end: time  # if end <= start the service crosses midnight

    def intervals_on(self, d: date, tz: ZoneInfo) -> list[Interval]:
        if d.weekday() not in self.days:
            return []
        start = datetime.combine(d, self.start, tzinfo=tz)
        end_day = d if self.end > self.start else d + timedelta(days=1)
        end = datetime.combine(end_day, self.end, tzinfo=tz)
        return [Interval(start.astimezone(timezone.utc), end.astimezone(timezone.utc))]


@dataclass(frozen=True)
class LineClosure:
    start: datetime
    end: datetime
    reason: str = ""

    @classmethod
    def from_config(cls, raw: dict, default_tz: str = "UTC") -> "LineClosure":
        tz = ZoneInfo(raw.get("timezone", default_tz))

        def conv(v, is_end):
            if isinstance(v, datetime):
                dt = v
            elif isinstance(v, date):
                dt = datetime.combine(v + timedelta(days=1) if is_end else v, time())
            else:
                s = str(v)
                if len(s) == 10:  # plain date; end date is inclusive
                    d = date.fromisoformat(s)
                    dt = datetime.combine(d + timedelta(days=1) if is_end else d, time())
                else:
                    dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(timezone.utc)

        start, end = conv(raw["start"], False), conv(raw["end"], True)
        if end <= start:
            raise ValueError(f"line closure end must be after start: {raw}")
        return cls(start, end, raw.get("reason", ""))


def _subtract(intervals: list[Interval], closures: list[LineClosure]) -> list[Interval]:
    for b in closures:
        out = []
        for iv in intervals:
            if b.end <= iv.start or b.start >= iv.end:
                out.append(iv)
                continue
            if iv.start < b.start:
                out.append(Interval(iv.start, b.start))
            if b.end < iv.end:
                out.append(Interval(b.end, iv.end))
        intervals = out
    return intervals


@dataclass
class Timetable:
    name: str
    timezone: str = "UTC"
    service_hours: list[ServiceHours] = field(default_factory=list)
    closures: list[LineClosure] = field(default_factory=list)
    # Only start a deploy if at least this much of the service period remains.
    min_remaining: timedelta = timedelta(0)

    @classmethod
    def from_config(cls, name: str, raw: dict, line_closures: list[LineClosure]) -> "Timetable":
        tzname = raw.get("timezone", "UTC")
        ZoneInfo(tzname)  # validate
        hours = []
        for w in pick(raw, "service_hours", "windows", default=[]) or []:
            hours.append(ServiceHours(
                days=tuple(_parse_days(w.get("days"))),
                start=_parse_hhmm(w.get("start", "00:00")),
                end=_parse_hhmm(w.get("end", "00:00")),
            ))
        if not hours:  # no service hours declared => always in service
            hours.append(ServiceHours(tuple(range(7)), time(0), time(0)))
        closures = list(line_closures) + [
            LineClosure.from_config(b, tzname)
            for b in pick(raw, "line_closures", "blackouts", default=[]) or []
        ]
        return cls(
            name=name,
            timezone=tzname,
            service_hours=hours,
            closures=closures,
            min_remaining=parse_duration(raw.get("min_remaining", 0)),
        )

    def intervals(self, frm: datetime, days: int = SEARCH_HORIZON_DAYS) -> list[Interval]:
        """In-service intervals (UTC, closures removed, merged) overlapping [frm, frm+days)."""
        tz = ZoneInfo(self.timezone)
        local_day = frm.astimezone(tz).date()
        raw: list[Interval] = []
        for offset in range(-1, days + 1):
            d = local_day + timedelta(days=offset)
            for w in self.service_hours:
                raw.extend(w.intervals_on(d, tz))
        raw.sort(key=lambda iv: iv.start)
        merged: list[Interval] = []
        for iv in raw:
            if merged and iv.start <= merged[-1].end:
                if iv.end > merged[-1].end:
                    merged[-1] = Interval(merged[-1].start, iv.end)
            else:
                merged.append(iv)
        return [iv for iv in _subtract(merged, self.closures) if iv.end > frm]

    def can_start(self, t: datetime) -> bool:
        return self.next_start(t) == t

    def next_start(self, t: datetime) -> datetime:
        """Earliest instant >= t at which a deploy may begin."""
        t = t.astimezone(timezone.utc)
        for iv in self.intervals(t):
            candidate = max(iv.start, t)
            if iv.end - candidate >= max(self.min_remaining, timedelta(seconds=1)):
                return candidate
        raise NoServiceError(
            f"timetable {self.name!r} has no usable service in the next "
            f"{SEARCH_HORIZON_DAYS} days after {t.isoformat()}"
        )

    def service_end(self, t: datetime) -> datetime | None:
        for iv in self.intervals(t, days=2):
            if t in iv:
                return iv.end
        return None

    def active_closure(self, t: datetime) -> LineClosure | None:
        return next((b for b in self.closures if b.start <= t < b.end), None)


class NoServiceError(RuntimeError):
    pass
