"""One day, end to end: read -> validate -> compute -> write -> one summary log line."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date

from .compute import build_rows
from .config import Config, Line
from .output import WriteResult, write_day
from .read import read_day
from .storage import ObjectStore
from .validate import ROW_REASONS, TIMESTAMP_REASONS, ValidationStats, validate

log = logging.getLogger(__name__)


@dataclass
class DaySummary:
    day: date
    rows_read: int = 0
    rows_after_filter: int = 0
    rows_written: int = 0
    trips_excluded_by_via: int = 0
    validation: ValidationStats = field(default_factory=ValidationStats)
    output: WriteResult = field(default_factory=WriteResult)
    seconds: float = 0.0

    def log_line(self) -> str:
        """Multi-line summary of one day, one group of counters per line."""
        v = self.validation

        def kv(d: dict) -> str:
            return "  ".join(f"{k}={n}" for k, n in d.items())

        return "\n".join([
            f"  read        rows_read={self.rows_read}  rows_after_filter={self.rows_after_filter}"
            f"  trips_excluded_by_via={self.trips_excluded_by_via}  rows_written={self.rows_written}",
            f"  rows        {kv({r: v.rows_dropped[r] for r in ROW_REASONS})}",
            f"  arrival     {kv({r: v.timestamps_rejected['arr'][r] for r in TIMESTAMP_REASONS})}",
            f"  departure   {kv({r: v.timestamps_rejected['dep'][r] for r in TIMESTAMP_REASONS})}",
            f"  n/a         arrival={v.not_applicable['arr']}  departure={v.not_applicable['dep']}",
            f"  output      {self.output.stop_events_key} ({self.output.rows} rows)"
            f"  {self.output.run_scope_key} ({self.output.scopes} lines)",
            f"  duration    {self.seconds:.1f}s",
        ])


def transform_day(cfg: Config, store: ObjectStore, day: date, lines: list[Line]) -> DaySummary:
    started = time.monotonic()
    summary = DaySummary(day)
    read = read_day(store, day, lines, cfg.chunk_size)
    summary.rows_read, summary.rows_after_filter = read.rows_read, read.rows_after_filter
    summary.trips_excluded_by_via = read.trips_excluded_by_via
    if read.rows_after_filter == 0:
        log.warning("date=%s no rows for lines %s in %d rows read",
                    day, ", ".join(map(str, lines)), read.rows_read)
    validated, summary.validation = validate(read.frame, cfg.limits)
    del read                                   # free the raw rows before writing
    rows = build_rows(validated, day)
    summary.rows_written = len(rows)
    summary.output = write_day(store, day, rows, lines)
    summary.seconds = time.monotonic() - started
    return summary
