"""Tests for rrule_check.

Layers:
- every worked example from RFC 5545 section 3.8.5.3 that is inside this
  tool's scope (DAILY/WEEKLY/MONTHLY/YEARLY), pinned to the RFC's own
  printed date lists;
- pins of section 3.3.10 semantics: the expand/limit table, numeric-BYDAY
  offsets, ISO-style week numbering with WKST, invalid dates ignored,
  DTSTART counting as the first occurrence, and the MUST NOT rejections;
- a randomized differential test: the period-expansion engine vs an
  independent day-by-day predicate oracle on hundreds of seeded random
  rules (BYSETPOS excluded there - it is exercised by the RFC vectors);
- audit, explain, and CLI tests.
"""

import calendar
import json
import os
import random
import subprocess
import sys
import unittest
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import rrule_check as rc

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "rrule_check.py")
DTS = datetime(1997, 9, 2, 9, 0)  # the RFC examples' DTSTART


def occ(rule, dtstart=DTS, n=10):
    return [f.strftime("%Y-%m-%d") for f in
            rc.occurrences(rc.parse(rule), dtstart, n)]


class TestParsing(unittest.TestCase):
    def test_freq_required(self):
        with self.assertRaises(rc.RRuleError):
            rc.parse("BYDAY=MO")

    def test_duplicate_part_rejected(self):
        with self.assertRaises(rc.RRuleError):
            rc.parse("FREQ=DAILY;COUNT=2;COUNT=3")

    def test_unknown_part_rejected(self):
        with self.assertRaises(rc.RRuleError):
            rc.parse("FREQ=DAILY;FOO=1")

    def test_case_insensitive_parts(self):
        rr = rc.parse("freq=weekly;byday=mo,we")
        self.assertEqual(rr.freq, "WEEKLY")
        self.assertEqual({wd for _, wd in rr.byday}, {0, 2})

    def test_rfc_must_nots(self):
        for bad, frag in (
                ("FREQ=DAILY;COUNT=3;UNTIL=20270101", "MUST NOT occur"),
                ("FREQ=WEEKLY;BYMONTHDAY=15", "MUST NOT"),
                ("FREQ=DAILY;BYDAY=1MO", "MUST NOT"),
                ("FREQ=YEARLY;BYWEEKNO=20;BYDAY=1MO", "MUST NOT"),
                ("FREQ=MONTHLY;BYYEARDAY=100", "MUST NOT"),
                ("FREQ=DAILY;BYWEEKNO=20", "MUST NOT"),
                ("FREQ=DAILY;BYSETPOS=1", "MUST only be used"),
                ("FREQ=HOURLY;INTERVAL=3", "outside this tool's scope"),
                ("FREQ=DAILY;INTERVAL=0", "positive"),
                ("FREQ=DAILY;COUNT=0", "positive"),
                ("FREQ=DAILY;BYMONTH=13", "out of range"),
                ("FREQ=DAILY;BYDAY=MO;WKST=XX", "bad WKST"),
                ("FREQ=DAILY;UNTIL=not-a-date", "bad date"),
                ("", "empty"),
        ):
            with self.assertRaises(rc.RRuleError, msg=bad) as ctx:
                rc.parse(bad)
            self.assertIn(frag, str(ctx.exception), bad)

    def test_signed_values(self):
        rr = rc.parse("FREQ=MONTHLY;BYMONTHDAY=1,-1")
        self.assertEqual(rr.bymonthday, (1, -1))
        with self.assertRaises(rc.RRuleError):
            rc.parse("FREQ=MONTHLY;BYMONTHDAY=0")
        with self.assertRaises(rc.RRuleError):
            rc.parse("FREQ=YEARLY;BYWEEKNO=54")
        rr = rc.parse("FREQ=MONTHLY;BYDAY=+2TU")
        self.assertEqual(rr.byday, ((2, 1),))


