"""One day, end to end: read -> validate -> compute -> load -> one summary log line."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

import psycopg

from .compute import build_rows
from .config import Config, Line
from .load import LoadResult, load_day
from .read import read_day
from .validate import ROW_REASONS, TIMESTAMP_REASONS, ValidationStats, validate

log = logging.getLogger(__name__)


@dataclass
class DaySummary:
    day: date
    rows_read: int = 0
    rows_after_filter: int = 0
    rows_staged: int = 0
    trips_excluded_by_via: int = 0
    validation: ValidationStats = field(default_factory=ValidationStats)
    load: LoadResult = field(default_factory=LoadResult)
    seconds: float = 0.0

    def log_line(self) -> str:
        """Multi-line summary of one day, one group of counters per line."""
        v = self.validation

        def kv(d: dict) -> str:
            return "  ".join(f"{k}={n}" for k, n in d.items())

        return "\n".join([
            f"  read        rows_read={self.rows_read}  rows_after_filter={self.rows_after_filter}"
            f"  trips_excluded_by_via={self.trips_excluded_by_via}  rows_staged={self.rows_staged}",
            f"  rows        {kv({r: v.rows_dropped[r] for r in ROW_REASONS})}",
            f"  arrival     {kv({r: v.timestamps_rejected['arr'][r] for r in TIMESTAMP_REASONS})}",
            f"  departure   {kv({r: v.timestamps_rejected['dep'][r] for r in TIMESTAMP_REASONS})}",
            f"  n/a         arrival={v.not_applicable['arr']}  departure={v.not_applicable['dep']}",
            f"  load        deleted={self.load.deleted}  stale={self.load.stale}"
            f"  inserted={self.load.inserted}  updated={self.load.updated}"
            f"  unchanged={self.load.unchanged}",
            f"  duration    {self.seconds:.1f}s",
        ])


def transform_day(cfg: Config, s3, conn: psycopg.Connection, day: date,
                  lines: list[Line], retransform: bool) -> DaySummary:
    started = time.monotonic()
    summary = DaySummary(day)
    read = read_day(s3, cfg.s3_bucket, day, lines, cfg.chunk_size)
    summary.rows_read, summary.rows_after_filter = read.rows_read, read.rows_after_filter
    summary.trips_excluded_by_via = read.trips_excluded_by_via
    if read.rows_after_filter == 0:
        log.warning("date=%s no rows for lines %s in %d rows read",
                    day, ", ".join(map(str, lines)), read.rows_read)
    validated, summary.validation = validate(read.frame, cfg.limits)
    del read                                   # free the raw rows before loading
    rows = build_rows(validated, day)
    summary.rows_staged = len(rows)
    summary.load = load_day(conn, day, rows, lines, retransform)
    if summary.load.stale:
        log.warning("date=%s %d staging rows of these lines are no longer in the raw "
                    "result and were kept; run with --retransform to remove them",
                    day, summary.load.stale)
    summary.seconds = time.monotonic() - started
    return summary
