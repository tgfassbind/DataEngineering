"""Write the result of one day to the data lake (processed/), as Parquet.

Two files per day, loaded into BigQuery by Kestra:

  processed/<day>_stop_events.parquet  -> core.stg_stop_event
      one row per served stop (columns: compute.OUTPUT_COLUMNS)
  processed/<day>_run_scope.parquet    -> core.stg_run_scope
      one row per processed line, also for a line without any row that day;
      bigquery/sql/10_load_core.sql replaces exactly these (day, line) pairs

Parquet keeps the column types (date, integers with NULL, boolean), so the
BigQuery load needs no type guessing. The planned times are written as text
'YYYY-MM-DD HH:MM:SS' (Swiss local time, no zone) and cast to DATETIME in
10_load_core.sql: a Parquet timestamp is loaded by BigQuery as TIMESTAMP,
i.e. as UTC, which would silently shift local times.

Each file is replaced in one step. The scope file is written last: if writing
the rows fails, the run fails (exit code 1) and Kestra loads nothing.
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .compute import OUTPUT_COLUMNS
from .config import Line, run_scope_key, stop_events_key
from .storage import ObjectStore

PARQUET = "application/vnd.apache.parquet"
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

STOP_EVENTS_SCHEMA = pa.schema([
    ("betriebstag", pa.date32()),
    ("fahrt_bezeichner", pa.string()),
    ("bpuic", pa.int64()),
    ("haltestellen_name", pa.string()),
    ("linien_text", pa.string()),
    ("betreiber_abk", pa.string()),
    ("betreiber_name", pa.string()),
    ("verkehrsmittel_text", pa.string()),
    ("produkt_id", pa.string()),
    ("stop_sequence", pa.int64()),
    ("sched_arr", pa.string()),
    ("sched_dep", pa.string()),
    ("arr_delay_s", pa.int64()),
    ("dep_delay_s", pa.int64()),
    ("is_additional_trip", pa.bool_()),
])
assert STOP_EVENTS_SCHEMA.names == OUTPUT_COLUMNS

RUN_SCOPE_SCHEMA = pa.schema([
    ("betriebstag", pa.date32()),
    ("linien_text", pa.string()),
    ("betreiber_abk", pa.string()),
])


@dataclass
class WriteResult:
    rows: int = 0
    scopes: int = 0
    stop_events_key: str = ""
    run_scope_key: str = ""


def _datetime_text(s: pd.Series) -> pd.Series:
    return s.dt.strftime(DATETIME_FORMAT).astype(object).where(s.notna(), None)


def stop_events_table(rows: pd.DataFrame) -> pa.Table:
    frame = rows.copy()
    frame["sched_arr"] = _datetime_text(frame["sched_arr"])
    frame["sched_dep"] = _datetime_text(frame["sched_dep"])
    return pa.Table.from_pandas(frame[OUTPUT_COLUMNS], schema=STOP_EVENTS_SCHEMA,
                                preserve_index=False)


def run_scope_table(day: date, lines: list[Line]) -> pa.Table:
    scopes = sorted({l.scope for l in lines})
    return pa.table({"betriebstag": [day] * len(scopes),
                     "linien_text": [s[0] for s in scopes],
                     "betreiber_abk": [s[1] for s in scopes]}, schema=RUN_SCOPE_SCHEMA)


def to_parquet_bytes(table: pa.Table) -> bytes:
    buf = io.BytesIO()
    pq.write_table(table, buf, compression="snappy")
    return buf.getvalue()


def write_day(store: ObjectStore, day: date, rows: pd.DataFrame,
              lines: list[Line]) -> WriteResult:
    events, scope = stop_events_table(rows), run_scope_table(day, lines)
    result = WriteResult(rows=events.num_rows, scopes=scope.num_rows,
                         stop_events_key=stop_events_key(day), run_scope_key=run_scope_key(day))
    store.write(result.stop_events_key, to_parquet_bytes(events), PARQUET)
    store.write(result.run_scope_key, to_parquet_bytes(scope), PARQUET)
    return result
