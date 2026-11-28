# Data Model

The cloud version of the pipeline stores the data in three layers. Each layer has
one job:

| Layer | Where | Content | Role |
|---|---|---|---|
| Data lake | GCS bucket, `raw/YYYY-MM-DD_istdaten.csv` | one original daily file per operating day, unchanged | source of truth, allows every table to be rebuilt |
| Warehouse | BigQuery dataset `core` | star schema: one fact table, four dimensions | cleaned, validated detail data |
| Data mart | BigQuery dataset `mart` | one aggregated table | the data product the app reads |

```
GCS raw/*.csv ──► transform (Python) ──► GCS processed/<day>_stop_events.parquet, _run_scope.parquet
                                               │  Kestra LoadFromGcs (WRITE_TRUNCATE)
                                               ▼
                                         core.stg_stop_event + core.stg_run_scope (load tables, current run only)
                                               │
                                               ├──► core.dim_stop   (MERGE)
                                               ├──► core.dim_line   (MERGE)
                                               └──► core.fact_stop_event (replace day partitions)
core.dim_date, core.dim_time_slot  (generated once)
core.fact_stop_event + dims ──► mart.mart_od_delay_by_slot (full recompute, SQL)
```

## Star schema

```
                 dim_date                 dim_time_slot
                (betriebstag)         (arr_slot_hour / dep_slot_hour)
                      │                        │
 dim_stop ──── fact_stop_event ────────────────┘
 (stop_key)           │
                      └──── dim_line (line_key)
```

### `core.fact_stop_event`

**Grain: one served stop of one trip on one operating day.**
Natural key: `(betriebstag, fahrt_bezeichner, bpuic)`.

| Column | Type | Source (Ist-Daten) | Meaning |
|---|---|---|---|
| `betriebstag` | DATE | `BETRIEBSTAG` | operating day; key to `dim_date`; partition column |
| `fahrt_bezeichner` | STRING | `FAHRT_BEZEICHNER` | trip id, unique within an operating day (degenerate dimension) |
| `stop_key` | INT64 | from `BPUIC` | key to `dim_stop` |
| `line_key` | INT64 | from `LINIEN_TEXT`, `BETREIBER_ABK` | key to `dim_line` |
| `stop_sequence` | INT64 | computed | 1-based, gap-free position of the stop within the trip |
| `sched_arr` | DATETIME | `ANKUNFTSZEIT` | planned arrival, Swiss local time; NULL at the first stop |
| `sched_dep` | DATETIME | `ABFAHRTSZEIT` | planned departure, Swiss local time; NULL at the last stop |
| `arr_slot_hour` | INT64 | hour of `sched_arr` | key to `dim_time_slot` (role: arrival) |
| `dep_slot_hour` | INT64 | hour of `sched_dep` | key to `dim_time_slot` (role: departure) |
| `arr_delay_s` | INT64 | `AN_PROGNOSE − ANKUNFTSZEIT` | **measure**, seconds, negative = early, NULL = no valid measurement |
| `dep_delay_s` | INT64 | `AB_PROGNOSE − ABFAHRTSZEIT` | **measure**, seconds, negative = early, NULL = no valid measurement |
| `is_additional_trip` | BOOL | `ZUSATZFAHRT_TF` | trip not in the regular timetable |
| `loaded_at` | TIMESTAMP | – | time the day was last loaded |

- **Partitioning:** by `betriebstag` (daily). Every pipeline run writes whole days (per
  line), so a rerun only touches the affected partitions.
- **Clustering:** `line_key, stop_key`. The aggregation and ad-hoc analysis filter and
  join by line and stop.

### `core.dim_stop`

**Grain: one stop (BPUIC).** Derived from the transformed data, SCD type 1.

| Column | Type | Source | Meaning |
|---|---|---|---|
| `stop_key` | INT64 | `core.stop_key(bpuic)` | surrogate key |
| `bpuic` | INT64 | `BPUIC` | natural key |
| `haltestellen_name` | STRING | `HALTESTELLEN_NAME` | current stop name |
| `source_day` | DATE | – | operating day the attributes were taken from |
| `updated_at` | TIMESTAMP | – | last change |

### `core.dim_line`

**Grain: one line of the data product, identified by `(linien_text, betreiber_abk)`.**
Derived from the transformed data, SCD type 1.