class TestRFCExamples(unittest.TestCase):
    """RFC 5545 3.8.5.3, verbatim expectations (dates from the RFC text)."""

    def test_daily_for_10(self):
        got = occ("FREQ=DAILY;COUNT=10")
        self.assertEqual(got[0], "1997-09-02")
        self.assertEqual(got[-1], "1997-09-11")
        self.assertEqual(len(got), 10)

    def test_every_other_day(self):
        self.assertEqual(occ("FREQ=DAILY;INTERVAL=2", n=5),
                         ["1997-09-02", "1997-09-04", "1997-09-06",
                          "1997-09-08", "1997-09-10"])

    def test_every_10_days_5_occurrences(self):
        self.assertEqual(occ("FREQ=DAILY;INTERVAL=10;COUNT=5"),
                         ["1997-09-02", "1997-09-12", "1997-09-22",
                          "1997-10-02", "1997-10-12"])

    def test_daily_until_dec_24(self):
        got = occ("FREQ=DAILY;UNTIL=19971224T000000Z", n=200)
        self.assertEqual(got[0], "1997-09-02")
        self.assertEqual(got[-1], "1997-12-23")
        self.assertEqual(len(got), 113)

    def test_every_day_in_january_3_years(self):
        got = occ("FREQ=YEARLY;UNTIL=20000131T140000Z;BYMONTH=1;"
                  "BYDAY=SU,MO,TU,WE,TH,FR,SA", datetime(1998, 1, 1, 9, 0), 200)
        self.assertEqual(len(got), 93)
        self.assertEqual(got[0], "1998-01-01")
        self.assertEqual(got[-1], "2000-01-31")
        # the RFC's alternative formulation must agree exactly
        alt = occ("FREQ=DAILY;UNTIL=20000131T140000Z;BYMONTH=1",
                  datetime(1998, 1, 1, 9, 0), 200)
        self.assertEqual(got, alt)

    def test_weekly_for_10(self):
        got = occ("FREQ=WEEKLY;COUNT=10")
        self.assertEqual(got, ["1997-09-02", "1997-09-09", "1997-09-16",
                               "1997-09-23", "1997-09-30", "1997-10-07",
                               "1997-10-14", "1997-10-21", "1997-10-28",
                               "1997-11-04"])

    def test_every_other_week_wkst_su(self):
        self.assertEqual(occ("FREQ=WEEKLY;INTERVAL=2;WKST=SU", n=6),
                         ["1997-09-02", "1997-09-16", "1997-09-30",
                          "1997-10-14", "1997-10-28", "1997-11-11"])

    def test_tu_th_for_five_weeks(self):
        got = occ("FREQ=WEEKLY;COUNT=10;WKST=SU;BYDAY=TU,TH")
        self.assertEqual(got, ["1997-09-02", "1997-09-04", "1997-09-09",
                               "1997-09-11", "1997-09-16", "1997-09-18",
                               "1997-09-23", "1997-09-25", "1997-09-30",
                               "1997-10-02"])

    def test_every_other_week_mwf(self):
        got = occ("FREQ=WEEKLY;INTERVAL=2;UNTIL=19971224T000000Z;WKST=SU;"
                  "BYDAY=MO,WE,FR", datetime(1997, 9, 1, 9, 0), 30)
        self.assertEqual(got[:7], ["1997-09-01", "1997-09-03", "1997-09-05",
                                   "1997-09-15", "1997-09-17", "1997-09-19",
                                   "1997-09-29"])
        self.assertEqual(got[-1], "1997-12-22")

    def test_every_other_week_tu_th_8(self):
        self.assertEqual(
            occ("FREQ=WEEKLY;INTERVAL=2;COUNT=8;WKST=SU;BYDAY=TU,TH"),
            ["1997-09-02", "1997-09-04", "1997-09-16", "1997-09-18",
             "1997-09-30", "1997-10-02", "1997-10-14", "1997-10-16"])

    def test_monthly_first_friday_10(self):
        got = occ("FREQ=MONTHLY;COUNT=10;BYDAY=1FR", datetime(1997, 9, 5, 9, 0))
        self.assertEqual(got, ["1997-09-05", "1997-10-03", "1997-11-07",
                               "1997-12-05", "1998-01-02", "1998-02-06",
                               "1998-03-06", "1998-04-03", "1998-05-01",
                               "1998-06-05"])

    def test_first_and_last_sunday_every_other_month(self):
        got = occ("FREQ=MONTHLY;INTERVAL=2;COUNT=10;BYDAY=1SU,-1SU",
                  datetime(1997, 9, 7, 9, 0))
        self.assertEqual(got, ["1997-09-07", "1997-09-28", "1997-11-02",
                               "1997-11-30", "1998-01-04", "1998-01-25",
                               "1998-03-01", "1998-03-29", "1998-05-03",
                               "1998-05-31"])

    def test_second_to_last_monday(self):
        got = occ("FREQ=MONTHLY;COUNT=6;BYDAY=-2MO",
                  datetime(1997, 9, 22, 9, 0))
        self.assertEqual(got, ["1997-09-22", "1997-10-20", "1997-11-17",
                               "1997-12-22", "1998-01-19", "1998-02-16"])

    def test_third_to_last_day_of_month(self):
        got = occ("FREQ=MONTHLY;BYMONTHDAY=-3", datetime(1997, 9, 28, 9, 0), 6)
        self.assertEqual(got, ["1997-09-28", "1997-10-29", "1997-11-28",
                               "1997-12-29", "1998-01-29", "1998-02-26"])

    def test_2nd_and_15th(self):
        got = occ("FREQ=MONTHLY;COUNT=10;BYMONTHDAY=2,15")
        self.assertEqual(got, ["1997-09-02", "1997-09-15", "1997-10-02",
                               "1997-10-15", "1997-11-02", "1997-11-15",
                               "1997-12-02", "1997-12-15", "1998-01-02",
                               "1998-01-15"])

    def test_first_and_last_day_of_month(self):
        got = occ("FREQ=MONTHLY;COUNT=10;BYMONTHDAY=1,-1",
                  datetime(1997, 9, 30, 9, 0))
        self.assertEqual(got, ["1997-09-30", "1997-10-01", "1997-10-31",
                               "1997-11-01", "1997-11-30", "1997-12-01",
                               "1997-12-31", "1998-01-01", "1998-01-31",
                               "1998-02-01"])

    def test_every_18_months_10th_thru_15th(self):
        got = occ("FREQ=MONTHLY;INTERVAL=18;COUNT=10;"
                  "BYMONTHDAY=10,11,12,13,14,15", datetime(1997, 9, 10, 9, 0))
        self.assertEqual(got, ["1997-09-10", "1997-09-11", "1997-09-12",
                               "1997-09-13", "1997-09-14", "1997-09-15",
                               "1999-03-10", "1999-03-11", "1999-03-12",
                               "1999-03-13"])

    def test_yearly_june_july_10(self):
        got = occ("FREQ=YEARLY;COUNT=10;BYMONTH=6,7", DTS)
        self.assertEqual(got[:2], ["1998-06-02", "1998-07-02"])
        self.assertEqual(got[-1], "2002-07-02")

    def test_every_third_year_yearday(self):
        got = occ("FREQ=YEARLY;INTERVAL=3;COUNT=10;BYYEARDAY=1,100,200",
                  datetime(1997, 1, 1, 9, 0))
        # 2000 is a leap year: day 100 is April 9, not April 10
        self.assertEqual(got[:6], ["1997-01-01", "1997-04-10", "1997-07-19",
                                   "2000-01-01", "2000-04-09", "2000-07-18"])

    def test_20th_monday_of_year(self):
        got = occ("FREQ=YEARLY;BYDAY=20MO", datetime(1997, 5, 19, 9, 0), 3)
        self.assertEqual(got, ["1997-05-19", "1998-05-18", "1999-05-17"])

    def test_monday_of_week_20(self):
        got = occ("FREQ=YEARLY;BYWEEKNO=20;BYDAY=MO",
                  datetime(1997, 5, 12, 9, 0), 3)
        self.assertEqual(got, ["1997-05-12", "1998-05-11", "1999-05-17"])

    def test_every_thursday_in_march(self):
        got = occ("FREQ=YEARLY;BYMONTH=3;BYDAY=TH",
                  datetime(1997, 3, 13, 9, 0), 6)
        self.assertEqual(got, ["1997-03-13", "1997-03-20", "1997-03-27",
                               "1998-03-05", "1998-03-12", "1998-03-19"])

    def test_thursday_june_july_august(self):
        got = occ("FREQ=YEARLY;BYDAY=TH;BYMONTH=6,7,8",
                  datetime(1997, 6, 5, 9, 0), 14)
        self.assertEqual(got[:5], ["1997-06-05", "1997-06-12", "1997-06-19",
                                   "1997-06-26", "1997-07-03"])
        self.assertEqual(len([g for g in got if g.startswith("1997")]), 13)

    def test_friday_the_13th(self):
        got = occ("FREQ=MONTHLY;BYDAY=FR;BYMONTHDAY=13", DTS, 3)
        self.assertEqual(got, ["1998-02-13", "1998-03-13", "1998-11-13"])

    def test_first_saturday_after_first_sunday(self):
        got = occ("FREQ=MONTHLY;BYDAY=SA;BYMONTHDAY=7,8,9,10,11,12,13",
                  datetime(1997, 9, 13, 9, 0), 6)
        self.assertEqual(got, ["1997-09-13", "1997-10-11", "1997-11-08",
                               "1997-12-13", "1998-01-10", "1998-02-07"])

    def test_us_presidential_election_day(self):
        got = occ("FREQ=YEARLY;INTERVAL=4;BYMONTH=11;BYDAY=TU;"
                  "BYMONTHDAY=2,3,4,5,6,7,8", datetime(1996, 11, 5, 9, 0), 3)
        self.assertEqual(got, ["1996-11-05", "2000-11-07", "2004-11-02"])

    def test_bysetpos_third_of_tu_we_th(self):
        got = occ("FREQ=MONTHLY;COUNT=3;BYDAY=TU,WE,TH;BYSETPOS=3",
                  datetime(1997, 9, 4, 9, 0))
        self.assertEqual(got, ["1997-09-04", "1997-10-07", "1997-11-06"])

    def test_bysetpos_second_to_last_weekday(self):
        got = occ("FREQ=MONTHLY;BYDAY=MO,TU,WE,TH,FR;BYSETPOS=-2",
                  datetime(1997, 9, 29, 9, 0), 7)
        self.assertEqual(got, ["1997-09-29", "1997-10-30", "1997-11-27",
                               "1997-12-30", "1998-01-29", "1998-02-26",
                               "1998-03-30"])

    def test_wkst_changes_the_result(self):
        dt = datetime(1997, 8, 5, 9, 0)
        mo = occ("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=MO", dt, 4)
        su = occ("FREQ=WEEKLY;INTERVAL=2;COUNT=4;BYDAY=TU,SU;WKST=SU", dt, 4)
        self.assertEqual(mo, ["1997-08-05", "1997-08-10",
                              "1997-08-19", "1997-08-24"])
        self.assertEqual(su, ["1997-08-05", "1997-08-17",
                              "1997-08-19", "1997-08-31"])

    def test_invalid_date_february_30_ignored(self):
        got = occ("FREQ=MONTHLY;BYMONTHDAY=15,30;COUNT=5",
                  datetime(2007, 1, 15, 9, 0))
        self.assertEqual(got, ["2007-01-15", "2007-01-30", "2007-02-15",
                               "2007-03-15", "2007-03-30"])

    def test_every_20_minutes_via_daily_byhour_byminute(self):
        got = rc.occurrences(
            rc.parse("FREQ=DAILY;BYHOUR=9,10,11,12,13,14,15,16;"
                     "BYMINUTE=0,20,40"), DTS, 25)
        self.assertEqual([f.strftime("%H:%M") for f in got[:4]],
                         ["09:00", "09:20", "09:40", "10:00"])
        self.assertEqual(got[23].strftime("%Y-%m-%d %H:%M"),
                         "1997-09-02 16:40")
        self.assertEqual(got[24].strftime("%Y-%m-%d %H:%M"),
                         "1997-09-03 09:00")


