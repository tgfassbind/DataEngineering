"""End to end: real object storage (S3 API) + real PostgreSQL, real CLI.

Needs the two services, e.g. from this repo:

    docker compose up -d object-storage database

and these variables (values from your .env):

    E2E_S3_ENDPOINT=http://127.0.0.1:8333
    E2E_S3_ACCESS_KEY_ID=...   E2E_S3_SECRET_ACCESS_KEY=...
    E2E_POSTGRES_HOST=127.0.0.1  E2E_POSTGRES_PORT=5432
    E2E_POSTGRES_USER=...      E2E_POSTGRES_PASSWORD=...  E2E_POSTGRES_DB=deng

Without them the tests are skipped. Isolation: the tests create their own
bucket and their own database (from db/init/01_schemas.sql) and remove both
afterwards, so they never touch the pipeline's real data.

Expected values come from the fixture via the csv module, not from app code.
"""
from __future__ import annotations

import csv
import io
import os
import unittest
import uuid
from datetime import date
from unittest import mock

import boto3
import psycopg
from botocore.config import Config as BotoConfig
from psycopg import sql

from app import cli
from app.config import Config, Line
from app.pipeline import transform_day
from app.read import make_s3_client
from tests.helpers import FIXTURE, REPO, fixture_rows, make_row, to_csv_bytes

E2E = {k: os.environ.get(f"E2E_{k}") for k in (
    "S3_ENDPOINT", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY",
    "POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_DB")}
MISSING = [k for k, v in E2E.items() if not v and k != "POSTGRES_PASSWORD"]
DAY = date(2026, 9, 21)
IR75 = [Line("IR75", "SBB")]