| Column | Type | Source | Meaning |
|---|---|---|---|
| `line_key` | INT64 | `core.line_key(linien_text, betreiber_abk)` | surrogate key |
| `linien_text` | STRING | `LINIEN_TEXT` | e.g. `S1`, `IR75` |
| `betreiber_abk` | STRING | `BETREIBER_ABK` | e.g. `SBB` |
| `betreiber_name` | STRING | `BETREIBER_NAME` | operator name |
| `verkehrsmittel_text` | STRING | `VERKEHRSMITTEL_TEXT` | e.g. `S`, `IR` |
| `produkt_id` | STRING | `PRODUKT_ID` | e.g. `Zug` |
| `source_day` | DATE | – | operating day the attributes were taken from |
| `updated_at` | TIMESTAMP | – | last change |

`LINIEN_ID` is **not** part of the line: for trains it holds the train number
(e.g. `2106`), which changes with every trip. A line in the sense of the data product
is defined by `transform/config/lines.yml`, including the `via_bpuic` filter
(SBB runs an S1 in two networks; only trips via Rotkreuz are kept).

### `core.dim_date`

**Grain: one calendar day.** Generated (date spine), not derived from the data, so
days without data still exist.

| Column | Type | Meaning |
|---|---|---|
| `date` | DATE | key |
| `iso_weekday` | INT64 | 1 = Monday … 7 = Sunday |
| `weekday_name` | STRING | `Monday` … |
| `is_weekend` | BOOL | Saturday or Sunday |
| `iso_week`, `month`, `year` | INT64 | calendar attributes |

### `core.dim_time_slot`

**Grain: one hour of the day.** Generated, 24 static rows.

| Column | Type | Meaning |
|---|---|---|
| `slot_hour` | INT64 | key, 0–23 |
| `label` | STRING | e.g. `07:00–07:59` |

### `mart.mart_od_delay_by_slot`

**Grain: one line × origin stop × destination stop × ISO weekday × one-hour slot of the
planned departure at the origin.** Every ordered pair of stops of a trip counts, not
only neighbouring stops. Aggregated from `fact_stop_event`.

| Column | Type | Meaning |
|---|---|---|
| `line_key`, `linien_text`, `betreiber_abk` | INT64, STRING | line |
| `from_stop_key`, `from_stop` | INT64, STRING | origin stop (role of `dim_stop`) |
| `to_stop_key`, `to_stop` | INT64, STRING | destination stop (role of `dim_stop`), any later stop of the same trip |
| `weekday` | INT64 | ISO weekday of the planned departure at the origin |
| `slot_hour` | INT64 | hour of the planned departure at the origin |
| `median_dep_delay_min` | NUMERIC | median departure delay at the origin, minutes, rounded to 0.1 |
| `median_arr_delay_min` | NUMERIC | median arrival delay at the destination, minutes, rounded to 0.1 |
| `n_dep_obs`, `n_arr_obs` | INT64 | number of measurements behind each median |

- **Denormalised on purpose:** the names are copied from the dimensions at every
  refresh, so the app reads one table without joins. The dimensions stay the source
  of the names; the keys keep the link to them.
- **No partitioning:** the table is small and always recomputed in full.
- **Clustering:** `linien_text, from_stop, to_stop`, the filter of the app query
  (*line X from A to B*). At the current size the effect is small; it documents the
  expected query pattern.

Example query of the app:

```sql
SELECT slot_hour, median_arr_delay_min, n_arr_obs
FROM mart.mart_od_delay_by_slot
WHERE linien_text = 'S1' AND betreiber_abk = 'SBB'
  AND from_stop = 'Rotkreuz' AND to_stop = 'Luzern'
  AND weekday = 1
ORDER BY slot_hour;
```

## Design decisions

**Transform stays in Python, modelling runs in BigQuery (ETL + ELT).**
The validation in `transform/app/validate.py` (stop order across midnight, duplicates,
status, range and time-order checks per timestamp) is complex and covered by tests.
It is reused unchanged: it reads the raw files from the data lake and writes the
validated rows back to the lake as Parquet (`processed/`). Kestra loads these files
into the load tables (`LoadFromGcs`), so every step is visible in the flow. Keys,
dimensions, fact and mart are then built with SQL inside BigQuery.

