"""Hand-checkable scenarios: known medians, rounding, NULLs, slots, pair generation."""
from __future__ import annotations

from tests import scenarios
from tests.helpers import DatabaseTestCase
from tests.reference import reference

# (line, operator, from, from_stop, to, to_stop, weekday, hour, dep_min, arr_min, n_dep, n_arr)
# worked out by hand from scenarios.py; see the block letters there.
EXPECTED = [
    # G  L1/BLS: 9 s = 0.15 -> 0.2, no arrival
    ("L1", "BLS", 1001, "Alpha", 1002, "Beta", 1, 7, "0.2", None, 1, 0),
    # A  dep [-30, 90, 150] -> 90 s; arr [45, -15] -> 15 s = 0.25 -> 0.3; 'Alpha' < 'alpha' (byte order)
    ("L1", "SBB", 1001, "Alpha", 1002, "Beta", 1, 7, "1.5", "0.3", 3, 2),
    # B  dep [-5, -3, -3, -1] -> -3 s = -0.05 -> -0.1; arr [-3, -2] -> -2.5 s = -0.0417 -> 0.0
    ("L1", "SBB", 1001, "Alpha", 1002, "Beta", 1, 8, "-0.1", "0.0", 4, 2),
    # C  no departures; arr [61, 600] -> 330.5 s = 5.508 -> 5.5
    ("L1", "SBB", 1001, "Alpha", 1002, "Beta", 2, 9, None, "5.5", 0, 2),
    # I  Sunday 23:50 departure, arrival after midnight still belongs to slot Sun 23
    ("L1", "SBB", 1001, "Alpha", 1002, "Beta", 7, 23, "2.0", "-2.0", 1, 1),
    # J  dep [10, 20, 31, 41] -> 25.5 s = 0.425 -> 0.4; arr [100, 200, 400] -> 200 s = 3.33 -> 3.3
    ("L1", "SBB", 1001, "Alpha", 1003, "Gamma", 4, 17, "0.4", "3.3", 4, 3),
    # K  dep [-12, -6] -> -9 s = -0.15 -> -0.2; arr [6, 12] -> 9 s -> 0.2
    ("L1", "SBB", 1002, "Beta", 1001, "Alpha", 1, 18, "-0.2", "0.2", 2, 2),
    # L  dep [1, 2] -> 1.5 s = 0.025 -> 0.0; arr [-4, -3] -> -3.5 s = -0.058 -> -0.1
    ("L1", "SBB", 1002, "Beta", 1001, "Alpha", 1, 19, "0.0", "-0.1", 2, 2),
    # F  only Beta->Gamma (Alpha has no sched_dep): 20 s -> 0.3, 40 s -> 0.7
    ("L1", "SBB", 1002, "Beta", 1003, "Gamma", 5, 12, "0.3", "0.7", 1, 1),
    # G  L2 shares Alpha->Beta Mon 07 with L1, separate row
    ("L2", "SBB", 1001, "Alpha", 1002, "Beta", 1, 7, "5.0", "6.0", 1, 1),
    # H  all ordered pairs of one 3-stop trip
    ("L2", "SBB", 1001, "Alpha", 1002, "Beta", 6, 13, "0.1", "0.2", 1, 1),
    ("L2", "SBB", 1001, "Alpha", 1003, "Gamma", 6, 13, "0.1", "0.4", 1, 1),
    ("L2", "SBB", 1002, "Beta", 1003, "Gamma", 6, 13, "0.3", "0.4", 1, 1),
    # I  Alpha departs 00:06 Monday -> weekday 1, slot 0; -59 s -> -1.0, 3 s = 0.05 -> 0.1
    ("L3", "SBB", 1001, "Alpha", 1002, "Beta", 1, 0, "-1.0", "0.1", 1, 1),
    ("L3", "SBB", 1003, "Gamma", 1001, "Alpha", 7, 23, "0.0", "1.0", 1, 1),
    ("L3", "SBB", 1003, "Gamma", 1002, "Beta", 7, 23, "0.0", "0.1", 1, 1),
]


class Scenarios(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        self.rows = scenarios.build()
        self.load(self.rows)
        self.notices = self.refresh()

    def test_expected_rows(self):
        self.assertEqual(self.report(), EXPECTED)

    def test_reference_agrees_with_hand_values(self):
        # guards the reference used by the differential test
        self.assertEqual(reference(self.rows), EXPECTED)

    def test_no_negative_zero_and_one_decimal(self):
        bad = self.query("""SELECT count(*) FROM reporting.od_delay_by_slot
                            WHERE median_dep_delay_min::text LIKE '-0.0%'
                               OR median_arr_delay_min::text LIKE '-0.0%'
                               OR median_dep_delay_min::text !~ '^-?[0-9]+\\.[0-9]$'
                               OR median_arr_delay_min::text !~ '^-?[0-9]+\\.[0-9]$'""")
        self.assertEqual(bad, [(0,)])

    def test_excluded_cases_have_no_rows(self):
        got = {(r[0], r[1], r[2], r[4], r[6], r[7]) for r in self.report()}
        self.assertNotIn(("L1", "SBB", 1001, 1002, 3, 10), got)   # D both NULL
        self.assertFalse([k for k in got if k[4] == 4 and k[5] == 11])  # E single stop
        self.assertFalse([k for k in got if k[4] == 5 and k[2] == 1001])  # F no sched_dep

    def test_notice_summary(self):
        self.assertEqual(len(self.notices), 1)
        self.assertRegex(self.notices[0],
                         r"^reporting\.refresh_od_delay: rows_deleted=0 rows_written=16 "
                         r"distinct_pairs=6 duration=\d+\.\d+s \(lock_waited=\d+\.\d+s\)$")