class TestWeekNumbering(unittest.TestCase):
    def test_week1_start_examples(self):
        # ISO rule, WKST=MO: 1997 Jan 1 is a Wednesday, its week has 5
        # in-year days -> week 1 starts Mon Dec 30, 1996.
        self.assertEqual(rc._week1_start(1997, 0), date(1996, 12, 30))
        # 1998 Jan 1 is a Thursday -> week 1 starts Mon Dec 29, 1997.
        self.assertEqual(rc._week1_start(1998, 0), date(1997, 12, 29))
        # 1999 Jan 1 is a Friday: its week has only 3 in-year days ->
        # week 1 starts Mon Jan 4.
        self.assertEqual(rc._week1_start(1999, 0), date(1999, 1, 4))

    def test_week_53_only_some_years(self):
        # WKST=MO: 2015, 2020 and 2026 have a week 53; 2014 does not.
        for y, has in ((2014, False), (2015, True), (2019, False),
                       (2020, True), (2026, True)):
            n = (rc._week1_start(y + 1, 0) - rc._week1_start(y, 0)).days // 7
            self.assertEqual(n, 53 if has else 52, y)

    def test_negative_week_numbers(self):
        got = occ("FREQ=YEARLY;BYWEEKNO=-1;BYDAY=SU",
                  datetime(1997, 1, 1, 9, 0), 3)
        # 1997 has 52 ISO weeks: last Sunday is Dec 28, 1997. 1998 has a
        # week 53 (Jan 1 1998 is a Thursday): its last ISO week runs
        # Dec 28 1998 - Jan 3 1999, so its last Sunday is Jan 3, 1999.
        self.assertEqual(got[:2], ["1997-12-28", "1999-01-03"])


