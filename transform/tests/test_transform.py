"""Read + validate + compute, without S3 or PostgreSQL.

Expected numbers are derived from the fixture with the csv module in the
test itself, never from the transform code.
"""
from __future__ import annotations

import io
import os
import unittest
from unittest import mock
from collections import Counter
from datetime import date, datetime

import pandas as pd

from app.compute import build_rows
from app.config import ConfigError, Limits, Line, parse_line_spec, resolve_lines
from app.read import RawFileInvalid, filter_chunks
from app.validate import validate
from tests.helpers import FIXTURE, fixture_rows, make_row, to_csv_bytes

IR75 = [Line("IR75", "SBB")]
DAY = date(2026, 9, 21)


def run(data: bytes, lines=IR75, limits=Limits(), chunk_size=1000):
    read = filter_chunks(io.BytesIO(data), lines, chunk_size)
    validated, stats = validate(read.frame, limits)
    return read, build_rows(validated, DAY), stats


def is_null(v) -> bool:
    return v is None or bool(pd.isna(v))


def parse(v: str) -> datetime:
    return datetime.strptime(v, "%d.%m.%Y %H:%M:%S" if v.count(":") == 2 else "%d.%m.%Y %H:%M")


class FixtureFacts(unittest.TestCase):
    """The raw fixture itself has the documented properties."""

    rows = fixture_rows()

    def test_shape(self):
        self.assertEqual(len(self.rows), 450)
        self.assertEqual(len({r["FAHRT_BEZEICHNER"] for r in self.rows}), 51)
        self.assertEqual({(r["LINIEN_TEXT"], r["BETREIBER_ABK"]) for r in self.rows},
                         {("IR75", "SBB")})
        keys = Counter((r["FAHRT_BEZEICHNER"], r["BPUIC"]) for r in self.rows)
        self.assertEqual(max(keys.values()), 1)

    def test_status_counts(self):
        self.assertEqual(Counter(r["AN_PROGNOSE_STATUS"] for r in self.rows),
                         {"REAL": 377, "PROGNOSE": 17, "UNBEKANNT": 5, "": 51})
        self.assertEqual(Counter(r["AB_PROGNOSE_STATUS"] for r in self.rows),
                         {"REAL": 394, "UNBEKANNT": 5, "PROGNOSE": 1, "": 50})
        cancelled = [r for r in self.rows if r["FAELLT_AUS_TF"] == "true"]
        self.assertEqual(len(cancelled), 5)
        for r in cancelled:
            self.assertEqual((r["AN_PROGNOSE_STATUS"], r["AN_PROGNOSE"], r["AB_PROGNOSE"]),
                             ("UNBEKANNT", "", ""))

    def test_every_trip_has_one_first_and_one_last_stop(self):
        by_trip = Counter(r["FAHRT_BEZEICHNER"] for r in self.rows if not r["ANKUNFTSZEIT"])
        self.assertEqual(len(by_trip), 51)
        self.assertEqual(set(by_trip.values()), {1})
        by_trip = Counter(r["FAHRT_BEZEICHNER"] for r in self.rows if not r["ABFAHRTSZEIT"])
        self.assertEqual(len(by_trip), 51)
        self.assertEqual(set(by_trip.values()), {1})