**Surrogate keys are deterministic hashes.**
BigQuery has no sequences. Keys from `ROW_NUMBER()` or `GENERATE_UUID()` would change
on every rerun and break the joins. `FARM_FINGERPRINT` of the natural key always gives
the same INT64, so reruns and backfills stay consistent and the fact load needs no
lookup into the dimensions. The keys are defined once, as the SQL functions
`core.stop_key(bpuic)` = `FARM_FINGERPRINT(CAST(bpuic AS STRING))` and
`core.line_key(linien_text, betreiber_abk)` =
`FARM_FINGERPRINT(CONCAT(linien_text, '|', betreiber_abk))`, and computed in SQL only,
never in Python.
`dim_date` and `dim_time_slot` use their natural value (`DATE`, hour) as key: the fact
needs `betriebstag` as `DATE` for partitioning anyway, and a second key would add nothing.

**SCD type 1 for stops and lines.**
A renamed stop should appear with its new name in the app; history of names is not
needed for the use case. `source_day` guards the overwrite: attributes are only
replaced by data from the same or a later operating day, so a backfill of old days
never puts an older name back.

**Only served stops.**
Cancelled stops (`FAELLT_AUS_TF`) and pass-throughs (`DURCHFAHRT_TF`) are removed in
the transform. The figures describe the delay of stops that were actually served.

**Weekday and slot come from the planned departure, not the operating day.**
A trip of operating day Monday that departs at 00:30 counts as Tuesday, 0:00 slot.
This is what a passenger looks up. The mart therefore does not join `dim_date` via
`betriebstag`; `dim_date` serves analysis on the fact table.

**Planned times travel as text.**
The transform writes `sched_arr` / `sched_dep` as `YYYY-MM-DD HH:MM:SS` (Swiss local
time) and `10_load_core.sql` casts them to `DATETIME`. A Parquet timestamp would be
loaded by BigQuery as `TIMESTAMP` (UTC) and silently shift the local times.

**Median, not mean.**
Delays are skewed by a few large outliers; the median describes the typical case.
In BigQuery it is computed exactly with `PERCENTILE_CONT(x, 0.5)`, not with the
approximate `APPROX_QUANTILES`.

## Loading and reruns

| Table | How it is filled | Rerun behaviour |
|---|---|---|
| `stg_stop_event` | Kestra load of `processed/<day>_stop_events.parquet` | replaced by each load (`WRITE_TRUNCATE`); columns come from the Parquet file |
| `stg_run_scope` | Kestra load of `processed/<day>_run_scope.parquet`: one row per processed (day, line), also for lines without rows | replaced by each load |
| `dim_stop`, `dim_line` | `MERGE` from `stg_stop_event` | insert new, update changed attributes (not with older data) |
| `fact_stop_event` | delete the processed days and lines, insert from `stg_stop_event` | idempotent; other lines of the same day stay untouched |
| `dim_date`, `dim_time_slot` | generated once at setup | – |
| `mart_od_delay_by_slot` | full recompute from fact + dims | idempotent |

The SQL is in [`bigquery/sql/`](../bigquery/sql/). Data-quality checks of every
step, incl. the results table `core.dq_check_result`: [data_quality.md](data_quality.md). The load into `core` and the mart
refresh each run in one transaction; on an error nothing changes. BigQuery does not
enforce primary keys, so the load checks the fact key of the load table first and
stops without changes if it finds duplicates.

## Evolution from the local version (Architecture v0.1/v0.2)

The model already existed locally; the cloud version makes it explicit.

| Local (PostgreSQL) | Cloud (BigQuery) | Change |
|---|---|---|
| raw file in SeaweedFS | raw file in GCS | storage location only |
| `staging.stop_events` | `core.fact_stop_event` | same grain and logic; names and line text moved to dimensions, surrogate keys added |
| – | `core.dim_stop`, `core.dim_line`, `core.dim_date`, `core.dim_time_slot` | new |
| `reporting.od_delay_by_slot` | `mart.mart_od_delay_by_slot` | same grain and medians; keys added |
| `reporting.refresh_od_delay()` | SQL step in Kestra | stored procedure replaced by BigQuery SQL |

The local `staging` table was already validated and at final grain, so it is a fact
table, not a staging area in the usual sense. The landing zone for unvalidated data
is the data lake.