class TestEdgeCases(unittest.TestCase):
    def test_dtstart_counts_as_first_occurrence(self):
        rr = rc.parse("FREQ=DAILY;COUNT=3")
        got = rc.occurrences(rr, DTS, 10)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0], DTS)

    def test_until_is_inclusive(self):
        got = occ("FREQ=DAILY;UNTIL=19970904T090000")
        self.assertEqual(got, ["1997-09-02", "1997-09-03", "1997-09-04"])

    def test_dtstart_not_matching_skips_to_next(self):
        # DTSTART is a Tuesday; rule wants Mondays -> first instance is the
        # following Monday (RFC: unsynchronized DTSTART is undefined; the
        # audit warns about exactly this).
        got = occ("FREQ=WEEKLY;BYDAY=MO", datetime(2026, 10, 20, 9, 0), 1)
        self.assertEqual(got, ["2026-10-26"])

    def test_leap_day_yearly(self):
        got = occ("FREQ=YEARLY", datetime(2024, 2, 29, 9, 0), 3)
        self.assertEqual(got, ["2024-02-29", "2028-02-29", "2032-02-29"])

    def test_31st_monthly_skips_short_months(self):
        got = occ("FREQ=MONTHLY", datetime(2026, 1, 31, 9, 0), 4)
        self.assertEqual(got, ["2026-01-31", "2026-03-31",
                               "2026-05-31", "2026-07-31"])

    def test_exhausted_rule_returns_fewer(self):
        got = rc.occurrences(rc.parse("FREQ=DAILY;COUNT=2"), DTS, 10)
        self.assertEqual(len(got), 2)

    def test_time_parts_from_dtstart(self):
        got = rc.occurrences(rc.parse("FREQ=DAILY;COUNT=2"),
                             datetime(2026, 1, 1, 8, 45), 2)
        self.assertEqual([f.strftime("%H:%M") for f in got], ["08:45", "08:45"])