class FixtureTransform(unittest.TestCase):
    """The transform output for the fixture meets the acceptance criteria."""

    @classmethod
    def setUpClass(cls):
        cls.raw = fixture_rows()
        cls.read, cls.out, cls.stats = run(FIXTURE.read_bytes(), chunk_size=37)
        cls.by_key = {(r.fahrt_bezeichner, r.bpuic): r for r in cls.out.itertuples()}

    def test_counts(self):
        self.assertEqual(self.read.rows_read, 450)
        self.assertEqual(self.read.rows_after_filter, 450)
        self.assertEqual(len(self.out), 445)                       # 450 - 5 cancelled
        self.assertEqual(self.out["fahrt_bezeichner"].nunique(), 51)
        self.assertEqual(int(self.out["arr_delay_s"].notna().sum()), 377)
        self.assertEqual(int(self.out["dep_delay_s"].notna().sum()), 394)
        self.assertEqual(self.stats.rows_dropped,
                         {"cancelled": 5, "pass_through": 0, "invalid_key": 0, "duplicate_key": 0})

    def test_no_plausibility_or_time_order_rejections(self):
        for side in ("arr", "dep"):
            rej = self.stats.timestamps_rejected[side]
            self.assertEqual((rej["missing_or_unparseable"], rej["delay_out_of_range"],
                              rej["time_order"]), (0, 0, 0), side)
        self.assertEqual(self.stats.timestamps_rejected["arr"]["not_real"], 17)
        self.assertEqual(self.stats.timestamps_rejected["dep"]["not_real"], 1)

    def test_delays_equal_independent_computation(self):
        """Every stored delay equals actual - planned from the raw row (REAL
        only), and every other delay is NULL - in particular never 0."""
        for r in self.raw:
            if r["FAELLT_AUS_TF"] == "true":
                self.assertNotIn((r["FAHRT_BEZEICHNER"], int(r["BPUIC"])), self.by_key)
                continue
            row = self.by_key[(r["FAHRT_BEZEICHNER"], int(r["BPUIC"]))]
            for planned, actual, status, got in (
                    ("ANKUNFTSZEIT", "AN_PROGNOSE", "AN_PROGNOSE_STATUS", row.arr_delay_s),
                    ("ABFAHRTSZEIT", "AB_PROGNOSE", "AB_PROGNOSE_STATUS", row.dep_delay_s)):
                if r[status] == "REAL":
                    want = int((parse(r[actual]) - parse(r[planned])).total_seconds())
                    self.assertEqual(got, want)
                else:
                    self.assertTrue(is_null(got), (r, got))

    def test_first_and_last_stops_are_null_not_zero(self):
        first = self.out[self.out["stop_sequence"] == 1]
        self.assertEqual(len(first), 51)
        self.assertTrue(first["sched_arr"].isna().all())
        self.assertTrue(first["arr_delay_s"].isna().all())
        last = self.out.loc[self.out.groupby("fahrt_bezeichner")["stop_sequence"].idxmax()]
        no_dep = last[last["sched_dep"].isna()]
        # 50 trips end normally; trip 2110-001 is cancelled from Thalwil on,
        # so its last *served* stop still has a departure.
        self.assertEqual(len(no_dep), 50)
        self.assertTrue(no_dep["dep_delay_s"].isna().all())

    def test_zero_delays_are_real_measurements(self):
        for row in self.out.itertuples():
            for side, value in (("arr", row.arr_delay_s), ("dep", row.dep_delay_s)):
                if not is_null(value) and value == 0:
                    raw = next(r for r in self.raw if r["FAHRT_BEZEICHNER"] == row.fahrt_bezeichner
                               and int(r["BPUIC"]) == row.bpuic)
                    col = "AN" if side == "arr" else "AB"
                    self.assertEqual(raw[f"{col}_PROGNOSE_STATUS"], "REAL")

    def test_early_arrivals_kept(self):
        early = int((self.out["arr_delay_s"] < 0).sum())
        raw_early = sum(1 for r in self.raw if r["AN_PROGNOSE_STATUS"] == "REAL"
                        and parse(r["AN_PROGNOSE"]) < parse(r["ANKUNFTSZEIT"]))
        self.assertEqual(early, raw_early)
        self.assertGreaterEqual(early, 90)

    def test_midnight_stop(self):
        row = self.by_key[("ch:1:sjyid:100001:2592-001", 8502206)]
        self.assertEqual(row.haltestellen_name, "Baar")
        self.assertEqual(row.arr_delay_s, 68)     # 23:59:08 - 23:58
        self.assertEqual(row.dep_delay_s, 164)    # 22.09. 00:00:44 - 21.09. 23:58

    def test_stop_sequence_follows_planned_time(self):
        """Independent check: within a trip, every stop's planned times are not
        earlier than the previous stop's, comparing departure(A) with
        arrival(B) where both exist."""
        for _, trip in self.out.groupby("fahrt_bezeichner"):
            trip = trip.sort_values("stop_sequence")
            self.assertEqual(list(trip["stop_sequence"]), list(range(1, len(trip) + 1)))
            prev_dep = None
            for row in trip.itertuples():
                if prev_dep is not None and not is_null(row.sched_arr):
                    self.assertGreaterEqual(row.sched_arr, prev_dep)
                if not is_null(row.sched_dep):
                    prev_dep = row.sched_dep

    def test_order_independent_of_file_order(self):
        lines = FIXTURE.read_bytes().decode().splitlines()
        shuffled = "\n".join([lines[0]] + list(reversed(lines[1:]))) + "\n"
        _, out2, _ = run(shuffled.encode())
        key = ["fahrt_bezeichner", "bpuic"]
        a = self.out.sort_values(key).reset_index(drop=True)
        b = out2.sort_values(key).reset_index(drop=True)
        self.assertEqual(list(a["row_hash"]), list(b["row_hash"]))

    def test_additional_trips_kept(self):
        want = sum(1 for r in self.raw if r["ZUSATZFAHRT_TF"] == "true" and r["FAELLT_AUS_TF"] != "true")
        self.assertEqual(int(self.out["is_additional_trip"].sum()), want)
        self.assertGreater(want, 0)

    def test_hash_is_sha256_and_deterministic(self):
        self.assertTrue(self.out["row_hash"].str.fullmatch(r"[0-9a-f]{64}").all())
        self.assertEqual(self.out["row_hash"].nunique(), len(self.out))
        _, again, _ = run(FIXTURE.read_bytes(), chunk_size=1000)
        self.assertEqual(list(self.out["row_hash"]), list(again["row_hash"]))


