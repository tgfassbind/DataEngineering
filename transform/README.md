# Transform

Batch job that reads the raw files from the data lake (Cloud Storage bucket),
keeps only the configured lines, validates every timestamp, computes the delays and
writes the result back to the data lake as Parquet (`processed/`). Kestra then loads
these files into BigQuery and builds the star schema with SQL
([`bigquery/`](../bigquery/README.md), [data model](../docs/data_model.md)).

```
gs://…/raw/<day>_istdaten.csv ──► transform ──► gs://…/processed/<day>_stop_events.parquet
                                              └► gs://…/processed/<day>_run_scope.parquet
```

## Folder structure

```
transform/
├── app/
│   ├── __main__.py      entry point for `python -m app` (container command)
│   ├── cli.py           argument parsing, date range, per-day log block, exit codes
│   ├── config.py        settings from environment variables, line selection (file / CLI)
│   ├── storage.py       Cloud Storage access (google-cloud-storage)
│   ├── read.py          streams the raw CSV from the bucket in chunks, filters lines
│   ├── validate.py      timestamp parsing, stop order, validation rules
│   ├── compute.py       arrival/departure delays, output columns
│   ├── output.py        writes the two Parquet files per day to processed/
│   └── seed.py          uploads a local CSV into raw/ (for the test fixture)
├── config/
│   └── lines.yml        versioned selection of lines
├── tests/               unit tests and end-to-end tests (against a GCS emulator)
├── Dockerfile           python:3.12.7-slim-bookworm, pinned dependencies
├── requirements.txt     runtime dependencies incl. pinned transitive packages
└── requirements-dev.txt test-only dependencies
```


## Usage

A job like the fetcher (Compose profile `jobs`). In normal operation Kestra starts it. By hand:

```bash
docker compose run --rm transform --date 2026-09-30
docker compose run --rm transform --start-date 2026-09-01 --end-date 2026-09-30
docker compose run --rm transform --date 2026-09-30 --lines IR75:SBB,S1:SBB@8502202
```

| Argument | Meaning |
|---|---|
| `--date YYYY-MM-DD` | one operating day |
| `--start-date` / `--end-date` | inclusive date range |
| `--lines` | other lines than `config/lines.yml` for this run |

The raw file must already be in the bucket; otherwise the day fails with
"fetch the day first". A rerun simply writes the day's files again.

## How it works

Each day is processed on its own. A failing day does not stop the others.

### 1. Select lines

A line is `linien_text` **and** `betreiber_abk` (e.g. `IR75` / `SBB`). Where one
operator uses the same name in several networks (SBB runs an S1 around Basel and
one around Luzern/Zug), the entry lists stops in `via_bpuic`: a trip is kept only if
it touches one of them, and then the whole trip is kept.

```yaml
lines:
  - linien_text: IR75
    betreiber_abk: SBB
    via_bpuic: [8502202]
  - linien_text: S1
    betreiber_abk: SBB
    via_bpuic: [8502202]     # Rotkreuz: S1 Luzern/Zug only
```

Precedence: `--lines` > `TRANSFORM_LINES_FILE` > `config/lines.yml`.
On the CLI, via stops are written as `S1:SBB@8502202` (several: `@A+B`).

### 2. Read

The raw file is streamed from the bucket straight into pandas in chunks
(`TRANSFORM_CHUNK_SIZE`, default 100,000 rows). Each chunk is reduced to the
selected lines and the needed columns. The file is never written to disk and never
fully held in memory. The separator (`;` or `,`) is detected from the header.

### 3. Order stops

Timestamps are parsed as full datetimes. Each trip is sorted by planned time and
gets a `stop_sequence`. This happens before any timestamp is rejected, so the
sequence reflects the real stop order regardless of the order in the file.

### 4. Validate

Rows that cannot be used are dropped; individual timestamps that cannot be trusted
are set to NULL. Arrival and departure of a stop are judged separately.