class TestPropertyDifferential(unittest.TestCase):
    """Engine vs an independent day-by-day predicate oracle on seeded
    random rules. BYSETPOS is excluded here (covered by RFC vectors)."""

    # Compare every instance inside a fixed 30-year window: sparse rules
    # (BYWEEKNO with BYMONTH) can go years between occurrences.
    WINDOW_DAYS = 30 * 366

    # -- independent oracle ------------------------------------------------
    @staticmethod
    def _wkstart(d, wkst):
        return d - timedelta(days=(d.weekday() - wkst) % 7)

    @classmethod
    def _weekno(cls, d, wkst):
        y = d.year
        w1 = rc._week1_start(y, wkst)
        if d < w1:
            y -= 1
            w1 = rc._week1_start(y, wkst)
        wn = (d - w1).days // 7 + 1
        n_weeks = (rc._week1_start(y + 1, wkst) - w1).days // 7
        return wn, n_weeks

    @classmethod
    def _day_matches(cls, rr, ds, d):
        iv = rr.interval
        if rr.freq == "DAILY":
            if (d - ds).days % iv:
                return False
        elif rr.freq == "WEEKLY":
            if (cls._wkstart(d, rr.wkst) - cls._wkstart(ds, rr.wkst)).days \
                    // 7 % iv:
                return False
        elif rr.freq == "MONTHLY":
            if ((d.year * 12 + d.month) - (ds.year * 12 + ds.month)) % iv:
                return False
        else:
            if (d.year - ds.year) % iv:
                return False
        if rr.bymonth and d.month not in rr.bymonth:
            return False
        plain = {wd for n, wd in rr.byday if n is None}
        ordinal = [(n, wd) for n, wd in rr.byday if n is not None]

        def monthday_ok():
            last = calendar.monthrange(d.year, d.month)[1]
            return any(v == d.day if v > 0 else d.day == last + v + 1
                       for v in rr.bymonthday)

        def month_ordinal_ok():
            last = calendar.monthrange(d.year, d.month)[1]
            for n, wd in ordinal:
                if d.weekday() != wd:
                    continue
                if n > 0 and (d.day - 1) // 7 == n - 1:
                    return True
                if n < 0 and (last - d.day) // 7 == -n - 1:
                    return True
            return False

        def year_ordinal_ok():
            for n, wd in ordinal:
                if d.weekday() != wd:
                    continue
                before = sum(1 for k in range(d.timetuple().tm_yday)
                             if date(d.year, 1, 1) +
                             timedelta(days=k) .weekday() == wd)
                total = sum(1 for k in range(365 + calendar.isleap(d.year))
                            if date(d.year, 1, 1) +
                            timedelta(days=k).weekday() == wd)
                if n > 0 and before == n - 1:
                    return True
                if n < 0 and total - before - 1 == -n - 1:
                    return True
            return False

        if rr.freq == "DAILY":
            return True
        if rr.freq == "WEEKLY":
            if rr.byday:
                return d.weekday() in plain
            return d.weekday() == ds.weekday()
        if rr.freq == "MONTHLY":
            if rr.bymonthday:
                if not monthday_ok():
                    return False
                return not plain or d.weekday() in plain
            if rr.byday:
                ok = d.weekday() in plain if plain else False
                return ok or month_ordinal_ok()
            return d.day == ds.day
        # YEARLY
        if rr.byweekno:
            wn, nw = cls._weekno(d, rr.wkst)
            if not any((v == wn) if v > 0 else (wn == nw + v + 1)
                       for v in rr.byweekno):
                return False
            if plain:
                return d.weekday() in plain
            return d.weekday() == ds.weekday()
        if rr.byyearday:
            doy = d.timetuple().tm_yday
            days_in_year = 365 + calendar.isleap(d.year)
            if not any(v == doy if v > 0 else doy == days_in_year + v + 1
                       for v in rr.byyearday):
                return False
            if rr.bymonthday and not monthday_ok():
                return False
            return not plain or d.weekday() in plain
        if rr.bymonthday:
            if not monthday_ok():
                return False
            return not plain or d.weekday() in plain
        if rr.byday:
            if ordinal and not plain:
                return year_ordinal_ok() if not rr.bymonth else \
                    month_ordinal_ok()
            if ordinal:
                return d.weekday() in plain or (
                    month_ordinal_ok() if rr.bymonth else year_ordinal_ok())
            return d.weekday() in plain
        if rr.bymonth:
            return d.day == ds.day
        return d.month == ds.month and d.day == ds.day

    @classmethod
    def oracle(cls, rr, dtstart, limit):
        out = []
        ds = dtstart.date()
        d = ds
        end = ds + timedelta(days=cls.WINDOW_DAYS)
        while d < end and len(out) < limit:
            if cls._day_matches(rr, ds, d):
                for t in rc._times_for(rr, dtstart.time()):
                    it = datetime.combine(d, t)
                    if it >= dtstart and (rr.until is None or it <= rr.until):
                        out.append(it)
                        if rr.count is not None and len(out) >= rr.count:
                            return out
            d += timedelta(days=1)
        return out

    def test_engine_agrees_with_oracle(self):
        rng = random.Random(20261019)
        checked = 0
        for _ in range(400):
            rule = self.random_rule(rng)
            try:
                rr = rc.parse(rule)
            except rc.RRuleError:
                continue
            dtstart = datetime(2026, 1, 1, rng.randint(0, 23),
                               rng.choice((0, 15, 30, 45))) + \
                timedelta(days=rng.randint(0, 700))
            horizon = dtstart + timedelta(days=self.WINDOW_DAYS)
            fast = [f for f in rc.occurrences(rr, dtstart, 12 ** 6)
                    if f < horizon]
            slow = self.oracle(rr, dtstart, 12 ** 6)
            self.assertEqual(fast, slow, rule)
            checked += 1
        self.assertGreaterEqual(checked, 340)

    @staticmethod
    def random_rule(rng):
        freq = rng.choice(["DAILY", "WEEKLY", "MONTHLY", "YEARLY"])
        parts = [f"FREQ={freq}"]
        if rng.random() < 0.5:
            parts.append(f"INTERVAL={rng.randint(1, 4)}")
        if rng.random() < 0.4:
            parts.append(f"WKST={rng.choice(list(rc.WDAY))}")
        r = rng.random()
        if r < 0.3:
            parts.append(f"COUNT={rng.randint(1, 40)}")
        elif r < 0.5:
            parts.append(f"UNTIL={rng.randint(2027, 2031)}0101T000000Z")
        if rng.random() < 0.4:
            ms = rng.sample(range(1, 13), rng.randint(1, 2))
            parts.append("BYMONTH=" + ",".join(map(str, ms)))
        wdays = rng.sample(list(rc.WDAY), rng.randint(1, 3))
        if freq == "WEEKLY" and rng.random() < 0.7:
            parts.append("BYDAY=" + ",".join(wdays))
        elif freq == "MONTHLY":
            r = rng.random()
            if r < 0.4:
                parts.append("BYDAY=" + ",".join(
                    f"{rng.randint(-2, 4) or 1}{d}" for d in wdays[:1]))
            elif r < 0.7:
                md = sorted(rng.sample(range(1, 29), rng.randint(1, 3)))
                if rng.random() < 0.3:
                    md.append(-rng.randint(1, 3))
                parts.append("BYMONTHDAY=" + ",".join(map(str, md)))
            else:
                parts.append("BYDAY=" + ",".join(wdays))
        elif freq == "YEARLY":
            r = rng.random()
            if r < 0.25 and "BYMONTH=1" not in parts:
                wn = rng.randint(-2, 53) or 1
                parts.append(f"BYWEEKNO={wn}")
                parts.append("BYDAY=" + ",".join(wdays[:1]))
            elif r < 0.45:
                parts.append(f"BYYEARDAY={rng.randint(1, 365)}")
            elif r < 0.7:
                parts.append("BYMONTHDAY=" + str(rng.randint(1, 28)))
            elif r < 0.9:
                if "BYMONTH" in ";".join(parts):
                    parts.append("BYDAY=" +
                                 f"{rng.randint(1, 4)}{wdays[0]}")
                else:
                    parts.append("BYDAY=" + ",".join(wdays))
        return ";".join(parts)


