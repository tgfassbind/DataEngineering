# Data Quality

The pipeline checks the data at every step. Two kinds of checks:

- **Blocking checks** guard the correctness of what is stored. A failed check stops
  the step, nothing is changed, and the Kestra run fails.
- **Monitoring checks** look for things that are possible but unusual (a missing
  line, a low volume, old data). They never stop the pipeline; findings are stored
  in `core.dq_check_result` and logged as warnings in Kestra.

| Step | Where | Kind | On failure |
|---|---|---|---|
| 1. Fetch | `fetcher/app/fetch.py` | blocking per day | day `FAILED`, raw file not created or replaced |
| 2. Transform | `transform/app/validate.py`, `read.py` | row / value filter + blocking per day | invalid rows and timestamps are dropped and counted; an invalid file fails the day, nothing written |
| 3. Load into the star schema | `bigquery/sql/10_load_core.sql` | blocking | load stopped, fact and dimensions unchanged |
| 4. Data mart | `bigquery/sql/20_refresh_mart.sql` | blocking | rollback, the previous mart stays published |
| 5. Monitoring | `bigquery/sql/30_check_quality.sql` | warning | finding recorded and logged, run stays successful |

## 1. Fetch

- The file must start with the header `BETRIEBSTAG` (guards against HTML error pages
  or a changed format) before anything is uploaded.
- A download that breaks off cancels the upload: no partial file, an existing file
  stays unchanged.
- Status per day: `FETCHED`, `SKIPPED`, `OVERWRITTEN`, `FAILED`; exit code 1 if any day
  failed (Kestra retries the fetch).

## 2. Transform

Applied to every row of the selected lines (details: [transform README](../transform/README.md)):

| Rule | Effect |
|---|---|
| cancelled stop (`FAELLT_AUS_TF`), pass-through (`DURCHFAHRT_TF`) | row dropped (design decision: only served stops) |
| missing trip id or invalid `BPUIC` | row dropped (`invalid_key`) |
| same stop twice in a trip | later row dropped (`duplicate_key`) |
| status not `REAL`, time missing or unreadable | timestamp rejected, delay = NULL |
| delay > 3 h (configurable) | timestamp rejected (`delay_out_of_range`) |
| arrival after departure, segment not positive or > 6 h | timestamp rejected (`time_order`) |
| file of another operating day, unknown flag values | day `FAILED` (`RawFileInvalid`), nothing written |

Every dropped row and rejected timestamp is counted per reason in the log of the day.
The raw file in the bucket stays unchanged, so every decision can be traced back.

## 3. Load into the star schema (blocking)

Checked on the load table `core.stg_stop_event` before anything is written:

| Check | Why |
|---|---|
| `row_outside_run_scope` | the delete only covers the processed days and lines; a row outside would be duplicated |
| `required_value_missing` | keys and flags must be filled (the Parquet columns are nullable) |
| `duplicate_fact_key` | (operating day, trip, stop) is the fact key; BigQuery does not enforce primary keys |
| `stop_sequence_not_1_to_n` | the mart pairs stops by their sequence |
| `trip_with_several_lines` | a trip belongs to exactly one line |
| `planned_time_unreadable` | planned times arrive as text and must be valid `DATETIME` values |
| `delay_without_planned_time` | a delay needs the planned time it refers to |
| `delay_over_24h` | hard upper bound, independent of the transform's configurable limit |
| `day_not_in_dim_date` | the calendar must cover every loaded day |

After the insert, inside the same transaction: **referential integrity** of the new
fact rows to `dim_stop`, `dim_line` and `dim_time_slot` (BigQuery does not enforce
foreign keys). A failure rolls back the whole day.

The error message lists every failed check with its count, e.g.
`Load stopped, nothing changed. Failed checks on core.stg_stop_event: duplicate_fact_key (3)`.

## 4. Data mart (blocking)

Checked on the new content before the commit; a failure keeps the previous mart:

| Check | Why |
|---|---|
| `duplicate_mart_key` | one row per line, OD pair, weekday and slot |
| `median_does_not_match_count` | a median exists exactly when there are measurements |
| `group_without_measurement` | groups without any measurement are not part of the product |
| `weekday_or_slot_out_of_range` | weekday 1–7, slot 0–23 |
| `origin_equals_destination` | an OD pair has two different stops |
| `mart_empty` | facts with measurements exist, but the mart is empty |

## 5. Monitoring (warnings)

Run once per pipeline run, for its date range. One `PASS` row per check without
findings, one `WARN` row per finding in `core.dq_check_result`.

| Check | Finding when | Typical cause |
|---|---|---|
| `line_missing_on_day` | a line with rows in the last 28 days has none on the day | strike, line not in the source, filter problem |
| `volume_anomaly` | rows per day and line differ by more than 50 % from the median of the same weekday in the last 28 days (at least 2 such days) | holiday timetable, construction work, incomplete source file |
| `low_measurement_share` | less than 80 % of the planned arrivals or departures have a valid measurement | missing real-time data; the medians of the day rest on few values |
| `stale_data` | the newest operating day in the fact table is more than 2 days old | the daily run did not happen or failed |

The thresholds are declared at the top of `30_check_quality.sql`.

## Failure handling and reruns

| What failed | State afterwards | What to do |
|---|---|---|
| fetch of a day | other days fetched; Kestra retries 5× every 30 min | if still failing: check the source, rerun `backfill` (`full` or `fetch_only`) |
| transform of a day | other days written; flow fails before loading | fix the cause, rerun `transform_only` |
| blocking load check | that day unchanged in BigQuery; flow fails | read the listed checks, fix, rerun `transform_only` or `load_only` |
| blocking mart check | previous mart still published | fix, rerun `load_only` |
| monitoring warning | run successful | look at the finding; rerun if the source was incomplete (`full` with `force_overwrite`) |

Every step can be rerun: the fetch skips stored days unless forced, the transform
replaces its files, the load replaces the processed days and lines, and the mart is
recomputed in full.

## Queries

Warnings of the last run:

```sql
SELECT r.betriebstag, r.check_name, l.linien_text, r.details
FROM core.dq_check_result AS r
LEFT JOIN core.dim_line AS l USING (line_key)
WHERE r.checked_at = (SELECT MAX(checked_at) FROM core.dq_check_result)
  AND r.status = 'WARN'
ORDER BY r.betriebstag, r.check_name;
```

Warnings per check over the last 30 days:

```sql
SELECT check_name, COUNTIF(status = 'WARN') AS findings, COUNT(DISTINCT run_id) AS runs
FROM core.dq_check_result
WHERE checked_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 30 DAY)
GROUP BY check_name
ORDER BY findings DESC;
```