@unittest.skipIf(MISSING, f"E2E services not configured (missing E2E_{', E2E_'.join(MISSING)})")
class EndToEnd(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        suffix = uuid.uuid4().hex[:10]
        cls.bucket = f"e2e-transform-{suffix}"
        cls.dbname = f"e2e_transform_{suffix}"
        cls.s3 = boto3.client(
            "s3", endpoint_url=E2E["S3_ENDPOINT"], region_name="us-east-1",
            aws_access_key_id=E2E["S3_ACCESS_KEY_ID"],
            aws_secret_access_key=E2E["S3_SECRET_ACCESS_KEY"],
            config=BotoConfig(s3={"addressing_style": "path"}))
        cls.s3.create_bucket(Bucket=cls.bucket)
        admin = dict(host=E2E["POSTGRES_HOST"], port=int(E2E["POSTGRES_PORT"]),
                     user=E2E["POSTGRES_USER"], password=E2E["POSTGRES_PASSWORD"] or "",
                     dbname=E2E["POSTGRES_DB"])
        with psycopg.connect(**admin, autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.dbname)))
        cls.db = {**admin, "dbname": cls.dbname}
        with psycopg.connect(**cls.db, autocommit=True) as c:
            c.execute((REPO / "db" / "init" / "01_schemas.sql").read_text())
        cls.env = {
            "S3_ENDPOINT_URL": E2E["S3_ENDPOINT"], "S3_ACCESS_KEY_ID": E2E["S3_ACCESS_KEY_ID"],
            "S3_SECRET_ACCESS_KEY": E2E["S3_SECRET_ACCESS_KEY"], "S3_BUCKET": cls.bucket,
            "S3_REGION": "us-east-1", "POSTGRES_HOST": E2E["POSTGRES_HOST"],
            "POSTGRES_PORT": E2E["POSTGRES_PORT"], "POSTGRES_DB": cls.dbname,
            "POSTGRES_USER": E2E["POSTGRES_USER"],
            "POSTGRES_PASSWORD": E2E["POSTGRES_PASSWORD"] or "unused",
            "TRANSFORM_LINES_FILE": "", "TRANSFORM_CHUNK_SIZE": "64", "S3_BUCKET_WAIT_SECONDS": "0",
        }
        cls.admin = admin

    @classmethod
    def tearDownClass(cls):
        objs = cls.s3.list_objects_v2(Bucket=cls.bucket).get("Contents", [])
        for o in objs:
            cls.s3.delete_object(Bucket=cls.bucket, Key=o["Key"])
        cls.s3.delete_bucket(Bucket=cls.bucket)
        with psycopg.connect(**cls.admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(cls.dbname)))

    # -- helpers -------------------------------------------------------------------
    def setUp(self):
        with psycopg.connect(**self.db, autocommit=True) as c:
            c.execute("TRUNCATE staging.stop_events")
        self.put(DAY, FIXTURE.read_bytes())

    def put(self, day: date, data: bytes):
        self.s3.put_object(Bucket=self.bucket, Key=f"raw/{day.isoformat()}_istdaten.csv",
                           Body=data, ContentType="text/csv")

    def cli(self, *args: str) -> int:
        with mock.patch.dict(os.environ, self.env):
            return cli.main(list(args))

    def transform(self, day=DAY, lines=IR75, retransform=False):
        with mock.patch.dict(os.environ, self.env):
            cfg = Config.from_env()
        with psycopg.connect(**cfg.pg_conninfo(), autocommit=True) as conn:
            return transform_day(cfg, make_s3_client(cfg), conn, day, lines, retransform)

    def query(self, q: str, *params):
        with psycopg.connect(**self.db, autocommit=True) as c:
            return c.execute(q, params).fetchall()

    def snapshot(self):
        return self.query("SELECT * FROM staging.stop_events ORDER BY 1, 2, 3")

    # -- acceptance criteria in PostgreSQL --------------------------------------------
    def test_fixture_in_staging(self):
        self.assertEqual(self.cli("--date", DAY.isoformat()), 0)
        raw = fixture_rows()
        (n, trips, arr, dep, neg, zero_first, zero_last), = self.query("""
            SELECT count(*), count(DISTINCT fahrt_bezeichner),
                   count(arr_delay_s), count(dep_delay_s),
                   count(*) FILTER (WHERE arr_delay_s < 0),
                   count(*) FILTER (WHERE stop_sequence = 1 AND arr_delay_s IS NOT NULL),
                   count(*) FILTER (WHERE sched_dep IS NULL AND dep_delay_s IS NOT NULL)
            FROM staging.stop_events""")
        self.assertEqual(n, sum(1 for r in raw if r["FAELLT_AUS_TF"] != "true"))
        self.assertEqual(trips, 51)
        self.assertEqual(arr, sum(1 for r in raw if r["AN_PROGNOSE_STATUS"] == "REAL"))
        self.assertEqual(dep, sum(1 for r in raw if r["AB_PROGNOSE_STATUS"] == "REAL"))
        self.assertGreaterEqual(neg, 90)
        self.assertEqual((zero_first, zero_last), (0, 0))
        midnight = self.query("""SELECT arr_delay_s, dep_delay_s FROM staging.stop_events
                                 WHERE fahrt_bezeichner = 'ch:1:sjyid:100001:2592-001'
                                   AND bpuic = 8502206""")
        self.assertEqual(midnight, [(68, 164)])
        cancelled = {(r["FAHRT_BEZEICHNER"], int(r["BPUIC"])) for r in raw
                     if r["FAELLT_AUS_TF"] == "true"}
        stored = set(self.query("SELECT fahrt_bezeichner, bpuic FROM staging.stop_events"))
        self.assertFalse(cancelled & stored)

    # -- idempotency / hash upsert -------------------------------------------------------
    def test_second_run_changes_nothing(self):
        first = self.transform()
        self.assertEqual((first.load.inserted, first.load.updated, first.load.unchanged),
                         (445, 0, 0))
        before = self.snapshot()
        second = self.transform()
        self.assertEqual((second.load.inserted, second.load.updated, second.load.unchanged),
                         (0, 0, 445))
        self.assertEqual(self.snapshot(), before)          # incl. loaded_at: untouched

    def test_one_changed_value_gives_one_update(self):
        self.transform()
        before = {r[:3]: r for r in self.snapshot()}
        text = FIXTURE.read_text(encoding="utf-8")
        rows = list(csv.DictReader(io.StringIO(text)))
        target = next(r for r in rows if r["AN_PROGNOSE_STATUS"] == "REAL")
        target["AN_PROGNOSE"] = target["AN_PROGNOSE"][:-2] + (
            "00" if target["AN_PROGNOSE"][-2:] != "00" else "01")
        out = io.StringIO()
        w = csv.DictWriter(out, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
        self.put(DAY, out.getvalue().encode("utf-8"))
        result = self.transform()
        self.assertEqual((result.load.inserted, result.load.updated, result.load.unchanged),
                         (0, 1, 444))
        after = {r[:3]: r for r in self.snapshot()}
        changed = [k for k in after if after[k] != before[k]]   # loaded_at included
        key = (DAY, target["FAHRT_BEZEICHNER"], int(target["BPUIC"]))
        self.assertEqual(changed, [key])
        self.assertGreater(after[key][-1], before[key][-1])      # loaded_at moved

    def test_retransform_replaces_the_day_only(self):
        other_day = date(2026, 9, 22)
        self.put(other_day, to_csv_bytes([
            make_row("X", 1, "a", dep="10:00", dep_act="10:00:10", dep_status="REAL",
                     day="22.09.2026"),
            make_row("X", 2, "b", arr="10:10", arr_act="10:10:10", arr_status="REAL",
                     day="22.09.2026")]))
        self.transform()
        self.transform(day=other_day)
        # a stale row of the day that a fresh transform would not produce
        self.query("""INSERT INTO staging.stop_events
                      SELECT betriebstag, 'stale-trip', bpuic, haltestellen_name, linien_text,
                             betreiber_abk, stop_sequence, sched_arr, sched_dep, arr_delay_s,
                             dep_delay_s, is_additional_trip, row_hash, loaded_at
                      FROM staging.stop_events WHERE betriebstag = %s LIMIT 1
                      RETURNING 1""", DAY)
        self.assertEqual(self.cli("--date", DAY.isoformat(), "--retransform"), 0)
        rows = self.query("SELECT betriebstag, count(*) FROM staging.stop_events "
                          "GROUP BY 1 ORDER BY 1")
        self.assertEqual(rows, [(DAY, 445), (other_day, 2)])
        self.assertEqual(self.query("SELECT count(*) FROM staging.stop_events "
                                    "WHERE fahrt_bezeichner = 'stale-trip'"), [(0,)])
        result = self.transform(retransform=True)
        self.assertEqual((result.load.deleted, result.load.inserted), (445, 445))

    def test_retransform_only_touches_processed_lines(self):
        day = date(2026, 9, 24)

        def trip(name, line):
            return [make_row(name, 10, "A", dep="08:00", dep_act="08:00:30", dep_status="REAL",
                             line=line, day="24.09.2026"),
                    make_row(name, 11, "B", arr="08:10", arr_act="08:10:30", arr_status="REAL",
                             line=line, day="24.09.2026")]
        self.put(day, to_csv_bytes(trip("t-ir75", "IR75") + trip("t-s1", "S1")))
        self.assertEqual(self.cli("--date", day.isoformat(), "--lines", "IR75:SBB,S1:SBB"), 0)
        self.query("""INSERT INTO staging.stop_events
                      SELECT betriebstag, 'stale-s1', bpuic, haltestellen_name, linien_text,
                             betreiber_abk, stop_sequence, sched_arr, sched_dep, arr_delay_s,
                             dep_delay_s, is_additional_trip, row_hash, loaded_at
                      FROM staging.stop_events WHERE fahrt_bezeichner = 't-s1' LIMIT 1
                      RETURNING 1""")
        ir75_before = self.query("SELECT * FROM staging.stop_events "
                                 "WHERE linien_text = 'IR75' ORDER BY 1, 2, 3")
        s1_loaded_before = self.query("SELECT max(loaded_at) FROM staging.stop_events "
                                      "WHERE fahrt_bezeichner = 't-s1'")[0][0]
        result = self.transform(day=day, lines=[Line("S1", "SBB")], retransform=True)
        self.assertEqual((result.load.deleted, result.load.inserted), (3, 2))
        self.assertEqual(self.query("SELECT DISTINCT fahrt_bezeichner FROM staging.stop_events "
                                    "WHERE linien_text = 'S1'"), [("t-s1",)])
        self.assertGreater(self.query("SELECT min(loaded_at) FROM staging.stop_events "
                                      "WHERE fahrt_bezeichner = 't-s1'")[0][0], s1_loaded_before)
        self.assertEqual(self.query("SELECT * FROM staging.stop_events "
                                    "WHERE linien_text = 'IR75' ORDER BY 1, 2, 3"), ir75_before)

    def test_stale_rows_are_counted_not_deleted(self):
        self.transform()
        self.query("""UPDATE staging.stop_events SET fahrt_bezeichner = 'ghost'
                      WHERE (fahrt_bezeichner, bpuic) IN (SELECT fahrt_bezeichner, bpuic
                      FROM staging.stop_events ORDER BY 1, 2 LIMIT 1) RETURNING 1""")
        result = self.transform()
        self.assertEqual((result.load.stale, result.load.inserted, result.load.unchanged),
                         (1, 1, 444))
        self.assertEqual(self.query("SELECT count(*) FROM staging.stop_events"), [(446,)])

    # -- line filter --------------------------------------------------------------------
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
        self.assertEqual(self.query("SELECT DISTINCT fahrt_bezeichner FROM staging.stop_events "
                                    "WHERE betriebstag = %s", day), [("t-ir75",)])
        self.assertEqual(self.cli("--date", day.isoformat(), "--lines", "S1:SBB,IR75:SBB"), 0)
        got = self.query("SELECT fahrt_bezeichner, linien_text, betreiber_abk, arr_delay_s, "
                         "dep_delay_s FROM staging.stop_events WHERE betriebstag = %s "
                         "ORDER BY 1, stop_sequence", day)
        self.assertEqual(got, [("t-ir75", "IR75", "SBB", None, 30), ("t-ir75", "IR75", "SBB", -20, None),
                               ("t-s1-sbb", "S1", "SBB", None, 30), ("t-s1-sbb", "S1", "SBB", -20, None)])

    # -- failures ---------------------------------------------------------------------
    def test_missing_day_fails_but_others_run(self):
        code = self.cli("--start-date", "2026-09-20", "--end-date", "2026-09-21")
        self.assertEqual(code, 1)
        self.assertEqual(self.query("SELECT count(*) FROM staging.stop_events"), [(445,)])

    def test_bad_arguments_and_config(self):
        with self.assertRaises(SystemExit) as e:
            self.cli("--date", "21.09.2026")
        self.assertEqual(e.exception.code, 2)
        with self.assertRaises(SystemExit) as e:
            self.cli("--start-date", "2026-09-22", "--end-date", "2026-09-21")
        self.assertEqual(e.exception.code, 2)
        self.assertEqual(self.cli("--date", "2026-09-21", "--lines", "IR75"), 2)
        with mock.patch.dict(os.environ, {"S3_BUCKET": "does-not-exist-bucket"}):
            env = {**self.env, "S3_BUCKET": "does-not-exist-bucket"}
            with mock.patch.dict(os.environ, env):
                self.assertEqual(cli.main(["--date", "2026-09-21"]), 2)

    def test_invalid_raw_file_leaves_day_untouched(self):
        self.transform()
        before = self.snapshot()
        self.put(DAY, FIXTURE.read_bytes().replace(b"IR75,,IR,false", b"IR75,,IR,maybe", 1))
        self.assertEqual(self.cli("--date", DAY.isoformat(), "--retransform"), 1)
        self.assertEqual(self.snapshot(), before)

    def test_database_error_fails_one_day_and_the_range_continues(self):
        self.put(date(2026, 9, 22), FIXTURE.read_bytes().replace(b"21.09.2026", b"22.09.2026"))
        real_load = __import__("app.load", fromlist=["load_day"]).load_day
        calls = []

        def flaky(conn, day, *a, **kw):
            calls.append(day)
            if len(calls) == 1:
                raise psycopg.errors.DeadlockDetected("simulated deadlock")
            return real_load(conn, day, *a, **kw)
        with mock.patch("app.pipeline.load_day", flaky):
            code = self.cli("--start-date", "2026-09-21", "--end-date", "2026-09-22")
        self.assertEqual(code, 1)
        self.assertEqual(self.query("SELECT betriebstag, count(*) FROM staging.stop_events "
                                    "GROUP BY 1"), [(date(2026, 9, 22), 445)])

    def test_error_after_delete_rolls_back_whole_day(self):
        """--retransform deletes first; if the upsert then fails, the delete
        must be rolled back too (one transaction per day)."""
        self.transform()
        before = self.snapshot()
        with mock.patch("app.load.UPSERT", "SELECT 1/0"):
            with self.assertRaises(psycopg.errors.DivisionByZero):
                self.transform(retransform=True)
        self.assertEqual(self.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
