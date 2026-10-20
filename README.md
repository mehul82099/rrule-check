# rrule-check

Parse, explain, and audit iCalendar recurrence rules (RFC 5545 `RRULE`) — the format underneath every "repeat this event" in Google Calendar, Outlook, Apple Calendar, and every CalDAV server.

Recurrence rules look simple and are not. These all parse fine, and all of them are wrong in ways that only show up months later:

```console
$ rrule-check explain 'FREQ=MONTHLY;BYDAY=-2MO'
Every month, on the second-to-last Monday; forever
$ rrule-check audit 'FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30'
[never-fires] no month can provide the requested day-of-month (e.g. the 30th/31st in February): the rule is empty
$ rrule-check audit 'FREQ=YEARLY;BYWEEKNO=53;BYDAY=MO'
[week-53-only] week 53 occurs only when Jan 1 is a Thursday, or a Wednesday in a leap year (with WKST=MO): most years have no week 53, so this fires rarely (2015, 2020, 2026, ...)
```

## The traps it knows about

- **The expand/limit table.** Whether a `BYxxx` part *multiplies* or *filters* the candidate set depends on the `FREQ` (RFC 5545 §3.3.10): `BYMONTH` limits a DAILY rule but *expands* a YEARLY one; `BYMONTHDAY` is flat-out invalid with `FREQ=WEEKLY`. The tool implements the table and rejects every combination the RFC marks MUST NOT.
- **Numeric BYDAY means different things in different rules.** In a MONTHLY rule `1MO` is the first Monday of the month. In a YEARLY rule it is an offset within the *month* if `BYMONTH` is present, but an offset within the *year* otherwise (`20MO` = the 20th Monday of the year) — and it is invalid with `BYWEEKNO`.
- **Week numbers depend on the week start.** `BYWEEKNO` uses ISO-8601-style numbering where week 1 is the first week with at least four days in the year — and weeks start on `WKST` (default Monday). `WKST=SU` moves every boundary; week 53 exists only in some years.
- **Invalid dates vanish silently.** `BYMONTHDAY=30` skips February without any error (RFC: such instances "MUST be ignored and MUST NOT be counted"), so a monthly rule fires 11 times a year, not 12.
- **`COUNT` counts DTSTART.** The start date is occurrence #1, and a `DTSTART` that doesn't itself match the rule makes the recurrence set *undefined* — the audit warns about exactly that.
- **`BYSETPOS` picks from one interval's set.** `BYSETPOS=-1` is "the last instance *within each interval*", not "the last instance ever".

## What it does

- **`next 'RULE' --dtstart '...' [-n N]`** — concrete occurrences.
- **`explain 'RULE' [--dtstart '...']`** — the rule in plain words.
- **`audit 'RULE' [--dtstart '...']`** — findings: `never-fires`, `leap-only` (Feb 29 / year-day 366 rules), `week-53-only`, `skips-february`, `unbounded` (repeats forever), `dtstart-unsynchronized`.
- Supports `FREQ=DAILY/WEEKLY/MONTHLY/YEARLY` with `INTERVAL`, `COUNT`, `UNTIL`, `BYMONTH`, `BYWEEKNO`, `BYYEARDAY`, `BYMONTHDAY`, `BYDAY` (with numeric offsets), `BYHOUR`, `BYMINUTE`, `BYSETPOS`, and `WKST`. Python 3.9+, standard library only, no network. `--json` everywhere.

## How it is tested

65 tests in three layers:

1. **Every in-scope worked example from [RFC 5545 §3.8.5.3](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.8.5.3), pinned to the RFC's own printed date lists** — including the presidential-election rule (`BYMONTH=11;BYDAY=TU;BYMONTHDAY=2-8` every 4 years), the `WKST=MO` vs `WKST=SU` pair that produces different dates, `BYSETPOS=3` and `BYSETPOS=-2`, leap-aware `BYYEARDAY` (day 100 is April 10 in 1997 but April 9 in 2000), and the February-30-is-ignored rule.
2. **Semantic pins of [§3.3.10](https://www.rfc-editor.org/rfc/rfc5545.html#section-3.3.10)**: the expand/limit table, every MUST-NOT rejection, hand-computed week-1 starts and week-53 years, `UNTIL` inclusivity, DTSTART-as-first-occurrence.
3. **A randomized differential test.** 400 seeded random rules (seed 20261019) are expanded by the period-navigation engine and by an independent day-by-day predicate oracle — different code, different direction (the oracle decides "does this date match?" per date; the engine constructs candidate periods) — and must agree on every instance inside a 30-year window. `BYSETPOS` is exercised by the RFC vectors rather than the oracle. One shared primitive (the week-1-start calculation) is used by both; it is pinned separately with hand-computed values.

## Scope and limitations

- `SECONDLY`, `MINUTELY`, and `HOURLY` frequencies are rejected with a clear error: sub-daily scheduling is a time-of-day problem, and this tool's value is the calendar state machine. Everything else in §3.3.10 is implemented.
- Times are naive local wall-clock times, like a `DTSTART` with no time zone. The RFC's UTC/`TZID` rules for `UNTIL` are noted in parser errors but time zones themselves are out of scope. DST transitions are not modeled.
- `RDATE`/`EXDATE` sets and multiple `RRULE` properties are out of scope (the RFC leaves multiple RRULEs undefined).
- The reference implementation people compare against is [`dateutil.rrule`](https://dateutil.readthedocs.io/en/stable/rrule.html); this tool is dependency-free instead, and its audit layer (never-fires, week-53-only, unsynchronized DTSTART) is the part dateutil doesn't do for you.

## License

MIT — see [LICENSE](LICENSE).
