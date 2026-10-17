"""Performance: about one million pair observations.

60 days x 160 trips x 15 stops = 144,000 staging rows -> 60*160*(15*14/2) =
1,008,000 ordered stop pairs. For comparison: the fixture day (IR75 + S1)
gives 1,914 pairs, so a full year is roughly 0.7 million.
Skip with E2E_SKIP_PERF=1. The time limit is generous on purpose (slow laptops);
the measured duration is printed.
"""
from __future__ import annotations

import os
import time
import unittest

from tests.helpers import DatabaseTestCase

DAYS, TRIPS, STOPS = 60, 160, 15
LIMIT_S = 120


@unittest.skipIf(os.environ.get("E2E_SKIP_PERF") == "1", "E2E_SKIP_PERF=1")
class Performance(DatabaseTestCase):

    def test_one_million_pairs(self):
        # deterministic synthetic data, generated inside PostgreSQL (fast)
        self.execute(f"""
            INSERT INTO staging.stop_events (betriebstag, fahrt_bezeichner, bpuic, haltestellen_name,
                linien_text, betreiber_abk, stop_sequence, sched_arr, sched_dep,
                arr_delay_s, dep_delay_s, is_additional_trip, row_hash)
            SELECT day, 'trip-' || t, 8500000 + s, 'Stop ' || s,
                   CASE WHEN t % 2 = 0 THEN 'S1' ELSE 'IR75' END, 'SBB', s,
                   CASE WHEN s > 1 THEN day + make_interval(mins => 300 + t * 6 + s * 3) END,
                   CASE WHEN s < {STOPS} THEN day + make_interval(mins => 301 + t * 6 + s * 3) END,
                   CASE WHEN s > 1 AND (t + s) % 7 <> 0 THEN ((t * 37 + s * 11 + d) % 600) - 120 END,
                   CASE WHEN s < {STOPS} AND (t + s) % 5 <> 0 THEN ((t * 29 + s * 13 + d) % 600) - 120 END,
                   false, md5(day::text || t || '-' || s) || md5(t || '-' || s)
            FROM generate_series(0, {DAYS - 1}) AS d,
                 LATERAL (SELECT date '2026-01-05' + d AS day) AS dd,
                 generate_series(1, {TRIPS}) AS t,
                 generate_series(1, {STOPS}) AS s""")
        self.execute("ANALYZE staging.stop_events")
        pairs = self.query("""SELECT count(*) FROM staging.stop_events a JOIN staging.stop_events b
                              ON b.betriebstag = a.betriebstag AND b.fahrt_bezeichner = a.fahrt_bezeichner
                             AND b.stop_sequence > a.stop_sequence WHERE a.sched_dep IS NOT NULL""")[0][0]
        self.assertEqual(pairs, DAYS * TRIPS * STOPS * (STOPS - 1) // 2)
        t0 = time.monotonic()
        notices = self.refresh()
        first = time.monotonic() - t0
        t0 = time.monotonic()
        self.refresh()                         # second run also deletes the previous content
        second = time.monotonic() - t0
        rows = self.query("SELECT count(*) FROM reporting.od_delay_by_slot")[0][0]
        print(f"\n  performance: {pairs:,} pair observations -> {rows:,} rows; "
              f"first CALL {first:.1f}s, second CALL {second:.1f}s\n  {notices[0]}")
        self.assertGreater(rows, 0)
        self.assertLess(max(first, second), LIMIT_S)