class TestAudit(unittest.TestCase):
    def kinds(self, rule, dtstart=None):
        rr = rc.parse(rule)
        dt = rc._parse_dt(dtstart) if dtstart else None
        return [f.kind for f in rc.audit(rr, dt)]

    def test_never_fires(self):
        self.assertIn("never-fires", self.kinds("FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30"))
        self.assertIn("never-fires", self.kinds("FREQ=WEEKLY;BYDAY=MO;BYSETPOS=2"))
        self.assertIn("never-fires", self.kinds("FREQ=DAILY;UNTIL=20200101",
                                                "2026-01-01"))

    def test_leap_only(self):
        self.assertIn("leap-only", self.kinds("FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=29"))
        self.assertIn("leap-only", self.kinds("FREQ=YEARLY;BYYEARDAY=366"))
        self.assertNotIn("leap-only", self.kinds("FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=28,29"))

    def test_skips_february(self):
        self.assertIn("skips-february", self.kinds("FREQ=MONTHLY;BYMONTHDAY=30"))
        self.assertNotIn("skips-february", self.kinds("FREQ=MONTHLY;BYMONTHDAY=15"))

    def test_week_53_only(self):
        self.assertIn("week-53-only", self.kinds("FREQ=YEARLY;BYWEEKNO=53;BYDAY=MO"))
        self.assertNotIn("week-53-only", self.kinds("FREQ=YEARLY;BYWEEKNO=52;BYDAY=MO"))

    def test_unbounded(self):
        self.assertIn("unbounded", self.kinds("FREQ=DAILY"))
        self.assertNotIn("unbounded", self.kinds("FREQ=DAILY;COUNT=5"))
        self.assertNotIn("unbounded", self.kinds("FREQ=DAILY;UNTIL=20270101"))

    def test_dtstart_unsynchronized(self):
        self.assertIn("dtstart-unsynchronized",
                      self.kinds("FREQ=WEEKLY;BYDAY=MO", "2026-10-20 09:00"))
        self.assertNotIn("dtstart-unsynchronized",
                         self.kinds("FREQ=WEEKLY;BYDAY=MO", "2026-10-19 09:00"))

    def test_clean_rule(self):
        findings = [f for f in rc.audit(rc.parse("FREQ=WEEKLY;BYDAY=MO,WE"),
                                        rc._parse_dt("2026-10-19 09:00"))
                    if f.severity != "info"]
        self.assertEqual(findings, [])


