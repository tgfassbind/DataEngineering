"""Write one day's rows into staging.stop_events: bulk, one transaction.

    COPY rows -> temporary table
    [--retransform: DELETE the day's rows of the processed lines]
    INSERT ... SELECT FROM temp
      ON CONFLICT (betriebstag, fahrt_bezeichner, bpuic) DO UPDATE
      WHERE stored row_hash IS DISTINCT FROM new row_hash

New keys are inserted, rows with a different hash updated (loaded_at = now()),
identical rows not touched at all (no write, loaded_at unchanged).

Without --retransform, rows already in staging whose key no longer appears in
the new result (e.g. a stop that is now cancelled after a re-fetch) are left
in place; they are counted as "stale" in the log so they do not go unnoticed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import psycopg

from .compute import STAGING_COLUMNS, VALUE_COLUMNS, to_records
from .config import Line

TABLE = "staging.stop_events"
KEY = ("betriebstag", "fahrt_bezeichner", "bpuic")
_cols = ", ".join(STAGING_COLUMNS)
_updates = ",\n    ".join(f"{c} = EXCLUDED.{c}" for c in STAGING_COLUMNS + ["loaded_at"]
                           if c not in KEY)

CREATE_TEMP = f"""
CREATE TEMP TABLE stage_stop_events
    (LIKE {TABLE} INCLUDING DEFAULTS) ON COMMIT DROP
"""

DELETE_DAY = f"""
DELETE FROM {TABLE} AS t
USING unnest(%(texts)s::text[], %(operators)s::text[]) AS l(linien_text, betreiber_abk)
WHERE t.betriebstag = %(day)s
  AND t.linien_text = l.linien_text
  AND t.betreiber_abk = l.betreiber_abk
"""

# Rows of this day + these lines that the new result does not contain any more.
COUNT_STALE = f"""
SELECT count(*)
FROM {TABLE} AS t
JOIN unnest(%(texts)s::text[], %(operators)s::text[]) AS l(linien_text, betreiber_abk)
  ON t.linien_text = l.linien_text AND t.betreiber_abk = l.betreiber_abk
WHERE t.betriebstag = %(day)s
  AND NOT EXISTS (SELECT 1 FROM stage_stop_events s
                  WHERE s.betriebstag = t.betriebstag
                    AND s.fahrt_bezeichner = t.fahrt_bezeichner
                    AND s.bpuic = t.bpuic)
"""

UPSERT = f"""
WITH upserted AS (
    INSERT INTO {TABLE} ({_cols}, loaded_at)
    SELECT {_cols}, now() FROM stage_stop_events
    ON CONFLICT ({", ".join(KEY)}) DO UPDATE SET
    {_updates}
    WHERE {TABLE}.row_hash IS DISTINCT FROM EXCLUDED.row_hash
    -- xmax = 0 marks a tuple inserted by this statement; an updated tuple
    -- carries this transaction's id in xmax. Not formally documented but
    -- the standard idiom for ON CONFLICT and stable across PostgreSQL 9.5-17.
    -- Unchanged rows (WHERE above is false) are not returned at all.
    RETURNING (xmax = 0) AS inserted
)
SELECT count(*) FILTER (WHERE inserted)     AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated
FROM upserted
"""


LOCK_TIMEOUT = "5min"


@dataclass
class LoadResult:
    deleted: int = 0
    stale: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0


def load_day(conn: psycopg.Connection, day: date, rows: pd.DataFrame,
             lines: list[Line], retransform: bool) -> LoadResult:
    """Load one day in ONE transaction. Any error rolls back everything for
    this day, so staging never holds a half-loaded day."""
    result = LoadResult()
    with conn.transaction():
        with conn.cursor() as cur:
            # serialise concurrent runs for the same day (e.g. a manual rerun
            # while the orchestrator runs); released at commit/rollback.
            # lock_timeout: never wait forever behind a stuck session.
            cur.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
            cur.execute("SELECT pg_advisory_xact_lock(hashtext('staging.stop_events'), "
                        "%s)", (day.toordinal(),))
            cur.execute(CREATE_TEMP)
            with cur.copy(f"COPY stage_stop_events ({_cols}) FROM STDIN") as copy:
                for record, h in zip(to_records(rows), rows["row_hash"]):
                    copy.write_row(record + (h,))
            pairs = sorted({l.scope for l in lines})
            scope = {"day": day,
                     "texts": [t for t, _ in pairs],
                     "operators": [o for _, o in pairs]}
            if retransform:
                cur.execute(DELETE_DAY, scope)
                result.deleted = cur.rowcount
            else:
                cur.execute(COUNT_STALE, scope)
                result.stale = cur.fetchone()[0]
            cur.execute(UPSERT)
            result.inserted, result.updated = cur.fetchone()
    result.unchanged = len(rows) - result.inserted - result.updated
    return result


assert VALUE_COLUMNS + ["row_hash"] == STAGING_COLUMNS
