"""End to end: real CLI against a local Cloud Storage emulator.

The transform uses the real Google client; STORAGE_EMULATOR_HOST points it at
gcp-storage-emulator (in memory, started by the tests). No Google account,
key, Docker or network needed, and the real bucket is never touched.

What is checked: raw/ in, processed/<day>_stop_events.parquet and
processed/<day>_run_scope.parquet out. The load into BigQuery
(bigquery/sql/10_load_core.sql) is not part of this job.

Expected values come from the fixture via the csv module, not from app code.
"""
from __future__ import annotations

import io
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq
from google.cloud import storage

from app import cli
from tests.helpers import FIXTURE, fixture_rows, make_row, to_csv_bytes

DAY = date(2026, 9, 21)
BUCKET = "e2e-transform"
PROJECT = "test-project"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class EndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        port = free_port()
        cls.emulator_host = f"http://127.0.0.1:{port}"
        cls.emulator = subprocess.Popen(
            [sys.executable, "-m", "gcp_storage_emulator", "start", "--host", "127.0.0.1",
             "--port", str(port), "--in-memory", "--quiet"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.tmp = tempfile.TemporaryDirectory()
        # IR75 only, without via filter: the expected numbers below are for that line
        cls.lines_file = Path(cls.tmp.name) / "lines.yml"
        cls.lines_file.write_text("lines:\n  - linien_text: IR75\n    betreiber_abk: SBB\n")
        cls.env = {
            "STORAGE_EMULATOR_HOST": cls.emulator_host, "GCS_BUCKET": BUCKET,
            "GCP_PROJECT_ID": PROJECT, "TRANSFORM_LINES_FILE": str(cls.lines_file),
            "TRANSFORM_CHUNK_SIZE": "64",
        }
        with mock.patch.dict(os.environ, {"STORAGE_EMULATOR_HOST": cls.emulator_host}):
            cls.gcs = storage.Client(project=PROJECT)
            for _ in range(50):
                try:
                    cls.bucket = cls.gcs.create_bucket(BUCKET)
                    break
                except Exception:
                    time.sleep(0.2)
            else:
                raise RuntimeError("GCS emulator did not start")

    @classmethod
    def tearDownClass(cls):
        cls.emulator.terminate()
        cls.emulator.wait()
        cls.tmp.cleanup()

    # -- helpers -------------------------------------------------------------------
    def setUp(self):
        with mock.patch.dict(os.environ, self.env):
            for blob in list(self.gcs.list_blobs(BUCKET)):
                blob.delete()
            self.put(DAY, FIXTURE.read_bytes())

    def put(self, day: date, data: bytes):
        with mock.patch.dict(os.environ, self.env):
            self.bucket.blob(f"raw/{day.isoformat()}_istdaten.csv").upload_from_string(
                data, content_type="text/csv")

    def cli(self, *args: str) -> int:
        with mock.patch.dict(os.environ, self.env):
            return cli.main(list(args))

    def processed(self, day: date, kind: str):
        """processed/<day>_<kind>.parquet as a list of dicts, or None if missing."""
        with mock.patch.dict(os.environ, self.env):
            blob = self.bucket.get_blob(f"processed/{day.isoformat()}_{kind}.parquet")
            if blob is None:
                return None
            return pq.read_table(io.BytesIO(blob.download_as_bytes())).to_pylist()

    def events(self, day: date = DAY):
        return self.processed(day, "stop_events")

    # -- output of the fixture -------------------------------------------------------
    def test_fixture_output(self):
        self.assertEqual(self.cli("--date", DAY.isoformat()), 0)
        raw, rows = fixture_rows(), self.events()
        self.assertEqual(len(rows), sum(1 for r in raw if r["FAELLT_AUS_TF"] != "true"))
        self.assertEqual(len({r["fahrt_bezeichner"] for r in rows}), 51)
        self.assertEqual(sum(r["arr_delay_s"] is not None for r in rows),
                         sum(1 for r in raw if r["AN_PROGNOSE_STATUS"] == "REAL"))
        self.assertEqual(sum(r["dep_delay_s"] is not None for r in rows),
                         sum(1 for r in raw if r["AB_PROGNOSE_STATUS"] == "REAL"))
        self.assertGreaterEqual(sum(1 for r in rows if (r["arr_delay_s"] or 0) < 0), 90)
        self.assertFalse([r for r in rows if r["stop_sequence"] == 1
                          and r["arr_delay_s"] is not None])
        self.assertFalse([r for r in rows if r["sched_dep"] is None
                          and r["dep_delay_s"] is not None])
        midnight = [(r["arr_delay_s"], r["dep_delay_s"]) for r in rows
                    if r["fahrt_bezeichner"] == "ch:1:sjyid:100001:2592-001"
                    and r["bpuic"] == 8502206]
        self.assertEqual(midnight, [(68, 164)])
        cancelled = {(r["FAHRT_BEZEICHNER"], int(r["BPUIC"])) for r in raw
                     if r["FAELLT_AUS_TF"] == "true"}
        self.assertFalse(cancelled & {(r["fahrt_bezeichner"], r["bpuic"]) for r in rows})
        self.assertEqual({r["betriebstag"] for r in rows}, {DAY})
        self.assertEqual(self.processed(DAY, "run_scope"),
                         [{"betriebstag": DAY, "linien_text": "IR75", "betreiber_abk": "SBB"}])

    def test_rerun_gives_identical_output(self):
        self.assertEqual(self.cli("--date", DAY.isoformat()), 0)
        first = self.events()
        self.assertEqual(self.cli("--date", DAY.isoformat()), 0)
        self.assertEqual(self.events(), first)

    # -- line filter and scope --------------------------------------------------------
    def test_line_filter_on_multi_line_file(self):
        day = date(2026, 9, 23)

        def trip(name, line, operator):
            return [make_row(name, 10, "A", dep="08:00", dep_act="08:00:30", dep_status="REAL",
                             line=line, operator=operator, day="23.09.2026"),
                    make_row(name, 11, "B", arr="08:10", arr_act="08:09:40", arr_status="REAL",
                             line=line, operator=operator, day="23.09.2026")]
        self.put(day, to_csv_bytes(trip("t-ir75", "IR75", "SBB") + trip("t-s1-sbb", "S1", "SBB")
                                   + trip("t-s1-bls", "S1", "BLS") + trip("t-ic5", "IC5", "SBB")))
        self.assertEqual(self.cli("--date", day.isoformat()), 0)              # config: IR75
        self.assertEqual({r["fahrt_bezeichner"] for r in self.events(day)}, {"t-ir75"})
        self.assertEqual(self.cli("--date", day.isoformat(), "--lines", "S1:SBB,IR75:SBB"), 0)
        got = [(r["fahrt_bezeichner"], r["linien_text"], r["betreiber_abk"], r["arr_delay_s"],
                r["dep_delay_s"], r["sched_dep"])
               for r in sorted(self.events(day),
                               key=lambda r: (r["fahrt_bezeichner"], r["stop_sequence"]))]
        self.assertEqual(got, [
            ("t-ir75", "IR75", "SBB", None, 30, "2026-09-23 08:00:00"),
            ("t-ir75", "IR75", "SBB", -20, None, None),
            ("t-s1-sbb", "S1", "SBB", None, 30, "2026-09-23 08:00:00"),
            ("t-s1-sbb", "S1", "SBB", -20, None, None)])
        self.assertEqual([(r["linien_text"], r["betreiber_abk"])
                          for r in self.processed(day, "run_scope")],
                         [("IR75", "SBB"), ("S1", "SBB")])

    def test_line_without_rows_is_still_in_scope(self):
        # S9 has no rows that day: the scope must still list it, so that
        # 10_load_core.sql removes S9 rows a previous run may have loaded
        self.assertEqual(self.cli("--date", DAY.isoformat(), "--lines", "IR75:SBB,S9:SBB"), 0)
        self.assertEqual({r["linien_text"] for r in self.events()}, {"IR75"})
        self.assertEqual([r["linien_text"] for r in self.processed(DAY, "run_scope")],
                         ["IR75", "S9"])

    # -- failures ---------------------------------------------------------------------
    def test_missing_day_fails_but_others_run(self):
        code = self.cli("--start-date", "2026-09-20", "--end-date", "2026-09-21")
        self.assertEqual(code, 1)
        self.assertIsNone(self.events(date(2026, 9, 20)))
        self.assertIsNone(self.processed(date(2026, 9, 20), "run_scope"))
        self.assertEqual(len(self.events()), 445)

    def test_bad_arguments_and_config(self):
        for args in (["--date", "21.09.2026"],
                     ["--start-date", "2026-09-22", "--end-date", "2026-09-21"],
                     ["--date", "2026-09-21", "--retransform"]):          # option removed
            with self.assertRaises(SystemExit) as e:
                self.cli(*args)
            self.assertEqual(e.exception.code, 2, args)
        self.assertEqual(self.cli("--date", "2026-09-21", "--lines", "IR75"), 2)
        with mock.patch.dict(self.env, {"GCS_BUCKET": ""}):
            self.assertEqual(self.cli("--date", "2026-09-21"), 2)
        with mock.patch.dict(self.env, {"GCS_BUCKET": "does-not-exist-bucket"}):
            self.assertEqual(self.cli("--date", "2026-09-21"), 2)

    def test_invalid_raw_file_leaves_previous_output(self):
        self.assertEqual(self.cli("--date", DAY.isoformat()), 0)
        before = (self.events(), self.processed(DAY, "run_scope"))
        self.put(DAY, FIXTURE.read_bytes().replace(b"IR75,,IR,false", b"IR75,,IR,maybe", 1))
        self.assertEqual(self.cli("--date", DAY.isoformat(), "--lines", "IR75:SBB,S1:SBB"), 1)
        self.assertEqual((self.events(), self.processed(DAY, "run_scope")), before)

    def test_storage_error_fails_one_day_and_the_range_continues(self):
        self.put(date(2026, 9, 22), FIXTURE.read_bytes().replace(b"21.09.2026", b"22.09.2026"))
        from google.api_core import exceptions as gexc
        from app import storage as app_storage
        real_write, calls = app_storage.ObjectStore.write, []

        def flaky(store, key, data, content_type):
            calls.append(key)
            if len(calls) == 1:
                raise gexc.ServiceUnavailable("simulated outage")
            return real_write(store, key, data, content_type)
        with mock.patch.object(app_storage.ObjectStore, "write", flaky):
            code = self.cli("--start-date", "2026-09-21", "--end-date", "2026-09-22")
        self.assertEqual(code, 1)
        self.assertIsNone(self.processed(DAY, "run_scope"))      # day 1: nothing complete
        self.assertEqual(len(self.events(date(2026, 9, 22))), 445)
        self.assertIsNotNone(self.processed(date(2026, 9, 22), "run_scope"))


if __name__ == "__main__":
    unittest.main()
