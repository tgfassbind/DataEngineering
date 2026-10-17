"""Hand-checkable staging data for the aggregation tests.

Stops: 1001 Alpha, 1002 Beta, 1003 Gamma. Mondays: 2026-09-07/14/21/28.
Every block says what it is there for; the expected rows are in
test_refresh_scenarios.py (worked out by hand, see the comments there).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from tests.helpers import Stop, trip_rows

A, B, C = 1001, 1002, 1003
MON = [date(2026, 9, 7), date(2026, 9, 14), date(2026, 9, 21), date(2026, 9, 28)]


def at(day: date, hhmm: str, plus_days: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m) + timedelta(days=plus_days)


def two_stop(day, trip, dep_time, arr_time, dep_s, arr_s, line="L1", operator="SBB",
             frm=(A, "Alpha"), to=(B, "Beta")):
    return trip_rows(day, trip, [
        Stop(frm[0], frm[1], None, at(day, dep_time), None, dep_s),
        Stop(to[0], to[1], at(day, arr_time), None, arr_s, None),
    ], line, operator)


def build() -> list[tuple]:
    rows: list[tuple] = []

    # A) odd count with NULLs and negatives; stop-name min() (NULL name, case variant)
    #    L1 Alpha->Beta, Mondays 07:10
    rows += two_stop(MON[0], "a1", "07:10", "07:30", -30, 45)
    rows += trip_rows(MON[1], "a1", [Stop(A, "Alpha", None, at(MON[1], "07:10"), None, 90),
                                     Stop(B, None, at(MON[1], "07:30"), None, None, None)])
    rows += two_stop(MON[2], "a1", "07:10", "07:30", None, -15)
    rows += two_stop(MON[3], "a1", "07:10", "07:30", 150, None, frm=(A, "alpha"))

    # B) even count; -0.05 min -> -0.1 (half away from zero); -2.5 s = -0.04 min -> 0.0
    #    L1 Alpha->Beta, Mondays 08:00
    for day, dep, arr in zip(MON, (-1, -5, -3, -3), (-2, -3, None, None)):
        rows += two_stop(day, "b1", "08:00", "08:20", dep, arr)

    # C) departures all NULL -> median NULL, n_dep_obs 0, row kept (arrivals exist)
    rows += two_stop(date(2026, 9, 8), "c1", "09:00", "09:20", None, 600)
    rows += two_stop(date(2026, 9, 15), "c1", "09:00", "09:20", None, 61)

    # D) both NULL -> no row (Wednesday 10:00)
    rows += two_stop(date(2026, 9, 9), "d1", "10:00", "10:20", None, None)

    # E) trip with a single stop -> no pair (Thursday 11:00)
    rows += trip_rows(date(2026, 9, 10), "e1", [Stop(A, "Alpha", None, at(date(2026, 9, 10), "11:00"), None, 30)])

    # F) origin without sched_dep -> no pairs from it; Beta->Gamma remains (Friday)
    f = date(2026, 9, 11)
    rows += trip_rows(f, "f1", [
        Stop(A, "Alpha", None, None, None, 50),
        Stop(B, "Beta", at(f, "12:15"), at(f, "12:20"), 10, 20),
        Stop(C, "Gamma", at(f, "12:40"), None, 40, None),
    ])

    # G) two lines / two operators sharing Alpha->Beta in the Monday 07 slot of block A
    rows += two_stop(MON[0], "g1", "07:20", "07:40", 300, 360, line="L2")
    rows += two_stop(MON[0], "g2", "07:40", "08:00", 9, None, operator="BLS")

    # H) all ordered pairs of a 3-stop trip; B->C takes its slot from Beta's departure
    h = date(2026, 9, 12)   # Saturday
    rows += trip_rows(h, "h1", [
        Stop(A, "Alpha", None, at(h, "13:05"), None, 6),
        Stop(B, "Beta", at(h, "13:15"), at(h, "13:25"), 12, 18),
        Stop(C, "Gamma", at(h, "13:35"), None, 24, None),
    ], line="L2")

    # I) midnight: operating day Sunday, stops after midnight are Monday, slots 23 and 0
    s = date(2026, 9, 13)   # Sunday
    rows += trip_rows(s, "i1", [
        Stop(C, "Gamma", None, at(s, "23:55"), None, 0),
        Stop(A, "Alpha", at(s, "00:05", 1), at(s, "00:06", 1), 59, -59),
        Stop(B, "Beta", at(s, "00:15", 1), None, 3, None),
    ], line="L3")
    rows += trip_rows(s, "i2", [
        Stop(A, "Alpha", None, at(s, "23:50"), None, 120),
        Stop(B, "Beta", at(s, "00:05", 1), None, -120, None),
    ])

    # J) several trips per day in one cell, across two Thursdays (Alpha->Gamma, hour 17)
    for day, trip, t, dep, arr in [
        (date(2026, 9, 10), "j1", "17:00", 10, 100),
        (date(2026, 9, 10), "j2", "17:20", 20, 200),
        (date(2026, 9, 10), "j3", "17:40", 31, None),
        (date(2026, 9, 17), "j1", "17:59", 41, 400),
    ]:
        rows += two_stop(day, trip, t, "18:30", dep, arr, to=(C, "Gamma"))

    # K) even count whose median is exactly +-0.15 min -> +-0.2
    for day, dep, arr in zip(MON[:2], (-6, -12), (6, 12)):
        rows += two_stop(day, "k1", "18:10", "18:30", dep, arr, frm=(B, "Beta"), to=(A, "Alpha"))

    # L) half-second medians: 1.5 s = 0.025 min -> 0.0; -3.5 s = -0.0583 min -> -0.1
    for day, dep, arr in zip(MON[:2], (1, 2), (-3, -4)):
        rows += two_stop(day, "l1", "19:10", "19:30", dep, arr, frm=(B, "Beta"), to=(A, "Alpha"))

    return rows