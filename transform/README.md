# Transform

Batch job that reads the raw files from object storage, keeps only the configured
lines, validates every timestamp, computes the delays and loads the result into
the staging table `staging.stop_events` in PostgreSQL.

## Folder structure

```
transform/
├── app/
│   ├── __main__.py      entry point for `python -m app` (container command)
│   ├── cli.py           argument parsing, date range, per-day log block, exit codes
│   ├── config.py        settings from environment variables, line selection (file / CLI)
│   ├── read.py          streams the raw CSV from object storage in chunks, filters lines
│   ├── validate.py      timestamp parsing, stop order, validation rules
│   ├── compute.py       arrival/departure delays, row_hash
│   ├── load.py          COPY into a temp table, upsert or retransform in one transaction
│   └── seed.py          uploads a local CSV into object storage (for the test fixture)
├── config/
│   └── lines.yml        versioned selection of lines
├── tests/               unit and end-to-end tests
├── Dockerfile           python:3.12.7-slim-bookworm, pinned dependencies
├── requirements.txt     runtime dependencies incl. pinned transitive packages
└── requirements-dev.txt test-only dependencies
```

The staging table itself is created by `db/init/01_schemas.sql`
(column comments: `\d+ staging.stop_events`).

## Usage

A job like the fetcher (Compose profile `jobs`). In normal operation Kestra starts it. By hand:

```bash
docker compose run --rm transform --date 2026-09-30
docker compose run --rm transform --start-date 2026-09-01 --end-date 2026-09-30
docker compose run --rm transform --date 2026-09-30 --lines IR75:SBB,S1:SBB@8502202
docker compose run --rm transform --date 2026-09-30 --retransform
```

| Argument | Meaning |
|---|---|
| `--date YYYY-MM-DD` | one operating day |
| `--start-date` / `--end-date` | inclusive date range |
| `--lines` | other lines than `config/lines.yml` for this run |
| `--retransform` | delete the day (for the selected lines) and load it from scratch |

The raw file must already be in object storage; otherwise the day fails with
"fetch the day first".

## How it works

Each day is processed on its own and in its own transaction. A failing day does
not stop the others and never leaves staging half-loaded.

### 1. Select lines

A line is `linien_text` **and** `betreiber_abk` (e.g. `IR75` / `SBB`). Where one
operator uses the same name in several networks (SBB runs an S1 around Basel and
one around Luzern/Zug), the entry lists stops in `via_bpuic`: a trip is kept only if
it touches one of them, and then the whole trip is kept.

```yaml
lines:
  - linien_text: IR75
    betreiber_abk: SBB
  - linien_text: S1
    betreiber_abk: SBB
    via_bpuic: [8502202]     # Rotkreuz: S1 Luzern/Zug only
```

Precedence: `--lines` > `TRANSFORM_LINES_FILE` > `config/lines.yml`.
On the CLI, via stops are written as `S1:SBB@8502202` (several: `@A+B`).

### 2. Read

The raw file is streamed from object storage straight into pandas in chunks
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
- `row_hash` = SHA-256 over all stored value columns. It detects whether a row changed.

### 6. Load

In one transaction per day:

1. `COPY` all rows into a temporary table.
2. With `--retransform`: delete the day's rows of the selected lines.
3. Upsert on the key (operating day, trip, stop):
   - new key → insert
   - key exists, hash differs → update
   - key exists, same hash → untouched

A concurrent run for the same day (e.g. a manual rerun while Kestra runs) waits
for the first one to finish.

### 7. Logs and exit codes

One block per day with inserted / updated / unchanged / deleted rows, dropped rows
per reason and trips excluded by via stops, followed by a summary.

| Exit code | Meaning |
|---|---|
| 0 | all days loaded |
| 1 | at least one day failed; the other days are loaded |
| 2 | invalid arguments or configuration |

## Configuration

| Variable | Purpose |
|---|---|
| `S3_*` | object storage connection, shared with the fetcher |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | database connection |
| `TRANSFORM_LINES_FILE` | other lines file (path inside the container); empty = `config/lines.yml` |
| `TRANSFORM_CHUNK_SIZE` | CSV rows per chunk (memory vs. speed) |
| `TRANSFORM_MAX_ABS_DELAY_S` | max absolute delay in seconds (default 10800 = 3 h) |
| `TRANSFORM_MAX_SEGMENT_DURATION_S` | max travel time between two stops (default 21600 = 6 h) |
| `TRANSFORM_MEM_LIMIT` | hard memory limit of the container |

`config/lines.yml` is mounted from the repo, so edits apply without a rebuild.

## Test with the fixture

`dataset/` contains an excerpt of one day (21.09.2026). Upload it and transform it,
without fetching:

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

## Design decisions

### Staging close to the raw data

Staging keeps one row per trip and stop, close to the raw format. Origin–destination
pairs, weekdays and time slots are only built in the aggregation.

This keeps options open: if the aggregation changes (other slots, other statistics,
other pairs), staging is already prepared and does not have to be rebuilt first.
The transform only does what every later use needs: select lines, validate,
compute delays.

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

### Hash upsert and retransform

Every row carries a `row_hash` over all its stored values. A normal run inserts new
rows, updates rows whose hash changed and leaves the rest untouched. Reruns are
therefore cheap and idempotent, and changed values (from a re-fetched raw file or
changed transform code) are picked up automatically.

What an upsert cannot do is remove rows that should no longer exist, e.g. after
a stricter validation rule, a changed line definition, or a corrected source file
with fewer trips. For these cases `--retransform` deletes the day (for the selected
lines) and loads it from scratch, in the same transaction.

The transform only sees what is in object storage. A change at the source reaches
staging only after the day was re-fetched with `--force-overwrite`.

**Known limitation:** `--retransform` only deletes rows of the currently selected
lines. Rows of a line that was removed from `lines.yml` stay in staging and keep
flowing into the aggregation until they are deleted by hand.

### One transaction per day

Each day is loaded completely or not at all, so staging never holds a half-loaded
day, and a failing day does not roll back the others. An advisory lock makes a
second run for the same day wait instead of interfering.

### Rejected rows are only logged

Rejected rows are counted per reason in the log but not stored. We cannot correct
the raw data, and any correction on our side would no longer match the original.
The raw file in object storage remains the place to look at them.

### Line selection as a versioned file

The selected lines are part of what defines the result, so they live in
`config/lines.yml` in the repo rather than in `.env`: the same commit processes the
same lines. A YAML file also allows the per-line via stops, which would be awkward
in a single environment variable.