| Rule | Applies to | On failure |
|---|---|---|
| train cancelled (`FAELLT_AUS_TF`) | row | dropped |
| pass-through, no stop (`DURCHFAHRT_TF`) | row | dropped |
| trip id missing or `BPUIC` not an integer | row | dropped |
| same trip and stop twice | row | first kept, others dropped |
| status is exactly `REAL` | timestamp | NULL |
| planned and actual time present and parseable | timestamp | NULL |
| \|delay\| ≤ `TRANSFORM_MAX_ABS_DELAY_S` (3 h) | timestamp | NULL |
| same stop: actual arrival ≤ actual departure | both timestamps | both NULL |
| next stop: departure before arrival, segment ≤ `TRANSFORM_MAX_SEGMENT_DURATION_S` (6 h) | both timestamps | both NULL |

Early trains are valid (negative delay). Missing values are always NULL, never 0.

### 5. Compute

- `arr_delay_s` = actual − planned arrival, `dep_delay_s` = actual − planned departure,
  in whole seconds, signed.
- Line attributes (`betreiber_name`, `verkehrsmittel_text`, `produkt_id`) are passed
  through unchanged for the dimension `dim_line`.

### 6. Write

Two Parquet files per day, each replaced in one step:

| File | Content | Loaded into |
|---|---|---|
| `processed/<day>_stop_events.parquet` | one row per served stop | `core.stg_stop_event` |
| `processed/<day>_run_scope.parquet` | one row per processed line, also for a line without rows that day | `core.stg_run_scope` |

The scope file is written last. If anything fails before, the day is `FAILED`
(exit code 1) and Kestra loads nothing.

Columns of `stop_events`:

| Column | Parquet type | Meaning |
|---|---|---|
| `betriebstag` | date | operating day |
| `fahrt_bezeichner` | string | trip id |
| `bpuic` | int64 | stop id |
| `haltestellen_name` | string | stop name |
| `linien_text`, `betreiber_abk` | string | line (natural key of `dim_line`) |
| `betreiber_name`, `verkehrsmittel_text`, `produkt_id` | string | line attributes |
| `stop_sequence` | int64 | 1-based position of the stop in the trip |
| `sched_arr`, `sched_dep` | string | planned time `YYYY-MM-DD HH:MM:SS`, Swiss local time; NULL at first / last stop |
| `arr_delay_s`, `dep_delay_s` | int64 | signed delay in seconds; NULL = no valid measurement |
| `is_additional_trip` | bool | `ZUSATZFAHRT_TF` |

### 7. Logs and exit codes

One block per day with rows read / written, dropped rows per reason, rejected
timestamps, trips excluded by via stops and the written files, followed by a summary.

| Exit code | Meaning |
|---|---|
| 0 | all days written |
| 1 | at least one day failed (raw file missing or invalid, storage error); the other days are written |
| 2 | invalid arguments or configuration (e.g. bucket missing or not accessible) |

## Configuration

| Variable | Purpose |
|---|---|
| `GCS_BUCKET` | data lake bucket, shared with the fetcher |
| `GCP_PROJECT_ID` | optional; empty = project of the service-account key |
| `GCP_PIPELINE_KEY_FILE` | host path of the pipeline service account's JSON key; mounted read-only, `GOOGLE_APPLICATION_CREDENTIALS` is set in the container |
| `TRANSFORM_LINES_FILE` | other lines file (path inside the container); empty = `config/lines.yml` |
| `TRANSFORM_CHUNK_SIZE` | CSV rows per chunk (memory vs. speed) |
| `TRANSFORM_MAX_ABS_DELAY_S` | max absolute delay in seconds (default 10800 = 3 h) |
| `TRANSFORM_MAX_SEGMENT_DURATION_S` | max travel time between two stops (default 21600 = 6 h) |
| `TRANSFORM_MEM_LIMIT` | hard memory limit of the container |

`config/lines.yml` is mounted from the repo, so edits apply without a rebuild.

## Test with the fixture

`dataset/` contains an excerpt of one day (21.09.2026). Upload it to `raw/` and
transform it, without fetching (this writes to the real bucket):

```bash
docker compose run --rm --entrypoint python transform \
    -m app.seed --file dataset/excerpt_21092026_istdaten_v2.csv --date 2026-09-21
docker compose run --rm transform --date 2026-09-21 --lines IR75:SBB,S1:SBB
```

## Tests

```bash
cd transform
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
```

- `test_transform`: reading, validation rules, delays and the Parquet output, on the
  fixture and synthetic rows (no storage).
- `test_e2e`: the real CLI against a local Cloud Storage emulator
  (`gcp-storage-emulator`, in memory): output files, line filter and scope, reruns,
  missing / invalid raw files, storage errors, configuration errors. No Google
  account or Docker needed; the real bucket is never touched.

