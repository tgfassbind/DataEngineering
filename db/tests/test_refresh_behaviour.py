"""Procedure behaviour: idempotency, atomicity, locking, settings, schema objects."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

import psycopg
from psycopg import errors

from tests import scenarios
from tests.helpers import INIT_DIR, REPORT_COLS, DatabaseTestCase, Stop, trip_rows
from tests.scenarios import at

FULL_SNAPSHOT = "SELECT * FROM reporting.od_delay_by_slot ORDER BY 1, 2, 3, 5, 7, 8"


class Behaviour(DatabaseTestCase):

    def setUp(self):
        super().setUp()
        self.load(scenarios.build())
        self.refresh()
        self.before = self.query(FULL_SNAPSHOT)
        self.assertEqual(len(self.before), 16)

    def poison(self):
        """A delay that overflows numeric(6,1): the INSERT fails after the DELETE ran."""
        d = scenarios.MON[0]
        self.load(trip_rows(d, "poison", [
            Stop(1001, "Alpha", None, at(d, "06:00"), None, 2_000_000_000),
            Stop(1002, "Beta", at(d, "06:10"), None, 0, None)]))

    # -- idempotency -----------------------------------------------------------------
    def test_second_call_identical(self):
        notices = self.refresh()
        self.assertEqual(self.query(FULL_SNAPSHOT), self.before)
        self.assertIn("rows_deleted=16 rows_written=16", notices[0])

    def test_follows_staging_changes(self):
        # rows whose source is gone disappear; nothing is left over from the old content
        self.execute("DELETE FROM staging.stop_events WHERE linien_text = 'L3'")
        self.refresh()
        got = self.query(FULL_SNAPSHOT)
        self.assertEqual(got, [r for r in self.before if r[0] != "L3"])
        self.execute("TRUNCATE staging.stop_events")
        self.refresh()
        self.assertEqual(self.query(FULL_SNAPSHOT), [])

    # -- atomicity -------------------------------------------------------------------
    def test_failure_keeps_old_content(self):
        self.poison()
        with self.assertRaises(errors.NumericValueOutOfRange):
            self.refresh()
        self.assertEqual(self.query(FULL_SNAPSHOT), self.before)

    def test_no_commit_inside(self):
        # CALL inside the caller's transaction: rolled back together with it
        self.execute("TRUNCATE staging.stop_events")
        with self.connect() as c:          # psycopg: implicit BEGIN
            c.execute("CALL reporting.refresh_od_delay()")
            self.assertEqual(c.execute("SELECT count(*) FROM reporting.od_delay_by_slot").fetchone(), (0,))
            c.rollback()
        self.assertEqual(self.query(FULL_SNAPSHOT), self.before)

    def test_readers_see_old_content_until_commit(self):
        self.execute("TRUNCATE staging.stop_events")
        with self.connect() as writer:
            writer.execute("CALL reporting.refresh_od_delay()")    # not committed yet
            # a reader is not blocked and still sees the old rows
            with self.connect(autocommit=True) as reader:
                reader.execute("SET lock_timeout = '2s'")
                self.assertEqual(reader.execute("SELECT count(*) FROM reporting.od_delay_by_slot").fetchone(), (16,))
            writer.commit()
        self.assertEqual(self.query(FULL_SNAPSHOT), [])

    # -- locking ---------------------------------------------------------------------
    def test_concurrent_call_fails_after_lock_wait(self):
        with self.connect() as first:
            first.execute("CALL reporting.refresh_od_delay()")     # holds the lock until commit
            with self.connect(autocommit=True) as second:
                t0 = time.monotonic()
                with self.assertRaises(errors.LockNotAvailable) as cm:
                    second.execute("CALL reporting.refresh_od_delay(interval '1 second')")
                waited = time.monotonic() - t0
                self.assertIn("another refresh is still running", str(cm.exception))
                self.assertGreaterEqual(waited, 0.9)
                self.assertLess(waited, 10)
            first.rollback()
        self.assertEqual(self.query(FULL_SNAPSHOT), self.before)

    def test_concurrent_call_waits_and_then_runs(self):
        result = {}

        def second_call():
            try:
                with self.connect(autocommit=True) as c:
                    result["pid"] = c.info.backend_pid
                    c.execute("CALL reporting.refresh_od_delay(interval '30 seconds')")
                    result["count"] = c.execute("SELECT count(*) FROM reporting.od_delay_by_slot").fetchone()[0]
                    result["done"] = time.monotonic()
            except Exception as e:      # surfaced in the main thread
                result["error"] = e

        with self.connect() as first:
            first.execute("TRUNCATE staging.stop_events")
            first.execute("CALL reporting.refresh_od_delay()")
            th = threading.Thread(target=second_call)
            th.start()
            # the second CALL must be waiting on the ADVISORY lock (not on row locks of the DELETE)
            deadline = time.monotonic() + 10
            waiting = []
            while time.monotonic() < deadline and not waiting:
                if "pid" in result:
                    waiting = self.query("""SELECT 1 FROM pg_locks WHERE locktype = 'advisory'
                                            AND NOT granted AND pid = %s""", (result["pid"],))
                time.sleep(0.05)
            self.assertTrue(waiting, "second CALL is not waiting on the advisory lock")
            self.assertNotIn("done", result)
            released = time.monotonic()
            first.commit()
        th.join(30)
        self.assertFalse(th.is_alive())
        self.assertNotIn("error", result)
        self.assertGreaterEqual(result["done"], released)
        self.assertEqual(result["count"], 0)            # recomputed from the committed staging

    def test_invalid_lock_wait(self):
        with self.assertRaises(errors.InvalidParameterValue):
            self.execute("CALL reporting.refresh_od_delay(interval '-1 second')")
        with self.assertRaises(errors.InvalidParameterValue):
            self.execute("CALL reporting.refresh_od_delay(NULL)")
        with self.assertRaises(errors.InvalidParameterValue):
            self.execute("CALL reporting.refresh_od_delay(interval '30 days')")
        self.execute("CALL reporting.refresh_od_delay(interval '24 days')")   # upper bound accepted

    def test_session_settings_untouched(self):
        with self.connect() as c:
            c.execute("SET lock_timeout = '7s'")
            c.execute("SET search_path = staging, public")
            c.execute("CALL reporting.refresh_od_delay(interval '2 seconds')")
            self.assertEqual(c.execute("SHOW lock_timeout").fetchone(), ("7s",))
            self.assertEqual(c.execute("SHOW search_path").fetchone(), ("staging, public",))
            c.commit()

    # -- schema objects ----------------------------------------------------------------
    def test_constraints(self):
        base = dict(linien_text="X", betreiber_abk="SBB", from_bpuic=1, from_stop="a", to_bpuic=2,
                    to_stop="b", weekday=1, slot_hour=0, median_dep_delay_min=1.0,
                    median_arr_delay_min=1.0, n_dep_obs=1, n_arr_obs=1)
        bad = [dict(weekday=0), dict(weekday=8), dict(slot_hour=24), dict(slot_hour=-1),
               dict(n_dep_obs=-1), dict(n_dep_obs=0, n_arr_obs=0,
                                        median_dep_delay_min=None, median_arr_delay_min=None),
               dict(median_dep_delay_min=None), dict(n_arr_obs=0), dict(linien_text=None)]
        cols = ", ".join(REPORT_COLS)
        ph = ", ".join(["%s"] * len(REPORT_COLS))
        for change in bad:
            with self.subTest(change=change), self.assertRaises(
                    (errors.CheckViolation, errors.NotNullViolation)):
                row = {**base, **change}
                self.execute(f"INSERT INTO reporting.od_delay_by_slot ({cols}) VALUES ({ph})",
                             [row[c] for c in REPORT_COLS])
        # the valid base row itself is accepted
        self.execute("DELETE FROM reporting.od_delay_by_slot")
        self.execute(f"INSERT INTO reporting.od_delay_by_slot ({cols}) VALUES ({ph})",
                     [base[c] for c in REPORT_COLS])

    def test_comments_and_index(self):
        missing = self.query("""
            SELECT a.attname FROM pg_attribute a
            WHERE a.attrelid = 'reporting.od_delay_by_slot'::regclass AND a.attnum > 0
              AND NOT a.attisdropped AND col_description(a.attrelid, a.attnum) IS NULL""")
        self.assertEqual(missing, [])
        self.assertIsNotNone(self.query("SELECT obj_description('reporting.od_delay_by_slot'::regclass)")[0][0])
        idx = self.query("""SELECT indexdef FROM pg_indexes
                            WHERE schemaname = 'reporting' AND indexname = 'od_delay_by_slot_lookup_idx'""")
        self.assertEqual(len(idx), 1)
        self.assertIn("(linien_text, betreiber_abk, from_stop, to_stop, weekday, slot_hour)", idx[0][0])

    def test_init_scripts_via_psql_like_the_image(self):
        # docker-entrypoint-initdb.d runs: psql -v ON_ERROR_STOP=1 --no-psqlrc -f <file>
        psql = shutil.which("psql")
        if not psql:
            self.skipTest("psql not on PATH")
        name = f"{self.dbname}_psql"
        with psycopg.connect(**self.admin, autocommit=True) as c:
            c.execute(f'CREATE DATABASE "{name}"')
        try:
            env = {**os.environ, "PGHOST": self.db["host"], "PGPORT": str(self.db["port"]),
                   "PGUSER": self.db["user"], "PGPASSWORD": self.db["password"], "PGDATABASE": name}
            for _ in range(2):      # second pass = re-run on an existing database
                for path in sorted(INIT_DIR.glob("*.sql")):
                    subprocess.run([psql, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", str(path)],
                                   env=env, check=True, capture_output=True)
            out = subprocess.run([psql, "-X", "-v", "ON_ERROR_STOP=1", "-c",
                                  "CALL reporting.refresh_od_delay();"],
                                 env=env, check=True, capture_output=True, text=True)
            self.assertIn("rows_written=0", out.stderr)
        finally:
            with psycopg.connect(**self.admin, autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    def test_old_signature_replaced(self):
        # a leftover no-argument version must not make "CALL ...()" ambiguous after re-applying 03
        self.execute("""CREATE PROCEDURE reporting.refresh_od_delay() LANGUAGE sql AS $$ SELECT 1 $$""")
        from tests.helpers import apply_init_scripts
        apply_init_scripts(self.db)
        self.refresh()
        self.assertEqual(self.query(FULL_SNAPSHOT), self.before)

    def test_init_scripts_rerunnable(self):
        # applying db/init again (e.g. by hand with psql) must not fail or change data
        from tests.helpers import apply_init_scripts
        apply_init_scripts(self.db)
        self.assertEqual(self.query(FULL_SNAPSHOT), self.before)


class Collation(DatabaseTestCase):
    """Stop names use byte order whatever the database collation is (the image default is en_US.utf8)."""

    def test_icu_database_same_result(self):
        name = f"{self.dbname}_icu"
        with psycopg.connect(**self.admin, autocommit=True) as c:
            try:
                c.execute(f'CREATE DATABASE "{name}" TEMPLATE template0 ENCODING UTF8 '
                          f"LOCALE_PROVIDER icu ICU_LOCALE 'en-US' LOCALE 'C.UTF-8'")
            except psycopg.Error as e:
                self.skipTest(f"ICU not available: {e}")
        try:
            from tests.helpers import REPORT_SELECT, apply_init_scripts
            db = {**self.db, "dbname": name}
            apply_init_scripts(db)
            self.load(scenarios.build())
            self.refresh()
            here = self.report()
            orig = self.db
            try:
                type(self).db = db
                self.load(scenarios.build())
                self.refresh()
                there = self.query(REPORT_SELECT)
            finally:
                type(self).db = orig
            self.assertEqual(here, there)
            self.assertIn("Alpha", {r[3] for r in there})   # not 'alpha' (en-US would sort it first)
        finally:
            with psycopg.connect(**self.admin, autocommit=True) as c:
                c.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')