class TestExplain(unittest.TestCase):
    def test_plain(self):
        self.assertIn("Every week", rc.explain(rc.parse("FREQ=WEEKLY;BYDAY=MO")))
        self.assertIn("Monday", rc.explain(rc.parse("FREQ=WEEKLY;BYDAY=MO")))

    def test_ordinal(self):
        text = rc.explain(rc.parse("FREQ=MONTHLY;BYDAY=-2MO"))
        self.assertIn("second-to-last Monday", text)

    def test_interval_and_count(self):
        text = rc.explain(rc.parse("FREQ=DAILY;INTERVAL=2;COUNT=10"))
        self.assertIn("Every 2 days", text)
        self.assertIn("10 occurrence(s)", text)

    def test_forever(self):
        self.assertIn("forever", rc.explain(rc.parse("FREQ=YEARLY")))


class TestCLI(unittest.TestCase):
    def run_cli(self, *argv):
        return subprocess.run([sys.executable, SCRIPT, *argv],
                              capture_output=True, text=True, cwd=HERE)

    def test_next(self):
        r = self.run_cli("next", "FREQ=WEEKLY;BYDAY=MO",
                         "--dtstart", "2026-10-19 09:00", "-n", "3")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2026-10-19", r.stdout)
        self.assertIn("2026-10-26", r.stdout)
        self.assertIn("2026-11-02", r.stdout)

    def test_next_exhausted_exit_1(self):
        r = self.run_cli("next", "FREQ=DAILY;COUNT=1",
                         "--dtstart", "2026-10-19")
        self.assertEqual(r.returncode, 0)
        r = self.run_cli("next", "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30",
                         "--dtstart", "2026-01-01")
        self.assertEqual(r.returncode, 1)

    def test_explain(self):
        r = self.run_cli("explain", "FREQ=MONTHLY;BYDAY=1FR")
        self.assertEqual(r.returncode, 0)
        self.assertIn("1st Friday", r.stdout)

    def test_audit_exit_codes(self):
        self.assertEqual(self.run_cli(
            "audit", "FREQ=MONTHLY;BYMONTH=2;BYMONTHDAY=30").returncode, 1)
        self.assertEqual(self.run_cli(
            "audit", "FREQ=WEEKLY;BYDAY=MO").returncode, 0)

    def test_json(self):
        r = self.run_cli("--json", "next", "FREQ=DAILY;COUNT=2",
                         "--dtstart", "2026-10-19")
        self.assertEqual(json.loads(r.stdout),
                         ["2026-10-19 00:00", "2026-10-20 00:00"])
        r = self.run_cli("--json", "audit", "FREQ=YEARLY;BYYEARDAY=366")
        self.assertIn("leap-only", [f["kind"] for f in json.loads(r.stdout)])

    def test_parse_error_exit_2(self):
        self.assertEqual(self.run_cli("explain", "FREQ=WEEKLY;BYMONTHDAY=1").returncode, 2)
        self.assertEqual(self.run_cli("next", "BYDAY=MO").returncode, 2)


if __name__ == "__main__":
    unittest.main()
