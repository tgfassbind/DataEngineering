"""Compute the output rows: one row per served stop with signed delays."""
from __future__ import annotations

from datetime import date

import pandas as pd

# Columns of the output file processed/<day>_stop_events.parquet, in order.
# Same as the BigQuery load table core.stg_stop_event.
OUTPUT_COLUMNS = [
    "betriebstag", "fahrt_bezeichner", "bpuic", "haltestellen_name",
    "linien_text", "betreiber_abk", "betreiber_name", "verkehrsmittel_text", "produkt_id",
    "stop_sequence", "sched_arr", "sched_dep",
    "arr_delay_s", "dep_delay_s", "is_additional_trip",
]


def delay_seconds(actual: pd.Series, planned: pd.Series, valid: pd.Series) -> pd.Series:
    """actual - planned in whole seconds; <NA> where the timestamp is not valid."""
    seconds = (actual - planned).dt.total_seconds().round()
    return seconds.where(valid).astype("Int64")


def build_rows(validated: pd.DataFrame, day: date) -> pd.DataFrame:
    """One output row per served stop, output column names and types.

    sched_arr / sched_dep stay pandas timestamps (naive, Swiss local time);
    output.py writes them as text, see there.
    """
    v = validated
    out = pd.DataFrame({
        "betriebstag": [day] * len(v),
        "fahrt_bezeichner": v["fahrt_bezeichner"],
        "bpuic": v["bpuic"].astype("int64"),
        "haltestellen_name": v["HALTESTELLEN_NAME"].str.strip(),
        "linien_text": v["LINIEN_TEXT"].str.strip(),
        "betreiber_abk": v["BETREIBER_ABK"].str.strip(),
        "betreiber_name": v["BETREIBER_NAME"].str.strip(),
        "verkehrsmittel_text": v["VERKEHRSMITTEL_TEXT"].str.strip(),
        "produkt_id": v["PRODUKT_ID"].str.strip(),
        "stop_sequence": v["stop_sequence"].astype("int64"),
        "sched_arr": v["sched_arr"],
        "sched_dep": v["sched_dep"],
        "arr_delay_s": delay_seconds(v["act_arr"], v["sched_arr"], v["arr_valid"]),
        "dep_delay_s": delay_seconds(v["act_dep"], v["sched_dep"], v["dep_valid"]),
        "is_additional_trip": v["ZUSATZFAHRT_TF"].str.strip().str.lower() == "true",
    }).reset_index(drop=True)
    out["betriebstag"] = out["betriebstag"].astype(object)
    return out[OUTPUT_COLUMNS]