class ValidationRules(unittest.TestCase):
    """Synthetic trips for the rules the fixture does not exercise."""

    def trip(self, *stops, trip="T1", **kw):
        return [make_row(trip, 100 + i, f"S{i}", **s, **kw) for i, s in enumerate(stops)]

    def test_status_must_be_real_per_timestamp(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:00:30", dep_status="PROGNOSE"),
                         dict(arr="10:10", arr_act="10:11:00", arr_status="REAL",
                              dep="10:11", dep_act="10:12:00", dep_status="UNBEKANNT"),
                         dict(arr="10:20", arr_act="10:20:10", arr_status=""))
        _, out, st = run(to_csv_bytes(rows))
        self.assertEqual([None if is_null(v) else v for v in out["arr_delay_s"]], [None, 60, None])
        self.assertTrue(out["dep_delay_s"].isna().all())
        self.assertEqual(st.timestamps_rejected["arr"]["not_real"], 1)  # empty status, time present
        self.assertEqual(st.timestamps_rejected["dep"]["not_real"], 2)

    def test_cancelled_and_pass_through_rows_dropped(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:00:00", dep_status="REAL"),
                         dict(arr="10:05", dep="10:05"),
                         dict(arr="10:10", arr_act="10:10:00", arr_status="REAL"))
        rows[1]["DURCHFAHRT_TF"] = "true"
        rows.append(make_row("T2", 1, "X", dep="11:00", cancelled="true", dep_status="UNBEKANNT"))
        _, out, st = run(to_csv_bytes(rows))
        self.assertEqual(list(out["haltestellen_name"]), ["S0", "S2"])
        self.assertEqual(list(out["stop_sequence"]), [1, 2])
        self.assertEqual(st.rows_dropped["pass_through"], 1)
        self.assertEqual(st.rows_dropped["cancelled"], 1)

    def test_unparseable_and_out_of_range(self):
        rows = self.trip(dict(dep="10:00", dep_act="garbage", dep_status="REAL"),
                         dict(arr="10:10", arr_act="13:10:01", arr_status="REAL",   # 3 h + 1 s
                              dep="10:11", dep_act="", dep_status="REAL"),
                         dict(arr="13:20", arr_act="10:20:00", arr_status="REAL"))  # -3 h exactly
        _, out, st = run(to_csv_bytes(rows))
        self.assertEqual(st.timestamps_rejected["dep"]["missing_or_unparseable"], 2)
        self.assertEqual(st.timestamps_rejected["arr"]["delay_out_of_range"], 1)
        self.assertEqual(out["arr_delay_s"].iloc[2], -10800)        # limit is inclusive

    def test_limits_are_parameters(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:01:00", dep_status="REAL"))
        _, out, st = run(to_csv_bytes(rows), limits=Limits(max_abs_delay_s=59))
        self.assertTrue(out["dep_delay_s"].isna().all())
        self.assertEqual(st.timestamps_rejected["dep"]["delay_out_of_range"], 1)

    def test_same_stop_arrival_after_departure(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:00:00", dep_status="REAL"),
                         dict(arr="10:10", arr_act="10:12:00", arr_status="REAL",
                              dep="10:11", dep_act="10:11:30", dep_status="REAL"),
                         dict(arr="10:20", arr_act="10:20:00", arr_status="REAL"))
        _, out, st = run(to_csv_bytes(rows))
        self.assertTrue(is_null(out["arr_delay_s"].iloc[1]))
        self.assertTrue(is_null(out["dep_delay_s"].iloc[1]))
        self.assertEqual(out["dep_delay_s"].iloc[0], 0)            # not involved: stays
        self.assertEqual(out["arr_delay_s"].iloc[2], 0)
        self.assertEqual(st.timestamps_rejected["arr"]["time_order"], 1)
        self.assertEqual(st.timestamps_rejected["dep"]["time_order"], 1)

    def test_consecutive_stops_order_and_segment_length(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:30:00", dep_status="REAL"),   # departs after
                         dict(arr="10:10", arr_act="10:20:00", arr_status="REAL",     # next arrival
                              dep="10:11", dep_act="10:21:00", dep_status="REAL"),
                         dict(arr="17:00", arr_act="17:00:00", arr_status="REAL"))    # segment > 6 h
        _, out, st = run(to_csv_bytes(rows))
        self.assertEqual(out["dep_delay_s"].isna().tolist(), [True, True, True])
        self.assertEqual(out["arr_delay_s"].isna().tolist(), [True, True, True])
        self.assertEqual(st.timestamps_rejected["dep"]["time_order"], 2)
        self.assertEqual(st.timestamps_rejected["arr"]["time_order"], 2)

    def test_midnight_crossing_is_valid(self):
        rows = self.trip(dict(dep="23:50", dep_act="23:59:30", dep_status="REAL"),
                         dict(arr="23:58", arr_act="22.09.2026 00:07:00", arr_status="REAL",
                              dep="23:59", dep_act="22.09.2026 00:08:00", dep_status="REAL"),
                         dict(arr="22.09.2026 00:10", arr_act="22.09.2026 00:19:00", arr_status="REAL"))
        _, out, st = run(to_csv_bytes(rows))
        self.assertEqual(out["arr_delay_s"].tolist()[1:], [540, 540])
        self.assertEqual(out["dep_delay_s"].tolist()[:2], [570, 540])
        self.assertEqual(sum(st.timestamps_rejected["arr"].values()), 0)

    def test_planned_time_tie_does_not_depend_on_file_order(self):
        a = make_row("T1", 1, "A", dep="10:00", dep_act="10:00:00", dep_status="REAL")
        b = make_row("T1", 2, "B", arr="10:00", arr_act="10:00:50", arr_status="REAL",
                     dep="10:01", dep_act="10:01:10", dep_status="REAL")
        c = make_row("T1", 3, "C", arr="10:05", arr_act="10:05:00", arr_status="REAL")
        for order in ([a, b, c], [c, b, a], [b, c, a]):
            _, out, st = run(to_csv_bytes(order))
            self.assertEqual(list(out["haltestellen_name"]), ["A", "B", "C"])
            self.assertEqual(sum(st.timestamps_rejected["arr"].values()), 0)

    def test_rule_boundaries(self):
        # same stop: actual arrival == actual departure is valid;
        # consecutive: dep(A) == arr(B) is invalid (strict <);
        # segment exactly MAX_SEGMENT_DURATION_S is valid.
        rows = self.trip(dict(dep="10:00", dep_act="10:00:00", dep_status="REAL"),
                         dict(arr="10:10", arr_act="10:10:00", arr_status="REAL",
                              dep="10:10", dep_act="10:10:00", dep_status="REAL"),
                         dict(arr="16:10", arr_act="16:10:00", arr_status="REAL",
                              dep="16:11", dep_act="16:11:00", dep_status="REAL"),
                         dict(arr="16:20", arr_act="16:11:00", arr_status="REAL"))
        _, out, st = run(to_csv_bytes(rows), limits=Limits(max_abs_delay_s=10800,
                                                           max_segment_duration_s=21600))
        self.assertEqual([None if is_null(v) else v for v in out["arr_delay_s"]],
                         [None, 0, 0, None])
        self.assertEqual([None if is_null(v) else v for v in out["dep_delay_s"]],
                         [0, 0, None, None])
        self.assertEqual(st.timestamps_rejected["arr"]["time_order"], 1)
        self.assertEqual(st.timestamps_rejected["dep"]["time_order"], 1)

    def test_status_spelling(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:00:20", dep_status=" REAL "),
                         dict(arr="10:10", arr_act="10:10:00", arr_status="real"))
        _, out, st = run(to_csv_bytes(rows))
        self.assertEqual(out["dep_delay_s"].iloc[0], 20)            # whitespace stripped
        self.assertTrue(is_null(out["arr_delay_s"].iloc[1]))         # case-sensitive
        self.assertEqual(st.timestamps_rejected["arr"]["not_real"], 1)

    def test_duplicate_in_the_middle_does_not_affect_neighbours(self):
        a = make_row("T1", 1, "A", dep="10:00", dep_act="10:00:30", dep_status="REAL")
        b = make_row("T1", 2, "B", arr="10:10", arr_act="10:10:30", arr_status="REAL",
                     dep="10:11", dep_act="10:11:30", dep_status="REAL")
        a_again = make_row("T1", 1, "A-again", arr="10:15", arr_act="10:05:00", arr_status="REAL",
                           dep="10:16", dep_act="10:16:00", dep_status="REAL")
        c = make_row("T1", 3, "C", arr="10:20", arr_act="10:20:30", arr_status="REAL")
        _, out, st = run(to_csv_bytes([a, b, a_again, c]))
        self.assertEqual(list(out["haltestellen_name"]), ["A", "B", "C"])
        self.assertEqual(list(out["stop_sequence"]), [1, 2, 3])
        self.assertEqual(list(out["dep_delay_s"])[:2], [30, 30])
        self.assertEqual(list(out["arr_delay_s"])[1:], [30, 30])
        self.assertEqual(st.rows_dropped["duplicate_key"], 1)
        self.assertEqual(sum(st.timestamps_rejected["dep"].values()), 0)

    def test_empty_input(self):
        _, out, _ = run(to_csv_bytes([make_row("A", 1, "a", dep="10:00", line="S9")]))
        self.assertEqual(len(out), 0)
        self.assertEqual(list(out.columns)[-1], "row_hash")

    def test_duplicate_key_keeps_first_by_sequence(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:00:00", dep_status="REAL"),
                         dict(arr="10:10", arr_act="10:10:00", arr_status="REAL"))
        dup = make_row("T1", 100, "S0-again", arr="10:20", arr_act="10:21:00", arr_status="REAL")
        _, out, st = run(to_csv_bytes([rows[1], dup, rows[0]]))   # file order scrambled
        self.assertEqual(st.rows_dropped["duplicate_key"], 1)
        self.assertEqual(list(out["haltestellen_name"]), ["S0", "S1"])
        self.assertEqual(list(out["stop_sequence"]), [1, 2])

    def test_invalid_key_dropped(self):
        rows = self.trip(dict(dep="10:00", dep_act="10:00:00", dep_status="REAL"))
        rows.append(make_row("T1", 0, "bad", arr="10:10"))
        rows[-1]["BPUIC"] = "85x"
        _, out, st = run(to_csv_bytes(rows))
        self.assertEqual(len(out), 1)
        self.assertEqual(st.rows_dropped["invalid_key"], 1)


class ReadAndLines(unittest.TestCase):

    def test_line_filter_needs_text_and_operator(self):
        rows = [make_row("A", 1, "a", dep="10:00", line="IR75", operator="SBB"),
                make_row("B", 1, "b", dep="10:00", line="S1", operator="SBB"),
                make_row("C", 1, "c", dep="10:00", line="S1", operator="BLS"),
                make_row("D", 1, "d", dep="10:00", line="IR75", operator="XYZ")]
        data = to_csv_bytes(rows)
        read = filter_chunks(io.BytesIO(data), [Line("S1", "SBB")], 2)
        self.assertEqual(read.rows_read, 4)
        self.assertEqual(list(read.frame["FAHRT_BEZEICHNER"]), ["B"])
        read = filter_chunks(io.BytesIO(data), [Line("IR75", "SBB"), Line("S1", "BLS")], 1)
        self.assertEqual(sorted(read.frame["FAHRT_BEZEICHNER"]), ["A", "C"])

    def test_semicolon_files_and_bom_crlf(self):
        rows = [make_row("A", 1, "a", dep="10:00")]
        semi = filter_chunks(io.BytesIO(to_csv_bytes(rows, sep=";")), IR75, 10)
        self.assertEqual((semi.separator, semi.rows_after_filter), (";", 1))
        bom = b"\xef\xbb\xbf" + to_csv_bytes(rows).replace(b"\n", b"\r\n")
        self.assertEqual(filter_chunks(io.BytesIO(bom), IR75, 10).rows_after_filter, 1)

    def test_malformed_input_fails_loudly(self):
        good = to_csv_bytes([make_row("A", 1, "a", dep="10:00")] * 3)
        for bad in (b"<html>error</html>\n",
                    good.replace(b"\n", b",extra\n", 2).replace(b",extra\n", b"\n", 1),
                    good[:-30] + b"\n", b""):
            with self.assertRaises(RawFileInvalid):
                filter_chunks(io.BytesIO(bad), IR75, 10)

    def test_line_spec_and_precedence(self):
        self.assertEqual(parse_line_spec(" S1 : SBB "), Line("S1", "SBB"))
        for bad in ("IR75", ":SBB", "IR75:"):
            with self.assertRaises(ConfigError):
                parse_line_spec(bad)
        lines, src = resolve_lines(["IR75:SBB,S1:SBB", "S1:SBB"])
        self.assertEqual((lines, src), ([Line("IR75", "SBB"), Line("S1", "SBB")], "--lines"))
        with self.assertRaises(ConfigError):
            resolve_lines([","])
        with mock.patch.dict(os.environ, {"TRANSFORM_LINES_FILE": ""}):
            lines, _ = resolve_lines(None)        # shipped config/lines.yml
        self.assertEqual(lines, IR75)


if __name__ == "__main__":
    unittest.main()
