# Database and Aggregation

PostgreSQL holds the two layers after the raw files: **staging** (filled by the
transform job) and **reporting** (the data product). The aggregation is a stored
procedure in the same database; there is no separate container.

## Folder structure

```
db/
├── init/
│   ├── 01_schemas.sql             schemas staging + reporting, table staging.stop_events
│   ├── 02_reporting_tables.sql    table reporting.od_delay_by_slot, constraints, lookup index
│   └── 03_refresh_procedure.sql   procedure reporting.refresh_od_delay()
└── tests/                         scenario, reference, behaviour and performance tests
```

The scripts in `init/` run automatically, in file order, the first time the
`database` container starts with an empty volume. Tables and procedures are only
created this way, never by hand.

## The data product

`reporting.od_delay_by_slot` answers: *how late is line X from stop A to stop B,
on this weekday, in this hour?* For any origin–destination pair of a line, not only
neighbouring stops.

| Column | Meaning |
|---|---|
| `linien_text`, `betreiber_abk` | line and operator |
| `from_bpuic`, `from_stop` | origin stop |
| `to_bpuic`, `to_stop` | destination stop (any later stop of the same trip) |
| `weekday` | ISO weekday of the planned departure at the origin, 1 = Mon … 7 = Sun |
| `slot_hour` | hour 0–23 of the planned departure at the origin |
| `median_dep_delay_min` | median departure delay at the origin, minutes |
| `median_arr_delay_min` | median arrival delay at the destination, minutes |
| `n_dep_obs`, `n_arr_obs` | number of measurements behind each median |

Example:

```sql
SELECT slot_hour, median_arr_delay_min, n_arr_obs
FROM reporting.od_delay_by_slot
WHERE linien_text = 'S1' AND betreiber_abk = 'SBB'
  AND from_stop = 'Rotkreuz' AND to_stop = 'Luzern'
  AND weekday = 1
ORDER BY slot_hour;
```

Column comments in the database: `\d+ reporting.od_delay_by_slot`.

## How the aggregation works

```sql
CALL reporting.refresh_od_delay();
```

Kestra calls it at the end of every run that changed staging. In one transaction:

### 1. Lock

An advisory lock ensures only one refresh runs at a time. A second call waits up to
`lock_wait` (default 30 s) and then fails without changing anything:

```sql
CALL reporting.refresh_od_delay(lock_wait => interval '5 minutes');  -- 0 = wait without limit
```

### 2. Delete

All rows of the reporting table are deleted. Until the commit, readers still see
the old content.

### 3. Build pairs

For every trip, every stop is paired with every **later** stop of the same trip
(`stop_sequence`). A stop without planned departure (last stop) is never an origin.
The reverse direction comes from trips running the other way.

### 4. Group and compute

Pairs are grouped by line, origin, destination, weekday and hour. Weekday and hour
come from the **planned departure at the origin**: a trip of Monday's operating day
leaving at 00:06 on Tuesday counts as Tuesday, slot 0.

- Median of the departure delays at the origin and of the arrival delays at the destination.
- NULL delays are ignored; the counts show how many measurements remain.
- Seconds are converted to minutes and rounded to one decimal.
- A group without any measurement is not written.

### 5. Report

The procedure ends with a notice:

```
NOTICE:  reporting.refresh_od_delay: rows_deleted=… rows_written=… distinct_pairs=… duration=…s
```

Any error rolls back the whole call; the old table stays intact.

## Integrity

The table enforces its own rules:

- primary key: line, operator, origin, destination, weekday, hour
- weekday 1–7, hour 0–23, counts ≥ 0, at least one count > 0
- a median is NULL **exactly** when its count is 0

## Changing tables or procedures

Init scripts only run on an empty volume. After changing a file in `init/`
(or the database credentials), recreate the database volume:

```bash
docker compose down
docker volume rm deng-datapipeline_db-data
docker compose up -d
```

This deletes staging and reporting, not the raw files. Reload them afterwards with
a Kestra `transform_only` run over the needed range.

## Design decisions

### Stored procedure instead of a container

The aggregation is pure set logic: a self-join, grouping and a median. The database
does this best, and the data never leaves it. A separate container would read
staging out of PostgreSQL, aggregate in Python and write the result back: more code,
more transfer and another image, just to control one procedure. Kestra calls the
procedure directly.

This is different from the transform, which streams files from object storage and
applies per-timestamp validation logic; that belongs in Python.

The procedure's source of truth is `init/03_refresh_procedure.sql` in the repo. A
change made only in the running database is lost at the next volume reset and never
reaches other teams.

### Full recompute on every run

Each call deletes the reporting table and rebuilds it from all of staging. The runtime
grows with the amount of data, but the refresh runs early in the morning, when nobody
uses the data product. In return it is simple and always idempotent: no tracking of
which days changed, and a median cannot be updated incrementally anyway; it needs all
values.

### All origin–destination pairs precomputed

Every combination of stops of a line is stored, not only neighbouring stops. Any
journey is then a single lookup, regardless of where the student gets on and off.
This is the core decision of the data product. The price is table size, which grows
quadratically with the number of stops per line.

Delays of neighbouring segments are not summed: the arrival delay at the destination
already contains the delay of the whole journey.

### Median instead of mean

The project started with the average. We switched to the median, as recommended by
the lecturer, because it is robust against outliers such as a single train two hours
late.

### Slot by planned departure at the origin

Weekday and hour come from the planned departure at the origin, not from the actual
time and not from the operating day. This matches how a commuter thinks ("my 07:12
train"); using the actual time would move a late train into a later slot.

### Individual weekdays

Students have their lectures on different days: first semesters mostly on Monday and
Tuesday, later semesters on Thursday and Friday. Each weekday is therefore its own
slot. A split into weekdays and weekend would mix days with different traffic and
distort exactly the picture a student needs. That this leaves fewer measurements per
row is intended: the value shows what really happened on that weekday.

### Departure and arrival delay

The arrival delay at the destination tells whether you arrive on time or catch your
connection. The departure delay at the origin completes the picture. Example: class
ends at 17:50 and the train to Luzern leaves at 17:51. If it usually leaves a few
minutes late, there is no need to leave class early.

### Minutes in reporting, seconds in staging

Staging keeps whole seconds; only the reporting rounds to minutes with one decimal.
If staging stored rounded minutes, every further calculation built on it would carry
that rounding error. Seconds keep all options open.

### Number of observations

Each median comes with the number of measurements behind it (`n_dep_obs`,
`n_arr_obs`). There is no minimum; whoever needs to judge the reliability of a value
can do so.

### DELETE instead of TRUNCATE

`TRUNCATE` would be faster but locks the table for readers until the refresh is
done. With `DELETE`, a query during the refresh still sees the previous state. The
data may be one day old for those minutes, which is better than no answer.

### Advisory lock

Two refreshes never run at the same time. A second call waits a limited time and then
fails cleanly instead of hanging.

### Init scripts, no migrations

Tables and procedures are only created by the init scripts; there is no migration
tool. After a change the database volume is recreated and staging is reloaded from
the raw files. This is not an enterprise environment: the rebuild is quick, and
nothing in the database is lost that cannot be rebuilt.