"""Compute the staging rows: signed delays and the row hash that drives the upsert."""
from __future__ import annotations

import hashlib
import json
from datetime import date

import pandas as pd

# Column order of staging.stop_events without row_hash and loaded_at.
# The row hash is computed over exactly these values, in this order.
VALUE_COLUMNS = [
    "betriebstag", "fahrt_bezeichner", "bpuic", "haltestellen_name",
    "linien_text", "betreiber_abk", "stop_sequence", "sched_arr", "sched_dep",
    "arr_delay_s", "dep_delay_s", "is_additional_trip",
]
STAGING_COLUMNS = VALUE_COLUMNS + ["row_hash"]


def delay_seconds(actual: pd.Series, planned: pd.Series, valid: pd.Series) -> pd.Series:
    """actual - planned in whole seconds; <NA> where the timestamp is not valid."""
    seconds = (actual - planned).dt.total_seconds().round()
    return seconds.where(valid).astype("Int64")


def build_rows(validated: pd.DataFrame, day: date) -> pd.DataFrame:
    """One output row per served stop, staging column names and types."""
    v = validated
    out = pd.DataFrame({
        "betriebstag": [day] * len(v),
        "fahrt_bezeichner": v["fahrt_bezeichner"],
        "bpuic": v["bpuic"].astype("int64"),
        "haltestellen_name": v["HALTESTELLEN_NAME"].str.strip(),
        "linien_text": v["LINIEN_TEXT"].str.strip(),
        "betreiber_abk": v["BETREIBER_ABK"].str.strip(),
        "stop_sequence": v["stop_sequence"].astype("int64"),
        "sched_arr": v["sched_arr"],
        "sched_dep": v["sched_dep"],
        "arr_delay_s": delay_seconds(v["act_arr"], v["sched_arr"], v["arr_valid"]),
        "dep_delay_s": delay_seconds(v["act_dep"], v["sched_dep"], v["dep_valid"]),
        "is_additional_trip": v["ZUSATZFAHRT_TF"].str.strip().str.lower() == "true",
    }).reset_index(drop=True)
    out["betriebstag"] = out["betriebstag"].astype(object)
    records = to_records(out)
    out["row_hash"] = pd.Series([row_hash(r) for r in records], dtype=object)
    return out


def to_records(frame: pd.DataFrame) -> list[tuple]:
    """Plain Python values per row (NaT/<NA> -> None) in VALUE_COLUMNS order:
    the exact values that are hashed and written to PostgreSQL."""
    cols = []
    for name in VALUE_COLUMNS:
        s = frame[name]
        if name in ("sched_arr", "sched_dep"):
            cols.append([None if pd.isna(x) else x.to_pydatetime() for x in s])
        elif name in ("bpuic", "stop_sequence", "arr_delay_s", "dep_delay_s"):
            cols.append([None if pd.isna(x) else int(x) for x in s])
        elif name == "is_additional_trip":
            cols.append([bool(x) for x in s])
        else:
            cols.append([None if x is None or (not isinstance(x, (str, date)) and pd.isna(x))
                         else x for x in s])
    return list(zip(*cols))


def _canonical(value) -> object:
    """JSON-safe canonical form: dates/times ISO 8601, NULL -> null."""
    if value is None:
        return None
    if isinstance(value, (date,)):
        return value.isoformat()
    return value


def row_hash(record: tuple) -> str:
    """SHA-256 over the canonical string of all stored value columns.

    The canonical string is a compact JSON array of the values in
    VALUE_COLUMNS order (strings verbatim, integers as integers, booleans as
    true/false, timestamps ISO 8601 'YYYY-MM-DDTHH:MM:SS', NULL as null).
    JSON quoting makes it unambiguous whatever characters the texts contain.
    """
    canonical = json.dumps([_canonical(v) for v in record],
                           ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()