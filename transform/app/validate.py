"""Validation: turn the filtered raw rows of one day into served stops with a
validity flag PER TIMESTAMP (arrival and departure are judged separately).

Order of work (see README "Validation rules"):
  1. drop rows: cancelled (FAELLT_AUS_TF), pass-through (DURCHFAHRT_TF),
     unusable key (empty FAHRT_BEZEICHNER / non-integer BPUIC)
  2. parse all timestamps as full datetimes (midnight crossings work)
  3. sort each trip by planned time and assign stop_sequence (before any
     timestamp is invalidated, so the sequence is the true stop order);
     a duplicate (trip, stop) keeps the first by stop_sequence, the others are
     dropped BEFORE the time checks so they cannot invalidate their neighbours
  4. per timestamp: status REAL, both times present+parseable, |delay| <= limit
  5. time-order rules on the timestamps that survived step 4; a violation
     invalidates both timestamps involved

Comparisons are always between datetimes / durations, never calendar dates.
Early arrivals/departures (negative delays) are valid.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import Limits

ROW_REASONS = ("cancelled", "pass_through", "invalid_key", "duplicate_key")
TIMESTAMP_REASONS = ("not_real", "missing_or_unparseable", "delay_out_of_range", "time_order")
SIDES = {
    # side: (planned column, actual column, status column)
    "arr": ("ANKUNFTSZEIT", "AN_PROGNOSE", "AN_PROGNOSE_STATUS"),
    "dep": ("ABFAHRTSZEIT", "AB_PROGNOSE", "AB_PROGNOSE_STATUS"),
}
INT32_MAX = 2_147_483_647


@dataclass
class ValidationStats:
    rows_dropped: dict = field(default_factory=lambda: dict.fromkeys(ROW_REASONS, 0))
    # rejected timestamps per reason, split by side
    timestamps_rejected: dict = field(default_factory=lambda: {
        side: dict.fromkeys(TIMESTAMP_REASONS, 0) for side in SIDES})
    # timestamps that do not exist (first stop: no arrival, last stop: no
    # departure). Not a rejection: there is simply nothing to measure.
    not_applicable: dict = field(default_factory=lambda: dict.fromkeys(SIDES, 0))


def parse_timestamps(values: pd.Series) -> pd.Series:
    """'21.09.2026 05:10:29' (actual) or '21.09.2026 05:09' (planned) ->
    datetime64. Empty or unparseable -> NaT. Both formats are accepted in
    every column, so a precision change upstream does not break parsing."""
    s = values.str.strip()
    out = pd.to_datetime(s, format="%d.%m.%Y %H:%M:%S", errors="coerce")
    retry = out.isna() & (s != "")
    if retry.any():
        out.loc[retry] = pd.to_datetime(s[retry], format="%d.%m.%Y %H:%M", errors="coerce")
    return out


def _flag(values: pd.Series) -> pd.Series:
    return values.str.strip().str.lower() == "true"


def validate(raw: pd.DataFrame, limits: Limits) -> tuple[pd.DataFrame, ValidationStats]:
    """Return one row per served stop with parsed times, stop_sequence and the
    validity of each timestamp (columns arr_valid / dep_valid)."""
    stats = ValidationStats()
    df = raw.copy()
    df["_row"] = np.arange(len(df))   # file order, only as last tie-breaker

    # -- 1. row-level drops ------------------------------------------------------
    cancelled = _flag(df["FAELLT_AUS_TF"])
    stats.rows_dropped["cancelled"] = int(cancelled.sum())
    df = df.loc[~cancelled]

    pass_through = _flag(df["DURCHFAHRT_TF"])
    stats.rows_dropped["pass_through"] = int(pass_through.sum())
    df = df.loc[~pass_through].copy()

    df["fahrt_bezeichner"] = df["FAHRT_BEZEICHNER"].str.strip()
    bpuic_txt = df["BPUIC"].str.strip()
    bpuic_ok = bpuic_txt.str.fullmatch(r"\d{1,10}")
    bpuic = pd.to_numeric(bpuic_txt.where(bpuic_ok), errors="coerce")
    key_ok = (df["fahrt_bezeichner"] != "") & bpuic.notna() & (bpuic <= INT32_MAX)
    stats.rows_dropped["invalid_key"] = int((~key_ok).sum())
    df = df.loc[key_ok].copy()
    df["bpuic"] = bpuic.loc[key_ok].astype("int64")

    # -- 2. parse timestamps -----------------------------------------------------
    for side, (planned, actual, status) in SIDES.items():
        df[f"sched_{side}"] = parse_timestamps(df[planned])
        df[f"act_{side}"] = parse_timestamps(df[actual])
        df[f"status_{side}"] = df[status].str.strip()

    # -- 3. stop order within each trip -----------------------------------------
    # 1st key: planned arrival, else planned departure (first stop has no
    # arrival). 2nd key: planned departure, else arrival - resolves minute
    # ties such as "A departs 10:00" vs "B arrives 10:00, departs 10:01".
    # Rows without any planned time go last; file order is only the final
    # tie-breaker for rows whose planned times are completely identical.
    df["_order"] = df["sched_arr"].fillna(df["sched_dep"])
    df["_order2"] = df["sched_dep"].fillna(df["sched_arr"])
    df = df.sort_values(["fahrt_bezeichner", "_order", "_order2", "_row"],
                        na_position="last", kind="mergesort").reset_index(drop=True)
    # duplicate (trip, stop): keep the first in stop order, drop the rest now
    dup = df.duplicated(["fahrt_bezeichner", "bpuic"], keep="first")
    stats.rows_dropped["duplicate_key"] = int(dup.sum())
    df = df.loc[~dup].reset_index(drop=True)
    df["stop_sequence"] = df.groupby("fahrt_bezeichner", sort=False).cumcount() + 1

    # -- 4. per-timestamp checks -------------------------------------------------
    for side, (planned, actual, status) in SIDES.items():
        rejected = stats.timestamps_rejected[side]
        present = ((df[planned].str.strip() != "") | (df[actual].str.strip() != "")
                   | (df[f"status_{side}"] != ""))
        stats.not_applicable[side] = int((~present).sum())

        not_real = present & (df[f"status_{side}"] != "REAL")
        unparseable = (present & ~not_real
                       & (df[f"sched_{side}"].isna() | df[f"act_{side}"].isna()))
        delay = (df[f"act_{side}"] - df[f"sched_{side}"]).dt.total_seconds()
        out_of_range = (present & ~not_real & ~unparseable
                        & (delay.abs() > limits.max_abs_delay_s))
        rejected["not_real"] = int(not_real.sum())
        rejected["missing_or_unparseable"] = int(unparseable.sum())
        rejected["delay_out_of_range"] = int(out_of_range.sum())
        df[f"{side}_valid"] = present & ~not_real & ~unparseable & ~out_of_range

    # -- 5. time-order rules (evaluated together on the step-4 result) -----------
    arr_ok, dep_ok = df["arr_valid"].copy(), df["dep_valid"].copy()
    # same stop: actual arrival <= actual departure
    same_stop = arr_ok & dep_ok & (df["act_arr"] > df["act_dep"])
    # consecutive stops A -> B of the same trip:
    #   actual departure(A) < actual arrival(B) and the segment <= limit
    trip = df.groupby("fahrt_bezeichner", sort=False)
    next_arr = trip["act_arr"].shift(-1)
    next_ok = trip["arr_valid"].shift(-1, fill_value=False).astype(bool)
    segment_s = (next_arr - df["act_dep"]).dt.total_seconds()
    bad_segment = dep_ok & next_ok & ~((segment_s > 0)
                                       & (segment_s <= limits.max_segment_duration_s))
    # bad_segment on row A invalidates dep(A) and arr(B) (= next row, same trip)
    arr_bad_from_segment = (bad_segment.groupby(df["fahrt_bezeichner"], sort=False)
                            .shift(1, fill_value=False).astype(bool))
    arr_time_order = arr_ok & (same_stop | arr_bad_from_segment)
    dep_time_order = dep_ok & (same_stop | bad_segment)
    stats.timestamps_rejected["arr"]["time_order"] = int(arr_time_order.sum())
    stats.timestamps_rejected["dep"]["time_order"] = int(dep_time_order.sum())
    df["arr_valid"] = arr_ok & ~arr_time_order
    df["dep_valid"] = dep_ok & ~dep_time_order
    return df, stats
