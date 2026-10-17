"""Seeded random staging data vs. the pure-Python reference: exact equality.

Seeds: E2E_DIFF_SEEDS (comma separated), default three fixed seeds, so every
run tests the same data.
"""
from __future__ import annotations

import os
import random
from datetime import date, datetime, timedelta

from tests.helpers import DatabaseTestCase, Stop, trip_rows
from tests.reference import reference

SEEDS = [int(s) for s in os.environ.get("E2E_DIFF_SEEDS", "20260921,1,424242").split(",")]

# same bpuic with different spellings (case, umlaut, NULL) to exercise min() in byte order
NAMES = {
    8503000: ["Zürich HB", "Zurich HB", "zürich HB"],
    8502204: ["Zug", "zug", None],
    8502202: ["Rotkreuz"],
    8505000: ["Luzern", "Lucerne"],
    8502206: ["Baar"],
    8503006: ["Thalwil", None],
    8502203: ["Cham"],
    8505004: ["Ebikon"],
}
ROUTES = {  # stop order of each line; trips run on a random contiguous slice, either direction
    ("IR75", "SBB"): [8503000, 8503006, 8502206, 8502204, 8502202, 8505000],
    ("S1", "SBB"): [8502204, 8502203, 8502202, 8505004, 8505000],
    ("S1", "BLS"): [8502204, 8502203, 8502202, 8505000],   # same name, other operator
}
# values around the 0.05-min rounding boundaries (3 s = 0.05 min) and zero
EDGE = [-9, -6, -3, -2, -1, 0, 1, 2, 3, 6, 9, 15, 21, 27, -27]


def delay(rnd: random.Random) -> int | None:
    x = rnd.random()
    if x < 0.25:
        return None
    if x < 0.55:
        return rnd.choice(EDGE)
    return rnd.randint(-300, 1800)


def generate(seed: int) -> list[tuple]:
    rnd = random.Random(seed)
    days = sorted(rnd.sample(range(365), 150))
    rows: list[tuple] = []
    for d in days:
        day = date(2026, 1, 1) + timedelta(days=d)
        for t in range(rnd.randint(6, 16)):
            line, op = rnd.choice(list(ROUTES))
            route = ROUTES[(line, op)]
            if rnd.random() < 0.5:
                route = route[::-1]
            i = rnd.randrange(len(route))
            j = rnd.randint(i, len(route) - 1)          # i == j -> single-stop trip
            stops = route[i:j + 1]
            # few distinct slots so cells collect several observations;
            # some start late in the evening so later stops cross midnight
            hour = rnd.choice([0, 5, 7, 7, 8, 17, 17, 22, 23, 23])
            t0 = datetime(day.year, day.month, day.day, hour, rnd.choice([0, 15, 30, 45, 58]))
            built = []
            for k, b in enumerate(stops):
                arr = t0 + timedelta(minutes=7 * k) if k > 0 else None
                dep = arr + timedelta(minutes=1) if arr else t0
                if k == len(stops) - 1:
                    dep = None
                elif rnd.random() < 0.05:               # missing planned departure
                    dep = None
                built.append(Stop(b, rnd.choice(NAMES[b]), arr, dep,
                                  delay(rnd) if arr else None, delay(rnd) if dep else None))
            rows += trip_rows(day, f"ch:1:sjyid:{seed}:{t}-{d}", built, line, op)
    return rows


class Differential(DatabaseTestCase):

    def test_matches_reference(self):
        for seed in SEEDS:
            with self.subTest(seed=seed):
                self.execute("TRUNCATE staging.stop_events")
                rows = generate(seed)
                self.load(rows)
                self.refresh()
                expected = reference(rows)
                got = self.report()
                self.assertGreater(len(expected), 200)
                # the data must really contain the interesting cases
                self.assertTrue(any(r[10] == 0 for r in expected), "no row without departures")
                self.assertTrue(any(r[11] == 0 for r in expected), "no row without arrivals")
                self.assertTrue(any(r[10] >= 4 and r[10] % 2 == 0 for r in expected), "no even counts")
                self.assertEqual(got, expected)