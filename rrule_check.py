#!/usr/bin/env python3
"""rrule-check: parse, explain, and audit iCalendar recurrence rules (RFC 5545 RRULE).

Recurrence rules look simple ("every other Tuesday") and are not. The
semantics live in RFC 5545 section 3.3.10: an expand/limit table where each
BYxxx part either multiplies or filters the candidate set depending on the
FREQ, special cases for BYDAY with numeric offsets, ISO-style week numbers
that depend on WKST, invalid dates (February 30) silently dropped, and a
DTSTART that always counts as the first occurrence.

This tool implements exactly that: parsing, expansion to concrete instances,
plain-language explanation, and an audit for rules that parse fine but will
not do what their author meant (never-fires, leap-only, week-53-only,
silently-skipped months, unsynchronized DTSTART).

Scope: FREQ=DAILY/WEEKLY/MONTHLY/YEARLY (the day-granularity rules real
calendars are built from). SECONDLY/MINUTELY/HOURLY are rejected with a
clear error. Naive local wall-clock times, like DTSTART without a time zone.
Standard library only, no network.
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Tuple

__version__ = "1.0.0"

WDAY = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
WDAY_NAME = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]
MONTH_NAME = [None, "January", "February", "March", "April", "May", "June",
              "July", "August", "September", "October", "November", "December"]
FREQS = ("SECONDLY", "MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY",
         "YEARLY")

# Safety horizon for "never" proofs and next-instance search.
MAX_YEARS = 100


class RRuleError(ValueError):
    pass


def _parse_int_list(name: str, text: str, lo: int, hi: int,
                    signed: bool = False) -> Tuple[int, ...]:
    out = []
    pat = r"([+-]?)(\d+)" if signed else r"()(\d+)"
    for part in text.split(","):
        m = re.fullmatch(pat, part)
        if not m:
            raise RRuleError(f"{name}: bad value {part!r}")
        sign = -1 if m.group(1) == "-" else 1
        v = sign * int(m.group(2))
        if (signed and (v == 0 or abs(v) > hi)) or (not signed and not (lo <= v <= hi)):
            raise RRuleError(f"{name}: value {v} out of range")
        out.append(v)
    return tuple(out)


def _parse_byday(text: str) -> Tuple[Tuple[Optional[int], int], ...]:
    out = []
    for part in text.split(","):
        m = re.fullmatch(r"([+-]?\d{1,2})?([A-Z]{2})", part.upper())
        if not m or m.group(2) not in WDAY:
            raise RRuleError(f"BYDAY: bad value {part!r}")
        n = int(m.group(1)) if m.group(1) else None
        if n is not None and not (1 <= abs(n) <= 53):
            raise RRuleError(f"BYDAY: offset {n} out of range (1..53)")
        out.append((n, WDAY[m.group(2)]))
    return tuple(out)


def parse_until(text: str) -> datetime:
    m = re.fullmatch(r"(\d{8})(?:T(\d{6})Z?)?", text)
    if not m:
        raise RRuleError(f"UNTIL: bad date {text!r} (want YYYYMMDD or "
                         "YYYYMMDDTHHMMSS)")
    d, t = m.group(1), m.group(2) or "000000"
    try:
        return datetime(int(d[:4]), int(d[4:6]), int(d[6:8]),
                        int(t[:2]), int(t[2:4]), int(t[4:6]))
    except ValueError as e:
        raise RRuleError(f"UNTIL: {e}")


@dataclass(frozen=True)
class RRule:
    freq: str
    interval: int = 1
    count: Optional[int] = None
    until: Optional[datetime] = None
    bymonth: Tuple[int, ...] = ()
    byweekno: Tuple[int, ...] = ()
    byyearday: Tuple[int, ...] = ()
    bymonthday: Tuple[int, ...] = ()
    byday: Tuple[Tuple[Optional[int], int], ...] = ()
    byhour: Tuple[int, ...] = ()
    byminute: Tuple[int, ...] = ()
    bysetpos: Tuple[int, ...] = ()
    wkst: int = 0  # MO
    original: str = ""

    # -- RFC-mandated rejections, applied at parse time -----------------
    def validate(self) -> None:
        if self.freq not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
            raise RRuleError(
                f"FREQ={self.freq} is outside this tool's scope "
                "(DAILY/WEEKLY/MONTHLY/YEARLY); sub-daily rules need a "
                "time-of-day engine, not a calendar one")
        if self.interval < 1:
            raise RRuleError("INTERVAL must be a positive integer")
        if self.count is not None and self.count < 1:
            raise RRuleError("COUNT must be a positive integer")
        if self.count is not None and self.until is not None:
            raise RRuleError("UNTIL and COUNT MUST NOT occur in the same "
                             "recur (RFC 5545 3.3.10)")
        if any(n is not None for n, _ in self.byday):
            if self.freq not in ("MONTHLY", "YEARLY"):
                raise RRuleError(
                    "BYDAY with a numeric value (e.g. 1MO) MUST NOT be used "
                    f"with FREQ={self.freq} (RFC 5545 3.3.10)")
            if self.freq == "YEARLY" and self.byweekno:
                raise RRuleError(
                    "BYDAY with a numeric value MUST NOT be used with "
                    "FREQ=YEARLY when BYWEEKNO is present (RFC 5545 3.3.10)")
        if self.freq == "WEEKLY" and self.bymonthday:
            raise RRuleError("BYMONTHDAY MUST NOT be used with FREQ=WEEKLY "
                             "(RFC 5545 3.3.10)")
        if self.freq in ("DAILY", "WEEKLY", "MONTHLY") and self.byyearday:
            raise RRuleError(f"BYYEARDAY MUST NOT be used with FREQ={self.freq} "
                             "(RFC 5545 3.3.10)")
        if self.freq != "YEARLY" and self.byweekno:
            raise RRuleError("BYWEEKNO MUST NOT be used when FREQ is not "
                             "YEARLY (RFC 5545 3.3.10)")
        if self.bysetpos and not (self.bymonth or self.byweekno or
                                  self.byyearday or self.bymonthday or
                                  self.byday or self.byhour or self.byminute):
            raise RRuleError("BYSETPOS MUST only be used in conjunction with "
                             "another BYxxx rule part (RFC 5545 3.3.10)")


def parse(value: str) -> RRule:
    """Parse an RRULE RECUR value, e.g. 'FREQ=MONTHLY;BYDAY=1FR;COUNT=10'."""
    original = value.strip()
    if not original:
        raise RRuleError("empty rule")
    parts: Dict[str, str] = {}
    for piece in original.split(";"):
        if "=" not in piece:
            raise RRuleError(f"bad rule part {piece!r} (want NAME=VALUE)")
        name, _, val = piece.partition("=")
        name = name.strip().upper()
        val = val.strip()
        if name in parts:
            raise RRuleError(f"rule part {name} occurs more than once")
        parts[name] = val
    if "FREQ" not in parts:
        raise RRuleError("the FREQ rule part is REQUIRED (RFC 5545 3.3.10)")
    freq = parts.pop("FREQ").upper()
    if freq not in FREQS:
        raise RRuleError(f"bad FREQ {freq!r}")

    def ints(name, lo, hi, signed=False):
        raw = parts.pop(name, None)
        if raw is None:
            return ()
        return _parse_int_list(name, raw, lo, hi, signed)

    rr = RRule(
        freq=freq,
        interval=int(parts.pop("INTERVAL", "1"))
        if parts.get("INTERVAL", "1").isdigit() else -1,
        count=int(c) if (c := parts.pop("COUNT", None)) and c.isdigit() else
        (None if c is None else -1),
        until=parse_until(u) if (u := parts.pop("UNTIL", None)) else None,
        bymonth=ints("BYMONTH", 1, 12),
        byweekno=ints("BYWEEKNO", 1, 53, signed=True),
        byyearday=ints("BYYEARDAY", 1, 366, signed=True),
        bymonthday=ints("BYMONTHDAY", 1, 31, signed=True),
        byday=_parse_byday(parts.pop("BYDAY")) if "BYDAY" in parts else (),
        byhour=ints("BYHOUR", 0, 23),
        byminute=ints("BYMINUTE", 0, 59),
        bysetpos=ints("BYSETPOS", 1, 366, signed=True),
        wkst=WDAY[parts.pop("WKST", "MO").upper()]
        if parts.get("WKST", "MO").upper() in WDAY else -1,
        original=original,
    )
    if rr.wkst == -1:
        raise RRuleError(f"bad WKST {parts.get('WKST')!r}")
    if rr.interval == -1:
        raise RRuleError(f"bad INTERVAL {parts.get('INTERVAL')!r}")
    if rr.count == -1:
        raise RRuleError(f"bad COUNT {parts.get('COUNT')!r}")
    if parts:
        raise RRuleError(f"unknown rule part(s): {', '.join(sorted(parts))}")
    rr.validate()
    return rr


# ---------------------------------------------------------------------------
# Week arithmetic (RFC 5545: ISO-8601-style weeks, week 1 = first week with
# at least 4 days in the year, weeks start on WKST).
# ---------------------------------------------------------------------------

def _week1_start(year: int, wkst: int) -> date:
    """Start date of week 1 of `year` (first week with >= 4 in-year days)."""
    jan1 = date(year, 1, 1)
    back = (jan1.weekday() - wkst) % 7
    start = jan1 - timedelta(days=back)
    in_year = 7 - back  # days of that week inside the year
    if in_year < 4:
        start += timedelta(days=7)
    return start


def _month_last_day(y: int, m: int) -> int:
    return calendar.monthrange(y, m)[1]


def _monthday_dates(y: int, m: int, monthdays: Tuple[int, ...]) -> List[date]:
    last = _month_last_day(y, m)
    out = []
    for d in monthdays:
        day = d if d > 0 else last + d + 1
        if 1 <= day <= last:  # invalid dates (Feb 30) are ignored (RFC)
            out.append(date(y, m, day))
    return out


def _byday_in_month(y: int, m: int,
                    byday: Tuple[Tuple[Optional[int], int], ...]) -> List[date]:
    """MONTHLY special expand: all matching days, or the nth/-nth of a kind."""
    last = _month_last_day(y, m)
    out = []
    for n, wd in byday:
        if n is None:
            d = 1
            while d <= last:
                if date(y, m, d).weekday() == wd:
                    out.append(date(y, m, d))
                d += 1
        else:
            days = [d for d in range(1, last + 1)
                    if date(y, m, d).weekday() == wd]
            idx = n - 1 if n > 0 else n  # -1 -> last
            if -len(days) <= idx < len(days):
                out.append(date(y, m, days[idx]))
    return out


def _byday_in_year(y: int, byday) -> List[date]:
    """YEARLY special expand with numeric BYDAY: nth weekday of the year."""
    out = []
    for n, wd in byday:
        days = []
        d = date(y, 1, 1)
        while d.year == y:
            if d.weekday() == wd:
                days.append(d)
            d += timedelta(days=1)
        if n is None:
            out.extend(days)
        else:
            idx = n - 1 if n > 0 else n
            if -len(days) <= idx < len(days):
                out.append(days[idx])
    return out


def _yearday_dates(y: int, yeardays: Tuple[int, ...]) -> List[date]:
    days_in_year = 366 if calendar.isleap(y) else 365
    out = []
    for n in yeardays:
        d = n if n > 0 else days_in_year + n + 1
        if 1 <= d <= days_in_year:
            out.append(date(y, 1, 1) + timedelta(days=d - 1))
    return out


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------

def _period_dates(rr: RRule, period_start: date, dtstart: date) -> List[date]:
    """Candidate dates for one interval period, per the expand/limit table."""
    if rr.freq == "DAILY":
        dates = [period_start]
        if rr.bymonth and period_start.month not in rr.bymonth:
            return []
        if rr.bymonthday:  # Limit
            last = _month_last_day(period_start.year, period_start.month)
            ok = set()
            for md in rr.bymonthday:
                ok.add(md if md > 0 else last + md + 1)
            if period_start.day not in ok:
                return []
        if rr.byday:  # Limit
            if period_start.weekday() not in {wd for _, wd in rr.byday}:
                return []
        return dates

    if rr.freq == "WEEKLY":
        wk_start = period_start  # start of the week (WKST-based)
        days = [wk_start + timedelta(days=i) for i in range(7)]
        if rr.byday:  # Expand
            want = {wd for _, wd in rr.byday}
            dates = [d for d in days if d.weekday() in want]
        else:
            dates = [wk_start + timedelta(days=(dtstart.weekday() - rr.wkst) % 7)]
        if rr.bymonth:  # Limit
            dates = [d for d in dates if d.month in rr.bymonth]
        return dates

    if rr.freq == "MONTHLY":
        y, m = period_start.year, period_start.month
        if rr.bymonth and m not in rr.bymonth:  # Limit
            return []
        if rr.bymonthday:  # Expand
            dates = _monthday_dates(y, m, rr.bymonthday)
            if rr.byday:  # Note 1: Limit when BYMONTHDAY present
                want = {wd for _, wd in rr.byday}
                dates = [d for d in dates if d.weekday() in want]
        elif rr.byday:  # Note 1: special expand
            dates = _byday_in_month(y, m, rr.byday)
        else:
            day = dtstart.day
            if day <= _month_last_day(y, m):
                dates = [date(y, m, day)]
            else:
                dates = []  # invalid date -> ignored
        return dates

    # YEARLY
    y = period_start.year
    if rr.byweekno:  # Expand
        w1 = _week1_start(y, rr.wkst)
        w1_next = _week1_start(y + 1, rr.wkst)
        n_weeks = (w1_next - w1).days // 7
        week_starts = []
        for wn in rr.byweekno:
            k = wn - 1 if wn > 0 else n_weeks + wn
            if 0 <= k < n_weeks:
                week_starts.append(w1 + timedelta(weeks=k))
        if rr.byday:
            want = {wd for _, wd in rr.byday}  # special expand for WEEKLY
            dates = [ws + timedelta(days=(wd - rr.wkst) % 7)
                     for ws in week_starts for wd in sorted(want)]
        else:
            dates = [ws + timedelta(days=(dtstart.weekday() - rr.wkst) % 7)
                     for ws in week_starts]
        if rr.bymonth:
            dates = [d for d in dates if d.month in rr.bymonth]
        if rr.bymonthday:
            dates = [d for d in dates
                     if d in set(_monthday_dates(d.year, d.month, rr.bymonthday))]
        return sorted(dates)

    if rr.byyearday:  # Expand
        dates = _yearday_dates(y, rr.byyearday)
        if rr.bymonth:
            dates = [d for d in dates if d.month in rr.bymonth]
        if rr.bymonthday:
            dates = [d for d in dates
                     if d in set(_monthday_dates(d.year, d.month, rr.bymonthday))]
        if rr.byday:  # Limit (BYYEARDAY present)
            want = {wd for _, wd in rr.byday}
            dates = [d for d in dates if d.weekday() in want]
        return sorted(dates)

    months = sorted(rr.bymonth) if rr.bymonth else list(range(1, 13))
    if not (rr.bymonth or rr.bymonthday or rr.byday):
        # no BYxxx day parts at all: the anniversary of DTSTART, per the
        # RFC's "missing parts are retrieved from DTSTART" rule
        months = [dtstart.month]
    dates = []
    for m in months:  # BYMONTH is Expand for YEARLY
        if rr.bymonthday:  # Expand
            md = _monthday_dates(y, m, rr.bymonthday)
            if rr.byday:  # Note 2: Limit
                want = {wd for _, wd in rr.byday}
                md = [d for d in md if d.weekday() in want]
            dates.extend(md)
        elif rr.byday:
            if any(n is not None for n, _ in rr.byday):
                if rr.bymonth:  # numeric offset within the month (RFC)
                    dates.extend(_byday_in_month(y, m, rr.byday))
                else:  # offset within the year (e.g. 20MO)
                    dates.extend(d for d in _byday_in_year(y, rr.byday)
                                 if d.month == m)
            else:  # special expand for MONTHLY (BYMONTH present)
                dates.extend(_byday_in_month(y, m, rr.byday))
        else:
            day = dtstart.day
            if day <= _month_last_day(y, m):
                dates.append(date(y, m, day))
    return sorted(set(dates))


def _period_starts(rr: RRule, dtstart: date):
    """Yield successive period anchor dates from DTSTART."""
    if rr.freq == "DAILY":
        k = 0
        while True:
            yield dtstart + timedelta(days=k * rr.interval)
            k += 1
    elif rr.freq == "WEEKLY":
        wk_start = dtstart - timedelta(days=(dtstart.weekday() - rr.wkst) % 7)
        k = 0
        while True:
            yield wk_start + timedelta(weeks=k * rr.interval)
            k += 1
    elif rr.freq == "MONTHLY":
        k = 0
        while True:
            total = (dtstart.year * 12 + dtstart.month - 1) + k * rr.interval
            yield date(total // 12, total % 12 + 1, 1)
            k += 1
    else:  # YEARLY
        k = 0
        while True:
            yield date(dtstart.year + k * rr.interval, 1, 1)
            k += 1


def _times_for(rr: RRule, dtstart_time: time) -> List[time]:
    hours = rr.byhour or (dtstart_time.hour,)
    minutes = rr.byminute or (dtstart_time.minute,)
    return sorted({time(h, m) for h in hours for m in minutes})


def occurrences(rr: RRule, dtstart: datetime, limit: int) -> List[datetime]:
    """The first `limit` instances of the recurrence set (RFC: DTSTART
    counts as the first occurrence; invalid dates are ignored)."""
    out: List[datetime] = []
    want = min(limit, rr.count) if rr.count is not None else limit
    times = _times_for(rr, dtstart.time())
    horizon = date(dtstart.year + MAX_YEARS, 12, 31)
    for period in _period_starts(rr, dtstart.date()):
        if period > horizon:
            break
        dates = _period_dates(rr, period, dtstart.date())
        inst = sorted(datetime.combine(d, t) for d in dates for t in times)
        if rr.bysetpos:  # Limit, per interval period
            picked = []
            for p in rr.bysetpos:
                idx = p - 1 if p > 0 else p
                if -len(inst) <= idx < len(inst):
                    picked.append(inst[idx])
            inst = sorted(set(picked))
        for it in inst:
            if it < dtstart:
                continue
            if rr.until is not None and it > rr.until:
                return out
            out.append(it)
            if len(out) >= want:
                return out
    return out


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------

def _fmt_days(byday) -> str:
    def one(n, wd):
        name = WDAY_NAME[wd]
        if n is None:
            return name
        if n > 0:
            return f"the {_ord(n)} {name}"
        return f"the {_ord(n)} {name}"
    return " and ".join(one(n, wd) for n, wd in byday)


def _ord(n: int) -> str:
    """Ordinal for BYDAY offsets: 1 -> 1st, -1 -> last, -2 -> 2nd-to-last."""
    if n > 0:
        suf = "th" if 10 <= n % 100 <= 20 else \
            {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        return f"{n}{suf}"
    if n == -1:
        return "last"
    return f"{_ordinal_word(abs(n))}-to-last"


def _ordinal_word(n: int) -> str:
    return {1: "first", 2: "second", 3: "third", 4: "fourth",
            5: "fifth"}.get(n, f"{n}th")


def explain(rr: RRule, dtstart: Optional[datetime] = None) -> str:
    every = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month",
             "YEARLY": "year"}[rr.freq]
    text = ("Every " + every) if rr.interval == 1 else \
        f"Every {rr.interval} {every}s"
    bits = []
    if rr.byday:
        bits.append("on " + _fmt_days(rr.byday))
    if rr.bymonthday:
        bits.append("on month-day " + ", ".join(
            str(v) if v > 0 else f"{v} (from the end)"
            for v in rr.bymonthday))
    if rr.byyearday:
        bits.append("on year-day " + ", ".join(map(str, rr.byyearday)))
    if rr.byweekno:
        bits.append("in week " + ", ".join(map(str, rr.byweekno)) +
                    f" (weeks start {WDAY_NAME[rr.wkst]})")
    if rr.bymonth:
        bits.append("in " + ", ".join(MONTH_NAME[m] for m in rr.bymonth))
    if rr.bysetpos:
        bits.append("taking only position(s) " +
                    ", ".join(map(str, rr.bysetpos)) + " of each set")
    if bits:
        text += ", " + "; ".join(bits)
    if rr.count is not None:
        text += f"; {rr.count} occurrence(s) total (DTSTART counts as #1)"
    elif rr.until is not None:
        text += f"; until {rr.until.date().isoformat()} inclusive"
    else:
        text += "; forever"
    if dtstart is not None:
        text += f". First occurrence: {dtstart.date().isoformat()} (DTSTART)"
    return text


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Finding:
    kind: str
    severity: str
    message: str


def audit(rr: RRule, dtstart: Optional[datetime] = None) -> List[Finding]:
    out: List[Finding] = []
    if rr.count is None and rr.until is None:
        out.append(Finding("unbounded", "info",
                           "no COUNT or UNTIL: the rule repeats forever"))
    if rr.until is not None and dtstart is not None and \
            rr.until < dtstart:
        out.append(Finding("never-fires", "error",
                           f"UNTIL ({rr.until.date()}) is before DTSTART "
                           f"({dtstart.date()}): no instance can exist"))
        return out

    # Calendar reachability of the day part.
    day_part = None
    if rr.freq == "MONTHLY" or (rr.freq == "YEARLY" and not rr.byweekno
                                and not rr.byyearday):
        months = set(rr.bymonth) if rr.bymonth else set(range(1, 13))
        if rr.bymonthday:
            viable = [(m, d) for m in sorted(months) for d in rr.bymonthday
                      if 1 <= (d if d > 0 else calendar.monthrange(2024, m)[1] + d + 1)
                      <= (calendar.monthrange(2024, m)[1] if d < 0 else 31)
                      and (d > 0 and d <= (31 if m in (1, 3, 5, 7, 8, 10, 12)
                                           else 30 if m != 2 else 29)
                           or d < 0)]
            viable = [(m, d) for m in sorted(months) for d in rr.bymonthday
                      if (d > 0 and d <= {1:31,2:29,3:31,4:30,5:31,6:30,7:31,
                                          8:31,9:30,10:31,11:30,12:31}[m])
                      or (d < 0 and abs(d) <= {1:31,2:29,3:31,4:30,5:31,6:30,
                                               7:31,8:31,9:30,10:31,11:30,
                                               12:31}[m])]
            if not viable:
                out.append(Finding(
                    "never-fires", "error",
                    "no month can provide the requested day-of-month "
                    "(e.g. the 30th/31st in February): the rule is empty"))
            elif all(m == 2 and (d == 29 or d == -1) for m, d in viable) and \
                    2 in months:
                out.append(Finding(
                    "leap-only", "info",
                    "the day part can only be satisfied by February 29: this "
                    "fires only in leap years (next: 2028)"))
            elif 2 in months and any(d >= 29 for d in rr.bymonthday if d > 0):
                out.append(Finding(
                    "skips-february", "info",
                    "February has no 30th/31st: invalid dates are silently "
                    "ignored (RFC 5545 3.3.10), so February may contribute "
                    "fewer instances than other months"))
            day_part = "monthday"
    if rr.freq == "YEARLY" and rr.byyearday:
        if all(abs(n) == 366 or n == 366 for n in rr.byyearday) and \
                all(n > 0 for n in rr.byyearday):
            out.append(Finding(
                "leap-only", "info",
                "year-day 366 exists only in leap years: this fires only in "
                "leap years (next: 2028)"))
        day_part = "yearday"
    if rr.freq == "YEARLY" and rr.byweekno:
        if all(n == 53 for n in rr.byweekno):
            out.append(Finding(
                "week-53-only", "warning",
                "week 53 occurs only when Jan 1 is a Thursday, or a "
                "Wednesday in a leap year (with WKST=MO): most years have "
                "no week 53, so this fires rarely (2015, 2020, 2026, ...)"))
        day_part = "weekno"

    if rr.bysetpos and day_part in (None, "monthday") and \
            rr.freq in ("WEEKLY",) and rr.byday:
        per_week = len({wd for _, wd in rr.byday})
        if all(abs(p) > per_week for p in rr.bysetpos):
            out.append(Finding(
                "never-fires", "error",
                f"BYSETPOS selects position(s) {list(rr.bysetpos)} but each "
                f"week only has {per_week} candidate(s): the rule is empty"))

    if dtstart is not None:
        fires = occurrences(rr, dtstart, 2)
        if not fires:
            out.append(Finding(
                "never-fires", "error",
                "no occurrence exists within a 100-year horizon"))
        elif fires[0] != dtstart:
            out.append(Finding(
                "dtstart-unsynchronized", "warning",
                f"DTSTART ({dtstart.date()}) does not itself satisfy the "
                f"rule (first generated instance is {fires[0].date()}); "
                "RFC 5545 3.8.5.3 says the recurrence set of an "
                "unsynchronized DTSTART is undefined - implementations "
                "disagree on whether DTSTART is still an instance"))
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_dt(text: str) -> datetime:
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%Y%m%d",
                "%Y%m%dT%H%M%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise SystemExit(f"error: bad datetime {text!r} "
                     "(want '2026-10-19' or '2026-10-19 09:30')")


def _fmt(dt: datetime) -> str:
    return dt.strftime("%a %Y-%m-%d %H:%M")


def cmd_next(a) -> int:
    rr = parse(a.rule)
    dtstart = _parse_dt(a.dtstart) if a.dtstart else datetime.now()
    fires = occurrences(rr, dtstart, a.count)
    if a.json:
        print(json.dumps([f.strftime("%Y-%m-%d %H:%M") for f in fires],
                         indent=2))
    else:
        for f in fires:
            print(_fmt(f))
    if not fires:
        print("no occurrences (rule is empty or already exhausted)",
              file=sys.stderr)
        return 1
    return 0


def cmd_explain(a) -> int:
    rr = parse(a.rule)
    dtstart = _parse_dt(a.dtstart) if a.dtstart else None
    if a.json:
        print(json.dumps({"rule": rr.original,
                          "description": explain(rr, dtstart)}, indent=2))
    else:
        print(explain(rr, dtstart))
    return 0


def cmd_audit(a) -> int:
    rr = parse(a.rule)
    dtstart = _parse_dt(a.dtstart) if a.dtstart else None
    findings = audit(rr, dtstart)
    rows = [{"kind": f.kind, "severity": f.severity, "message": f.message}
            for f in findings]
    if a.json:
        print(json.dumps(rows, indent=2))
    else:
        for r in rows:
            print(f"[{r['kind']}] {r['message']}")
        if not rows:
            print("no findings")
    return 1 if any(r["severity"] != "info" for r in rows) else 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="rrule-check",
        description="Parse, explain, and audit iCalendar recurrence rules "
                    "(RFC 5545 RRULE).")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")
    p.add_argument("--json", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)
    for name, help_text, func in (
            ("next", "print the next occurrences", cmd_next),
            ("explain", "describe the rule in words", cmd_explain),
            ("audit", "flag rules that won't do what you mean", cmd_audit)):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("rule")
        sp.add_argument("--dtstart",
                        help="start datetime, e.g. '2026-10-19 09:00'")
        if name == "next":
            sp.add_argument("-n", "--count", type=int, default=5)
        sp.set_defaults(func=func)
    a = p.parse_args(argv)
    try:
        return a.func(a)
    except RRuleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
