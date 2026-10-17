"""Shared helpers for the database tests (real PostgreSQL 16, no mocks).

Connection (admin database; the tests create and drop their own databases):

    E2E_POSTGRES_HOST=127.0.0.1  E2E_POSTGRES_PORT=5432
    E2E_POSTGRES_USER=...  E2E_POSTGRES_PASSWORD=...  E2E_POSTGRES_DB=deng

Without them the tests are skipped.
"""
from __future__ import annotations

import hashlib
import os
import unittest
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import psycopg
from psycopg import sql

REPO = Path(__file__).resolve().parents[2]
INIT_DIR = REPO / "db" / "init"

E2E = {k: os.environ.get(f"E2E_POSTGRES_{k}") for k in ("HOST", "PORT", "USER", "PASSWORD", "DB")}
MISSING = [f"E2E_POSTGRES_{k}" for k, v in E2E.items() if not v and k != "PASSWORD"]
SKIP_REASON = f"PostgreSQL not configured (missing {', '.join(MISSING)})"

STAGING_COLS = ("betriebstag", "fahrt_bezeichner", "bpuic", "haltestellen_name", "linien_text",
                "betreiber_abk", "stop_sequence", "sched_arr", "sched_dep", "arr_delay_s",
                "dep_delay_s", "is_additional_trip", "row_hash")

REPORT_COLS = ("linien_text", "betreiber_abk", "from_bpuic", "from_stop", "to_bpuic", "to_stop",
               "weekday", "slot_hour", "median_dep_delay_min", "median_arr_delay_min",
               "n_dep_obs", "n_arr_obs")

# Medians as text: '-0.0' vs '0.0' and '1.50' vs '1.5' would compare equal as Decimal.
REPORT_SELECT = """
    SELECT linien_text, betreiber_abk, from_bpuic, from_stop, to_bpuic, to_stop,
           weekday, slot_hour, median_dep_delay_min::text, median_arr_delay_min::text,
           n_dep_obs, n_arr_obs
    FROM reporting.od_delay_by_slot
    ORDER BY linien_text COLLATE "C", betreiber_abk COLLATE "C", from_bpuic, to_bpuic, weekday, slot_hour
"""


def admin_conninfo() -> dict:
    return dict(host=E2E["HOST"], port=int(E2E["PORT"] or 5432), user=E2E["USER"],
                password=E2E["PASSWORD"] or "", dbname=E2E["DB"])


def apply_init_scripts(conninfo: dict) -> None:
    """All db/init/*.sql in sorted order, like docker-entrypoint-initdb.d."""
    with psycopg.connect(**conninfo, autocommit=True) as c:
        for path in sorted(INIT_DIR.glob("*.sql")):
            c.execute(path.read_text(encoding="utf-8"))


@unittest.skipIf(MISSING, SKIP_REASON)
class DatabaseTestCase(unittest.TestCase):
    """One temporary database per test class, built from db/init/*.sql."""

    @classmethod
    def setUpClass(cls):
        cls.admin = admin_conninfo()
        cls.dbname = f"test_reporting_{uuid.uuid4().hex[:10]}"
        with psycopg.connect(**cls.admin, autocommit=True) as c:
            c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(cls.dbname)))
        cls.db = {**cls.admin, "dbname": cls.dbname}
        apply_init_scripts(cls.db)

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(**cls.admin, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(cls.dbname)))

    def setUp(self):
        self.execute("DELETE FROM reporting.od_delay_by_slot")
        self.execute("TRUNCATE staging.stop_events")

    # -- helpers -------------------------------------------------------------------
    def connect(self, **kw) -> psycopg.Connection:
        return psycopg.connect(**self.db, **kw)

    def execute(self, q, params=None):
        with self.connect(autocommit=True) as c:
            c.execute(q, params)

    def query(self, q, params=None) -> list[tuple]:
        with self.connect(autocommit=True) as c:
            return c.execute(q, params).fetchall()

    def refresh(self) -> list[str]:
        """CALL the procedure in autocommit (= its own transaction); returns the NOTICEs."""
        notices: list[str] = []
        with self.connect(autocommit=True) as c:
            c.add_notice_handler(lambda d: notices.append(d.message_primary))
            c.execute("CALL reporting.refresh_od_delay()")
        return notices

    def report(self) -> list[tuple]:
        return self.query(REPORT_SELECT)

    def load(self, rows: list[tuple]) -> None:
        with self.connect() as c, c.cursor() as cur:
            with cur.copy(sql.SQL("COPY staging.stop_events ({}) FROM STDIN").format(
                    sql.SQL(", ").join(map(sql.Identifier, STAGING_COLS)))) as cp:
                for r in rows:
                    cp.write_row(r)


# -- building staging rows ------------------------------------------------------------
@dataclass(frozen=True)
class Stop:
    bpuic: int
    name: str | None
    sched_arr: datetime | None
    sched_dep: datetime | None
    arr_delay_s: int | None
    dep_delay_s: int | None


def trip_rows(day: date, trip_id: str, stops: list[Stop], line: str = "L1",
              operator: str = "SBB") -> list[tuple]:
    """Staging rows of one trip; stop_sequence = list position (1-based)."""
    rows = []
    for seq, s in enumerate(stops, start=1):
        key = f"{day}|{trip_id}|{s.bpuic}|{seq}"
        rows.append((day, trip_id, s.bpuic, s.name, line, operator, seq, s.sched_arr, s.sched_dep,
                     s.arr_delay_s, s.dep_delay_s, False, hashlib.sha256(key.encode()).hexdigest()))
    return rows