## Design decisions

### Output close to the raw data

The output keeps one row per trip and stop, close to the raw format; it becomes the
fact table `fact_stop_event`. Origin–destination pairs, weekdays and time slots are
only built in the data mart.

This keeps options open: if the aggregation changes (other slots, other statistics,
other pairs), the fact table is already prepared and does not have to be rebuilt
first. The transform only does what every later use needs: select lines, validate,
compute delays.

### Python for validation, SQL for modelling

Until Architecture v0.2 the transform loaded PostgreSQL directly (hash upsert,
`--retransform`). In the cloud version it only writes files: the validation is
complex and covered by tests, so it stays in Python (ETL); keys, dimensions, fact
and mart are built in BigQuery with SQL (ELT). Writing files instead of calling
BigQuery from Python keeps every step visible in Kestra: transform, load
(`LoadFromGcs`), SQL. The validated result also stays in the data lake, so a load
can be repeated without transforming again.

### Parquet, with planned times as text

Parquet carries the column types, so BigQuery needs no type guessing (as with CSV).
The planned times are the exception: BigQuery loads a Parquet timestamp as
`TIMESTAMP`, i.e. as UTC, which would silently shift Swiss local times. They are
therefore written as text and cast to `DATETIME` in `bigquery/sql/10_load_core.sql`.

### Streaming in chunks

A daily file covers all of Switzerland, while we only need a few lines. The file is
streamed and reduced chunk by chunk, so memory use stays low and constant,
independent of the file size, and the container runs within a fixed memory limit.

### Stop order before validation

Each trip's stops are numbered (`stop_sequence`) by planned time before any
timestamp is rejected. The aggregation builds origin–destination pairs from this
order, so it must reflect the real sequence even when actual times are invalid.
The result also does not depend on the row order in the file.

### Validation per timestamp, not per row

Arrival and departure of a stop are judged separately: an invalid departure does
not remove a valid arrival. This keeps as many real measurements as possible.

We accept that an occasional wrong value slips through: the data product is a
median, which is robust against single outliers.

### Validation rules and limits

These rules are the core of the filter logic:

- **Only `REAL`.** `PROGNOSE` is a forecast and `UNBEKANNT` is unknown; neither is a measurement.
- **Max 3 h delay.** Larger values are almost certainly data errors and would distort the statistics.
- **Time order.** A train cannot leave a stop before it arrives, or arrive at the next
  stop before leaving the previous one; and it does not take more than 6 h between two
  stops. If two timestamps contradict each other, both are discarded, because we cannot
  tell which one is wrong.

The limits are configurable (`TRANSFORM_MAX_ABS_DELAY_S`,
`TRANSFORM_MAX_SEGMENT_DURATION_S`).

### Line identity via stops

The first architecture assumed that line name and operator identify a line. In the
data they don't: SBB runs an S1 around Basel and one around Luzern/Zug, and the raw
data has no network id. `via_bpuic` was added later: a line is additionally defined
by stops it must serve.

Consequence: trips of the line that never reach these stops are dropped, e.g. an S1
running only Luzern–Sursee. For commuters to Rotkreuz this is intended.

### Reruns replace whole days

A rerun writes the day's files again; `10_load_core.sql` then replaces exactly the
processed days and lines in the fact table (`run_scope`). Changed values, removed
rows (e.g. after a stricter validation rule or a corrected source file) and lines
without rows that day are therefore all handled the same way, without a hash or a
special retransform mode.

The transform only sees what is in the bucket. A change at the source reaches
BigQuery only after the day was re-fetched with `--force-overwrite`.

**Known limitation:** only the lines of the current run are replaced. Rows of a
line that was removed from `lines.yml` stay in the fact table until they are
deleted by hand.

### Rejected rows are only logged

Rejected rows are counted per reason in the log but not stored. We cannot correct
the raw data, and any correction on our side would no longer match the original.
The raw file in the bucket remains the place to look at them.

### Line selection as a versioned file

The selected lines are part of what defines the result, so they live in
`config/lines.yml` in the repo rather than in `.env`: the same commit processes the
same lines. A YAML file also allows the per-line via stops, which would be awkward
in a single environment variable.