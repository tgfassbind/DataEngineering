# DENG HS2026
This is the Data Engineering project repository for group 01.

The group members are:
* Til Fassbind
* Alexander Türler

## Repository structure

```
dataengineering/
├── dataset/
├── docs/ # documents related to the project

├── db/
│   ├── init/ # run by the Postgres image on an empty db-data volume, in this order
│   │  ├─ 01_schemas.sql # schemas + staging.stop_events
│   │  ├─ 02_reporting_tables.sql # reporting.od_delay_by_slot
│   │  └─ 03_refresh_procedure.sql # CALL reporting.refresh_od_delay()
│   ├── tests/ # aggregation tests
│   └── requirements-dev.txt
│
├── fetcher/ # fetcher component of the data pipeline
│   ├─ app/
│   │  ├─ cli.py # argparse entrypoint, exit codes
│   │  ├─ config.py # env vars, storage key contract
│   │  ├─ routing.py # pure date → source logic
│   │  ├─ fetch.py # idempotency, download, extraction, upload
│   │  ├─ storage.py # S3 API wrapper
│   │  └─ inventory.py # list stored objects (key, size, ETag)
│   ├─ tests/
│   ├─ Dockerfile
│   ├─ requirements.txt # runtime dependencies
│   └─ requirements-dev.txt # test-only dependencies
│
├── orchestrator/ # workflow orchestration (Kestra)
│   ├─ flows/
│   │  ├─ backfill.yml # deng.backfill: fetch -> transform -> aggregate for a date range
│   │  └─ daily.yml # deng.daily: schedule, runs backfill for yesterday
│   └─ kestra/application.yml # server configuration
│
├── transform/ # transform component (raw CSV -> PostgreSQL staging)
│   ├─ app/
│   │  ├─ cli.py # argparse entrypoint, exit codes
│   │  ├─ config.py # env vars, lines file, storage key contract
│   │  ├─ read.py # S3 streaming, chunked CSV read, line filter
│   │  ├─ validate.py # stop order + per-timestamp validation
│   │  ├─ compute.py # delays, row hash
│   │  ├─ load.py # COPY + hash upsert, retransform
│   │  ├─ pipeline.py # one day end to end, summary log line
│   │  └─ seed.py # dev helper: upload fixture / synthetic file
│   ├─ config/lines.yml # selected lines
│   ├─ tests/
│   ├─ Dockerfile
│   └─ requirements.txt
│
├── .env.example # configuration template
├── docker-compose.yml
└── README.md
```

## Project plan
The project plan uses GitHub's Issues feature for backlog items; a written overview can also be found under docs.

## Dependencies
Docker Engine with Docker Compose v2, outbound internet.

# Quick start: Object Storage + Fetcher

```bash
cp .env.example .env # local defaults
docker compose run --rm fetcher --date 2026-10-01 # builds, starts storage, fetches
```

`docker compose run` starts the object storage automatically and waits for its
healthcheck. Always use `--rm`: it removes the fetcher container **and** its
anonymous scratch volume.

### More examples

```bash
# single day
docker compose run --rm fetcher --date 2026-10-01

# date range (inclusive); may span months, each date is routed individually
docker compose run --rm fetcher --start-date 2026-09-25 --end-date 2026-10-02

# re-fetch and overwrite even if the files already exist
docker compose run --rm fetcher --date 2026-10-01 --force-overwrite
docker compose run --rm fetcher --start-date 2026-08-01 --end-date 2026-08-31 --force-overwrite

# list what is stored (key, size, ETag), used to check differences between teams
docker compose run --rm --entrypoint python fetcher -m app.inventory
```

## Object storage

Local object storage is **SeaweedFS** (`chrislusf/seaweedfs:4.48`, Apache-2.0)
in single-node `mini` mode, used purely through its S3-compatible API on
port 8333. It contains no project logic.

* Credentials: `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` from `.env`;
  requests without valid credentials are rejected.
* The bucket `S3_BUCKET` is created by the storage service on startup.
* Only the S3 API is published, and only on `127.0.0.1`. WebDAV, the Iceberg
  and Lance catalogs and the admin UI are disabled.

## Storage contract

```
s3://<S3_BUCKET>/raw/YYYY-MM-DD_istdaten.csv
```

Example (`S3_BUCKET=sbb-ist-daten`):

```
sbb-ist-daten/
└── raw/
    ├── 2026-09-29_istdaten.csv   ← from archive ZIP 2026-09
    ├── 2026-09-30_istdaten.csv   ← from archive ZIP 2026-09
    ├── 2026-10-01_istdaten.csv   ← from permalink
    └── 2026-10-02_istdaten.csv   ← from permalink
```

* One object per operating day, key derived only from the date. The source
  (permalink vs. archive) is **not** visible in the key and does not matter to
  consumers.
* Content is the original CSV, byte-for-byte as delivered by the source
  (`;`-separated, header row starting with `BETRIEBSTAG`). No filtering.
* An object either exists completely or not at all (uploads only become
  visible on completion; a failed `--force-overwrite` leaves the old object).
* `Content-Type: text/csv`.

## How the fetcher decides

Routing, per date (`FETCHER_TIMEZONE`, default Europe/Zurich, defines "today"):

| Requested date | Source |
|---|---|
| in the current calendar month | permalink `…/ist-daten-v2/resource_permalink/YYYY-MM-DD_istdaten.csv` |
| **today is the 1st** and the date is yesterday (last day of the previous month) | permalink (the previous month's archive is only published by the first working day of the new month) |
| any other date in an earlier month | monthly archive ZIP `…/istdaten/YYYY/ist-daten-v2-YYYY-MM.zip` |
| after today | rejected (exit code 2) |

* **Permalink:** streamed directly into storage, no temp files.
* **Archive:** downloaded once per month into the scratch volume. Members named
  `YYYY-MM-DD_IstDaten.csv` at the ZIP root are read with Python's `zipfile`,
  and only the requested days are uploaded. The scratch directory is removed
  afterwards, on success or failure.

Idempotency, per date, checked before anything is downloaded:

| Object exists? | `--force-overwrite` | Result |
|---|---|---|
| no | – | fetch → `FETCHED` |
| yes | no | `SKIPPED` (source not contacted; the ZIP isn't downloaded if all its days are skipped) |
| yes | yes | fetch → `OVERWRITTEN` |
| any error | – | `FAILED`, other dates continue |

Before uploading, the first line of each file is checked for `BETRIEBSTAG`, so an
HTML error or challenge page served with HTTP 200 is never stored as data.

**Known limitation:** on the 2nd of a month (or later, until the archive is
published), dates of the previous month other than "yesterday on the 1st" route
to the archive. If that archive is not yet online, they fail with a clear log
line; rerun them later. They are never stored partially.

### Logs and exit codes

One line per date, plus a summary:

```
INFO app.fetch: date=2026-09-30 source=archive status=FETCHED 37200058 bytes from 2026-09-30_istdaten.csv -> raw/2026-09-30_istdaten.csv
INFO app.fetch: date=2026-10-01 source=permalink status=SKIPPED raw/2026-10-01_istdaten.csv already exists
ERROR app.fetch: date=2026-10-02 source=permalink status=FAILED https://…: 404 Client Error …
INFO fetcher: Summary: fetched=1, skipped=1, overwritten=0, failed=1
```

| Exit code | Meaning |
|---|---|
| 0 | all dates FETCHED / OVERWRITTEN / SKIPPED |
| 1 | at least one date FAILED (safe to rerun: done dates are skipped) |
| 2 | invalid arguments or configuration (incl. bucket missing) |

## Configuration (environment variables)

Set in `.env` (template: `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | **required** | storage credentials (used by storage *and* fetcher) |
| `S3_ENDPOINT_URL` | `http://object-storage:8333` | set to an **empty value** for real AWS S3 |
| `S3_BUCKET` | `sbb-ist-daten` | bucket name |
| `S3_REGION` | `us-east-1` | region |
| `S3_CREATE_BUCKET` | `false` | let the fetcher create a missing bucket |
| `S3_BUCKET_WAIT_SECONDS` | 60 | how long the fetcher waits for the bucket to appear |
| `S3_API_PORT` | 8333 | host port of the S3 API (127.0.0.1 only) |
| `PERMALINK_URL_TEMPLATE` | `https://data.opentransportdata.swiss/dataset/ist-daten-v2/resource_permalink/{day}_istdaten.csv` | `{day}` → YYYY-MM-DD |
| `ARCHIVE_URL_TEMPLATE` | `https://archive.opentransportdata.swiss/istdaten/{year}/ist-daten-{year}-{month}.zip` | `{year}` → YYYY, `{month}` → MM |
| `HTTP_TIMEOUT_SECONDS` | 120 | per-request timeout |
| `FETCHER_TIMEZONE` | `Europe/Zurich` | defines "today" / "current month" |
| `FETCHER_REFERENCE_DATE` | (empty = real today) | pin "today" for deterministic routing |

**Switching to AWS S3 later:** in `.env` set `S3_ENDPOINT_URL=` (empty), real
credentials and `S3_REGION`, and provision the bucket in AWS (or set
`S3_CREATE_BUCKET=true`). No code changes.

## Reproducibility

* Images pinned: `python:3.12.7-slim-bookworm`, `chrislusf/seaweedfs:4.48`.
  For byte-identical images, additionally pin digests (`image@sha256:…`).
* All Python packages pinned incl. transitive ones, installed with `--no-deps` + `pip check`.
* All configuration through `.env`
* Routing depends on "today". To reproduce someone else's run exactly, use the
  same dates and the same `FETCHER_REFERENCE_DATE`.
* Compare results with `python -m app.inventory`: same sizes and ETags mean the
  same bytes.
* Limit: the upstream data itself is outside our control (a permalink day can
  only be fetched while it is published). The object store is the
  reproducible snapshot.

## Tests (no Docker or internet needed)

```bash
cd fetcher
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
```

`test_routing` is pure stdlib. `test_e2e` runs the real CLI against an S3
server and a local mock of both SBB sources. It covers:

* month-boundary ranges and the first-of-month rule
* skip and overwrite behaviour
* failure isolation and temp cleanup
* bucket handling, inventory and exit codes

By default the S3 server is moto, an emulator. To run against the real storage
service instead:

```bash
docker compose up -d object-storage
E2E_S3_ENDPOINT=http://127.0.0.1:8333 E2E_S3_ACCESS_KEY_ID=<from .env> \
E2E_S3_SECRET_ACCESS_KEY=<from .env> python -m unittest tests.test_e2e -v
```

# Transform: raw CSV → PostgreSQL staging

Reads one day's raw file from object storage, keeps only the selected lines,
validates every timestamp, computes signed delays and upserts the result into
`staging.stop_events` (one row per trip × stop).

```
raw/YYYY-MM-DD_istdaten.csv ──stream──► filter lines ──► validate ──► delays + row_hash ──► staging.stop_events
        (object storage)          (chunks of 100k rows)                                   (PostgreSQL, upsert)
```

## Quick start

```bash
cp .env.example .env

# 1. put a day into object storage: either fetch it ...
docker compose run --rm fetcher --date 2026-10-01
# ... or, for the test fixture (21.09.2026, line IR75 only), upload it directly
docker compose run --rm --entrypoint python transform \
    -m app.seed --file dataset/excerpt_21092026_istdaten_v2.csv --date 2026-09-21

# 2. transform it (starts object storage + database, waits for both)
docker compose run --rm transform --date 2026-09-21

# 3. look at the result (any PostgreSQL client works, port 5432 on localhost;
#    adjust -U / -d if you changed POSTGRES_USER / POSTGRES_DB)
docker compose exec database psql -U deng -d deng \
    -c "SELECT count(*), count(arr_delay_s), count(dep_delay_s) FROM staging.stop_events"
#  count | count | count
#    445 |   377 |   394
```

Expected result for the fixture: 445 rows (450 − 5 cancelled), 51 trips,
377 arrival and 394 departure delays; all other delays are NULL.

### Looking at the data with pgAdmin (optional)

```bash
docker compose --profile tools up -d pgadmin
```

Open http://localhost:5050 and log in with `PGADMIN_DEFAULT_EMAIL` /
`PGADMIN_DEFAULT_PASSWORD` from `.env`. The server **DENG → SBB Ist-Daten** is
already registered (host `database`, user `POSTGRES_USER`, database
`POSTGRES_DB`). On the first connect pgAdmin asks for `POSTGRES_PASSWORD`; tick
"Save password" to skip that next time. The data is under
*Databases → deng → Schemas → staging → Tables → stop_events*.

* pgAdmin is not part of the pipeline: it only starts with `--profile tools`,
  and `docker compose down` stops it like everything else.
* Login and the registered server are set up only when the `pgadmin-data`
  volume is created. After changing them in `.env`, run
  `docker compose rm -sf pgadmin && docker volume rm deng-datapipeline_pgadmin-data`
  (pgAdmin settings only, no pipeline data).
* Published on 127.0.0.1 only (`PGADMIN_PORT`, default 5050).

### More examples

```bash
# date range (inclusive); each day is its own transaction, a failing day does not stop the others
docker compose run --rm transform --start-date 2026-09-01 --end-date 2026-09-30

# other lines than config/lines.yml (LINIEN_TEXT:BETREIBER_ABK[@BPUIC+BPUIC], comma separated or repeated)
docker compose run --rm transform --date 2026-09-21 --lines IR75:SBB,S1:SBB@8502202

# rebuild a day from scratch (after a code change or a re-fetch)
docker compose run --rm transform --date 2026-09-21 --retransform
```

Always use `--rm`. The job runs, prints one summary line per day and exits.
Ranges are limited to 3660 days (a guard against typos in the year).

## How a run works

Per day (`--date` or each day of the range):

1. **Read.** `raw/<date>_istdaten.csv` is streamed from object storage (boto3
   `StreamingBody`) straight into `pandas.read_csv(chunksize=…)`. Nothing is
   written to disk and the file is never fully in memory. Each chunk is reduced
   to the selected lines and the 16 needed columns; everything is read as text.
   A missing object fails the day with a clear message ("fetch the day first").
   The separator (`,` for Ist-Daten v2, `;` for older files) is detected from
   the header.
2. **Order.** Timestamps are parsed as full datetimes (both `21.09.2026 05:09`
   and `21.09.2026 05:10:29`). Each trip is sorted by planned time and gets a
   `stop_sequence`, before any timestamp is rejected, so the sequence is the true
   stop order. File order does not matter.
3. **Validate** each timestamp (see rules below).
4. **Compute** `arr_delay_s` / `dep_delay_s` and the `row_hash`.
5. **Load** in one transaction: `COPY` into a temporary table, then one
   `INSERT … ON CONFLICT … DO UPDATE … WHERE row_hash IS DISTINCT FROM EXCLUDED.row_hash`.
   New rows are inserted, changed rows updated, identical rows not touched.
   With `--retransform` the day's rows of the processed lines are deleted first,
   in the same transaction. Any error rolls the whole day back.
6. **Log** one summary line.

### Logs and exit codes

```
INFO transform: date=2026-09-21 status=OK
  read        rows_read=450  rows_after_filter=450  trips_excluded_by_via=0  rows_staged=445
  rows        cancelled=5  pass_through=0  invalid_key=0  duplicate_key=0
  arrival     not_real=17  missing_or_unparseable=0  delay_out_of_range=0  time_order=0
  departure   not_real=1  missing_or_unparseable=0  delay_out_of_range=0  time_order=0
  n/a         arrival=51  departure=50
  load        deleted=0  stale=0  inserted=445  updated=0  unchanged=0
  duration    0.1s
INFO transform: Summary: days=1 ok=1 failed=0
```


* `trips_excluded_by_via`: trips with a selected line name that miss all of
  the line's `via_bpuic` stops (another network with the same name).
* `rows`: rows not stored at all. `arrival` / `departure`: timestamps whose
  delay is stored as NULL, by first failing reason.
* `n/a`: timestamps that do not exist (first stop has no arrival,
  last stop no departure). Not a rejection, but also NULL.
* `stale`: rows already in staging for this day and these lines that the new
  result no longer contains (they are kept; see limitations).
* A second run of the same day shows `inserted=0 updated=0 unchanged=445`.

| Exit code | Meaning |
|---|---|
| 0 | all days transformed |
| 1 | at least one day FAILED (raw file missing or malformed, database error); the other days ran |
| 2 | invalid arguments or configuration, object storage / bucket or database not reachable |

## Staging table `staging.stop_events`

One row per **served** stop of a trip on one operating day. Cancelled stops and
pass-throughs are not stored. Delays are **per observation**. Created by `db/init/01_schemas.sql` (column comments are also stored
in the database: `\d+ staging.stop_events`).

| Column | Type | Meaning |
|---|---|---|
| `betriebstag` | date | operating day (= date of the raw file) |
| `fahrt_bezeichner` | text | trip id, unique within a day |
| `bpuic` | integer | stop id |
| `haltestellen_name` | text | stop name |
| `linien_text`, `betreiber_abk` | text | line and operator, e.g. IR75 / SBB (a line is only unique with its operator) |
| `stop_sequence` | integer | 1-based stop position in the trip, by planned time; counts served stops only, gap-free |
| `sched_arr`, `sched_dep` | timestamp | planned arrival / departure, naive Swiss local time; NULL at the first / last stop |
| `arr_delay_s`, `dep_delay_s` | integer, NULL | actual − planned in seconds, signed (negative = early), unclipped. **NULL = no valid measurement, never 0** |
| `is_additional_trip` | boolean | ZUSATZFAHRT_TF (kept) |
| `row_hash` | text | SHA-256 over all columns above, in this order, as a compact JSON array (timestamps ISO 8601, NULL as `null`) |
| `loaded_at` | timestamptz | last insert or change; unchanged rows keep their value |

Primary key `(betriebstag, fahrt_bezeichner, bpuic)`. Example (trip across midnight):

```
 stop_sequence | haltestellen_name |      sched_arr      |      sched_dep      | arr_delay_s | dep_delay_s
---------------+-------------------+---------------------+---------------------+-------------+-------------
             1 | Zürich HB         |                     | 2026-09-21 23:35:00 |             |          49
             2 | Thalwil           | 2026-09-21 23:45:00 | 2026-09-21 23:45:00 |         -26 |          49
             3 | Baar              | 2026-09-21 23:58:00 | 2026-09-21 23:58:00 |          68 |         164
             4 | Zug               | 2026-09-22 00:02:00 | 2026-09-22 00:03:00 |          85 |         139
             5 | Rotkreuz          | 2026-09-22 00:11:00 | 2026-09-22 00:11:00 |          62 |         145
             6 | Luzern            | 2026-09-22 00:25:00 |                     |         158 |
```

## Validation rules and why

Every arrival and every departure is judged **on its own**. A stop whose
departure is bad still contributes its arrival, and the other way round.

| Rule | Unit | On failure | Why |
|---|---|---|---|
| `FAELLT_AUS_TF = true` | row | row dropped (`cancelled`) | the train did not stop; there is nothing to measure |
| `DURCHFAHRT_TF = true` | row | row dropped (`pass_through`) | not a stop |
| trip id present, `BPUIC` an integer | row | row dropped (`invalid_key`) | the row cannot be identified |
| same `(trip, stop)` twice in a file | row | keep the first by `stop_sequence`, drop the rest (`duplicate_key`), before the time checks | primary key; a dropped copy must not affect its neighbours |
| status (`AN_/AB_PROGNOSE_STATUS`) is exactly `REAL` | timestamp | NULL (`not_real`) | `PROGNOSE` is a forecast, `UNBEKANNT`/empty is unknown: no measured time |
| planned and actual time present and parseable | timestamp | NULL (`missing_or_unparseable`) | a delay needs both |
| \|actual − planned\| ≤ `MAX_ABS_DELAY_S` (3 h) | timestamp | NULL (`delay_out_of_range`) | larger values are almost certainly data errors and would distort statistics |
| same stop: actual arrival ≤ actual departure | both timestamps | both NULL (`time_order`) | a train cannot leave before it arrives |
| next stop of the trip: actual departure(A) < actual arrival(B) and the segment ≤ `MAX_SEGMENT_DURATION_S` (6 h) | both timestamps | both NULL (`time_order`) | trains do not travel backwards or for hours between two stops; we cannot tell which of the two is wrong, so neither is trusted |

* **Early is valid.** Negative delays are kept; there is no "only late counts" rule.
* **Missing is NULL, never 0.** First stops have no arrival, last stops no
  departure. A 0 would claim "on time" and pull the median towards zero.
* Comparisons use full datetimes and durations, never calendar dates: a
  departure at 00:00:44 on the next day is fine.
* Time-order rules are evaluated together on the timestamps that passed the
  per-timestamp checks; the result does not depend on evaluation order.
* Limits are inclusive (exactly 3 h delay or exactly 6 h segment is valid). The
  status is compared after trimming spaces and is case-sensitive (`REAL`).
* Delays are whole seconds (planned times have minute precision, actual times second precision).

## Configuration

Set in `.env` (template: `.env.example`). The `S3_*` variables are shared with the fetcher.

| Variable | Default | Purpose |
|---|---|---|
| `S3_ENDPOINT_URL`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`, `S3_BUCKET` | see fetcher | where the raw files are |
| `S3_BUCKET_WAIT_SECONDS` | 60 | how long to wait for the bucket after storage starts (first run of a fresh clone) |
| `POSTGRES_USER`, `POSTGRES_PASSWORD` | **required** | database credentials (used by the database service *and* the transform) |
| `POSTGRES_DB` | `deng` | database name |
| `POSTGRES_HOST`, `POSTGRES_PORT` | `database`, 5432 | how the transform reaches the database inside the Docker network |
| `POSTGRES_BIND_ADDRESS`, `POSTGRES_HOST_PORT` | `127.0.0.1`, 5432 | where the database is published on your machine for external queries |
| `PGADMIN_DEFAULT_EMAIL`, `PGADMIN_DEFAULT_PASSWORD` | **required** (placeholders in `.env.example`) | pgAdmin login |
| `PGADMIN_PORT` | 5050 | pgAdmin on http://localhost:PORT |
| `TRANSFORM_LINES_FILE` | empty = `transform/config/lines.yml` | another lines file (path inside the container) |
| `TRANSFORM_CHUNK_SIZE` | 100000 | CSV rows per chunk (memory vs. speed) |
| `TRANSFORM_MAX_ABS_DELAY_S` | 10800 | max \|delay\| in seconds |
| `TRANSFORM_MAX_SEGMENT_DURATION_S` | 21600 | max actual travel time between two consecutive stops |
| `TRANSFORM_MEM_LIMIT` | `1g` | hard memory limit of the transform container |

**Selected lines** live in the versioned file `transform/config/lines.yml`. A
line is `linien_text` **and** `betreiber_abk` (e.g. `S1` exists for several
operators). Matching is exact and case-sensitive after trimming spaces.

Even that is not unique when one operator uses the same name in several
networks: SBB runs an S1 around Basel *and* one around Luzern/Zug. The raw data
has no network identifier (`LINIEN_ID` is the train number, different for each
trip; `BETREIBER_ID` is just SBB; `UMLAUF_ID` is empty). Such a line therefore
gets **`via_bpuic`**: stops (BPUIC) the trip must touch. A trip of that line is
kept only if at least one of its rows is at one of these stops, and then the
whole trip with all its stops is kept.

```yaml
lines:
  - linien_text: IR75
    betreiber_abk: SBB
  - linien_text: S1
    betreiber_abk: SBB
    via_bpuic: [8502202]     # Rotkreuz: S1 Luzern/Zug only, never the S1 Basel
```

Pick stops that only the wanted network serves. On the CLI: `S1:SBB@8502202`,
several stops `S1:SBB@8502202+8505000`.
Precedence: `--lines` > `TRANSFORM_LINES_FILE` > `config/lines.yml`. The file is
baked into the image and the repo's copy is mounted over it, so edits apply
without a rebuild.

**Database credentials** only take effect when the `db-data` volume is
created. Changing them later, or changing any `db/init/*.sql`, needs a fresh
database volume: `docker compose down && docker volume rm deng-datapipeline_db-data`
(deletes all database data, keeps the raw files). Do not use `docker compose down -v`:
it also deletes the object storage volume with all raw files.

## Reproducibility

* Same pinning approach as the fetcher: `python:3.12.7-slim-bookworm`,
  `postgres:16.10-bookworm`, `dpage/pgadmin4:9.18.0`, all Python packages incl. transitive ones,
  installed with `--no-deps` + `pip check`. For byte-identical images pin
  digests (`image@sha256:…`).
* The output depends only on the raw file, the lines and the limits. Re-running
  a day gives identical rows and hashes (`unchanged=N`), whatever the chunk size
  or the row order in the file.
* Compare two teams' results with
  `SELECT betriebstag, count(*), md5(string_agg(row_hash, '' ORDER BY fahrt_bezeichner, bpuic)) FROM staging.stop_events GROUP BY 1 ORDER BY 1;`
  The same fingerprint means the same staging content (`loaded_at` excluded).

## Tests

```bash
cd transform
python -m venv .venv && . .venv/bin/activate      # Python 3.12
pip install --no-deps -r requirements-dev.txt && pip check

# unit tests: no Docker needed (read, filter, validation rules, fixture acceptance numbers)
python -m unittest tests.test_transform -v

# end-to-end: real object storage + PostgreSQL + the real CLI
docker compose up -d object-storage database
E2E_S3_ENDPOINT=http://127.0.0.1:8333 E2E_S3_ACCESS_KEY_ID=<from .env> \
E2E_S3_SECRET_ACCESS_KEY=<from .env> E2E_POSTGRES_HOST=127.0.0.1 \
E2E_POSTGRES_PORT=5432 E2E_POSTGRES_USER=<from .env> E2E_POSTGRES_PASSWORD=<from .env> \
E2E_POSTGRES_DB=deng python -m unittest tests.test_e2e -v
```

The e2e tests create their own bucket and database and remove them afterwards.
They cover:

* the acceptance numbers in PostgreSQL
* a second run leaves everything unchanged, `loaded_at` included
* one changed value gives exactly one `updated`
* `--retransform` replaces the day, only for the processed lines
* the line filter on a multi-line file
* stale rows, failures, exit codes and rollback

Expected values are derived from the fixture with the `csv` module, not from
the transform code.

**Memory test** (proves chunked streaming): generate a 3-million-row file of
about 500 MB, comparable to a real day, directly into storage, then transform it
under the 1 GiB container limit:

```bash
docker compose run --rm --entrypoint python transform -m app.seed --synthetic 3000000 --date 2026-09-25
docker compose run --rm transform --date 2026-09-25
# status=OK ... rows_read=3000000 rows_after_filter=75000 ...
```

Measured peak about 360 MiB (control: the same file read as one single chunk
is killed at 1 GiB). The synthetic file is deterministic. Its random delays
deliberately produce some `time_order` rejections.

## Known limitations

* **Naive local timestamps / daylight saving time.** The source has no
  timezone. On **29.03.2026** the hour 02:00–03:00 does not exist, and on
  **25.10.2026** it occurs twice. Delays of trips running during the
  changeover can be off by ±1 h. This is within the 3 h plausibility limit, so
  the delay is stored. Segments that appear to go backwards are rejected as
  `time_order`. Check or exclude these hours in the aggregation if they matter.
* **Corrections by SBB are not detected.** The fetcher does not re-download
  an existing day. To pick up a corrected file:
  `docker compose run --rm fetcher --date D --force-overwrite`, then
  `docker compose run --rm transform --date D --retransform`.
* **Without `--retransform`, rows can go stale.** A stop that disappears from a
  re-fetched file (e.g. now cancelled) stays in staging. The run reports it as
  `stale=N` in the log; `--retransform` removes it.
* **`--retransform` only deletes the processed lines** of the day (so
  `--lines S1:SBB --retransform` never wipes IR75). Consequence: when a line is
  removed from `lines.yml`, its old rows stay in staging. The aggregation should
  filter on the configured lines, or delete them manually:
  `DELETE FROM staging.stop_events WHERE linien_text = 'X' AND betreiber_abk = 'Y';`
* **Line identity via stops.** `via_bpuic` keeps trips that touch the given
  stops. Trips of the same network that never reach any of them are dropped:
  short runs, for example, or a construction-work timetable that bypasses the
  stop. The log line counts them (`trips_excluded_by_via`). Staging has no
  network column, so the reporting step should not select the same
  `linien_text`/`betreiber_abk` twice with different via stops: their medians
  would be merged. After changing a line's `via_bpuic`, run the affected days
  with `--retransform`. Without it, rows of trips that are now excluded stay in
  staging and show up as `stale`.
* The time-order check only compares directly consecutive stored stops. If a
  middle stop has no valid arrival, the segment around it is not checked.
* **Malformed rows.** Rows with too many fields, truncated rows and broken
  encoding fail the whole day; it is never loaded partially. A row whose
  values are shifted so that its line columns no longer match the selected
  lines is filtered out like any other line and cannot be detected.
* The fetcher README documents `;`-separated files; Ist-Daten v2 (and the
  fixture) is `,`-separated. Both are accepted.

# Aggregation: staging → reporting

The last pipeline step. A stored procedure in the existing PostgreSQL
(no extra container) recomputes the data product `reporting.od_delay_by_slot`
from `staging.stop_events`.

```
staging.stop_events ──CALL reporting.refresh_od_delay()──► reporting.od_delay_by_slot
 (one row per trip × stop, seconds)                         (median per line × stop pair × weekday × hour, minutes)
```

## Purpose

Answers "how late is line X from stop A to stop B, on this weekday, in this
hour?" for any origin-destination pair of a line, not only neighbouring stops.
So Zürich HB → Luzern and Zürich HB → Rotkreuz are separate rows. The reverse
direction comes from trips running the other way.

## Table `reporting.od_delay_by_slot`

Created by `db/init/02_reporting_tables.sql` (comments in the database: `\d+ reporting.od_delay_by_slot`).

| Column | Type | Meaning |
|---|---|---|
| `linien_text`, `betreiber_abk` | text | line and operator |
| `from_bpuic`, `from_stop` | integer, text | origin stop (boarding) |
| `to_bpuic`, `to_stop` | integer, text | destination stop (any later stop of the same trip) |
| `weekday` | smallint | ISO weekday of the planned departure at the origin, 1 = Mon … 7 = Sun |
| `slot_hour` | smallint | hour 0..23 of the planned departure at the origin |
| `median_dep_delay_min` | numeric(6,1), NULL | median departure delay at the origin, minutes |
| `median_arr_delay_min` | numeric(6,1), NULL | median arrival delay at the destination, minutes |
| `n_dep_obs`, `n_arr_obs` | integer | number of non-NULL observations behind each median |

Primary key `(linien_text, betreiber_abk, from_bpuic, to_bpuic, weekday, slot_hour)`.
Lookup index on `(linien_text, betreiber_abk, from_stop, to_stop, weekday, slot_hour)`.
CHECKs: weekday 1..7, hour 0..23, counts ≥ 0, at least one count > 0, and each
median is NULL **exactly** when its count is 0.

## Rules

* **Pairs.** For every trip (`betriebstag`, `fahrt_bezeichner`) every ordered
  stop pair (a, b) with `b.stop_sequence > a.stop_sequence`. Origins without a
  planned departure (`sched_dep IS NULL`) give no pairs.
* **Slot.** `weekday` = ISO weekday and `slot_hour` = hour of `a.sched_dep`. The
  slot belongs to the departure at the origin, not to the operating day:
  a trip of Monday's operating day that leaves at 00:06 on Tuesday is in
  weekday 2, slot 0.
* **Median.** `percentile_cont(0.5)` over the signed seconds (mean of the two
  middle values for an even count), divided by 60 and rounded **once** to 0.1,
  half away from zero (`numeric` `round`): 3 s = 0.05 → 0.1, −3 s → −0.1,
  −2.5 s = −0.04 → 0.0 (never −0.0). NULL delays are ignored; a 0 is a real
  "on time".
* **Counts.** One observation per trip and operating day. The departure delay
  at A counts once for every destination B of that trip. No minimum: a median
  can rest on a single observation, so check `n_*_obs` before trusting a value.
* Groups where both counts are 0 are left out.
* **Stop names**: `min()` over the group in byte order (`COLLATE "C"`), so the
  result does not depend on the server's locale.
* All lines in staging are aggregated. The line selection happens in the transform.

## Procedure `reporting.refresh_od_delay()`

Created by `db/init/03_refresh_procedure.sql`.

* **Full recompute, atomic.** `DELETE` + `INSERT … SELECT` in the CALL's own
  transaction, no `COMMIT` inside, no `TRUNCATE`. Readers are never blocked and
  see the old content until the commit. On any error everything is rolled back
  and the old content stays.
* **Idempotent.** Same staging gives the same table, however often it is called.
* **One at a time.** A transaction-level advisory lock (key 8502202) serialises
  calls. A second call waits up to `lock_wait` (default 30 s), then fails with
  SQLSTATE `55P03`, "another refresh is still running", and changes nothing.
  Other waits: `CALL reporting.refresh_od_delay(interval '5 minutes')`, at
  most 24 days. **`interval '0'` means wait without a limit**, as with
  PostgreSQL's `lock_timeout`. The same limit applies to the table locks of the
  DELETE/INSERT, so a conflicting `ALTER TABLE` also makes the call fail rather
  than hang. Session settings of the caller (`lock_timeout`, `search_path`)
  are left unchanged, even inside an explicit transaction.
* **Summary**, as a NOTICE:
  `reporting.refresh_od_delay: rows_deleted=0 rows_written=1914 distinct_pairs=132 duration=0.025s (lock_waited=0.000s)`

### How to call

Once, after the last transformed day of a run (not after every day: it always
recomputes everything):

```bash
docker compose run --rm transform --start-date 2026-09-01 --end-date 2026-09-30
docker compose exec database sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CALL reporting.refresh_od_delay();"'
```

`psql` exits with code 1 if the CALL fails (e.g. the lock wait ran out). Example query:

```sql
SELECT weekday, slot_hour, median_dep_delay_min, median_arr_delay_min, n_dep_obs, n_arr_obs
FROM reporting.od_delay_by_slot
WHERE linien_text = 'S1' AND betreiber_abk = 'SBB' AND from_stop = 'Rotkreuz' AND to_stop = 'Luzern'
ORDER BY weekday, slot_hour;
```

### Fixture acceptance

With the fixture loaded **without the via filter** (`--lines IR75:SBB,S1:SBB`:
445 staging rows / 51 trips), the result is 1914 rows and 132 distinct stop pairs.
`db/tests/acceptance_fixture.sql` checks this and the spot values, and only reads:

```bash
docker compose run --rm --entrypoint python transform \
    -m app.seed --file dataset/excerpt_21092026_istdaten_v2.csv --date 2026-09-21
docker compose run --rm transform --date 2026-09-21 --lines IR75:SBB,S1:SBB
docker compose exec database sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CALL reporting.refresh_od_delay();"'
docker compose exec -T database sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
    < db/tests/acceptance_fixture.sql
# NOTICE:  acceptance OK
```

With the default `transform/config/lines.yml` (IR75 *and* S1 only via
Rotkreuz 8502202), 12 IR75 trips are excluded. Staging then holds 361 rows /
39 trips, and the result is 1662 rows (132 pairs). The acceptance script
expects the first variant.

**Compare two teams' results** (same idea as the staging fingerprint):

```sql
SELECT count(*), md5(string_agg(t::text, '|' ORDER BY linien_text COLLATE "C", betreiber_abk COLLATE "C",
                                from_bpuic, to_bpuic, weekday, slot_hour))
FROM reporting.od_delay_by_slot t;
-- fixture without via filter: 1914 | b9bcbff0e27e4423869448b6f99600fe
```

### Rebuild after a schema change

Tables and the procedure are created **only** by `db/init/*.sql`, which the
Postgres image runs only when the `db-data` volume is empty. After a change to
any of these files, remove **only the database volume**:

```bash
docker compose down
docker volume rm deng-datapipeline_db-data
docker compose up -d database          # init scripts run again
docker compose run --rm transform --start-date … --end-date … # staging is empty again: re-transform
docker compose exec database sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CALL reporting.refresh_od_delay();"'
```

This keeps `storage-data`, so the transform rebuilds staging from the raw files
already in object storage. Never use `docker compose down -v`: it also deletes
`storage-data`, i.e. all raw files.

## Tests

Against a **real PostgreSQL 16** (no mocks). Each test class creates its own
temporary database from all `db/init/*.sql` in sorted order and drops it afterwards.

```bash
cd db
python -m venv .venv && . .venv/bin/activate      # Python 3.12
pip install --no-deps -r requirements-dev.txt && pip check
docker compose up -d database
E2E_POSTGRES_HOST=127.0.0.1 E2E_POSTGRES_PORT=5432 E2E_POSTGRES_USER=<from .env> \
E2E_POSTGRES_PASSWORD=<from .env> E2E_POSTGRES_DB=deng python -m unittest discover -s tests -t . -v
```

| File | What it proves |
|---|---|
| `test_refresh_scenarios.py` | hand-worked medians: odd/even counts, NULLs, negatives, ±0.05 / ±0.15 rounding, −0.04 → 0.0, single-stop trips, origins without `sched_dep`, two lines/operators on one pair, all pairs of a trip, slots 23 and 0 across midnight and the Sunday → Monday change, byte-order stop names |
| `test_refresh_differential.py` | three fixed seeds of random staging data vs. a pure-Python reference (`reference.py`, `Decimal` + `ROUND_HALF_UP`); exact equality including the text of every median (`E2E_DIFF_SEEDS` for other seeds) |
| `test_refresh_behaviour.py` | idempotency, a failure halfway (numeric overflow after the DELETE) keeps the old table, no COMMIT inside, readers not blocked, lock wait and timeout, caller settings untouched, CHECKs, comments, index, init scripts re-runnable, same result in an ICU `en-US` database |
| `test_refresh_performance.py` | about 1 million pair observations (60 days × 160 trips × 15 stops); prints the duration (`E2E_SKIP_PERF=1` skips it) |

Measured: 1,008,000 pair observations → 24,892 rows in 5.5–8 s per call
(several runs, PostgreSQL 16 with default settings). For comparison,
one fixture day gives 1,914 pairs, so a year of IR75 + S1 is about 0.7 million.

## Known limitations

* **Daylight saving time** (single caveat). The slot is the hour of the naive
  local `sched_dep`. On the autumn changeover (25.10.2026) the two 02:00 hours
  fall into one slot. Delays across a changeover can be off by ±1 h (see
  transform limitations). Not corrected.
* **No minimum on `n_*_obs`.** A median from one observation is stored like one
  from a hundred. Consumers decide on a threshold.
* **Full recompute.** Every call rebuilds the whole table from all of staging.
  Cost grows with history × (stops per trip)², about 5–8 s per million pairs
  here. Call it once per run, not per day. Incremental refresh is not implemented.
* **Weekday and slot come from the planned departure** at the origin, not from
  the operating day or the actual time. A late-evening train that leaves after
  midnight belongs to the next weekday.
* Stop names are informational (`min()` per group). The identity is `bpuic`;
  if SBB renames a stop, the alphabetically smaller spelling is shown.
* Medians are limited to ±99,999.9 min by `numeric(6,1)`. A larger value (only
  possible if the transform's 3 h limit is raised a lot) fails the CALL, and
  the old content stays.

# Orchestration (Kestra)

The whole pipeline (fetch → transform → aggregate) runs as a Kestra workflow:
once a day on a schedule, and on demand for reruns and backfills.

```
             Kestra (UI 127.0.0.1:8080)                       Docker network <project>_deng-datapipeline
 daily ──subflow──► backfill ──Docker socket──► fetcher container ──► object-storage (raw/…csv)
 (06:15 Zurich)     │                       └─► transform container ─► database (staging)
                    └──JDBC: CALL reporting.refresh_od_delay()──────► database (reporting)
 kestra-db: Kestra's own metadata (flows, executions), never pipeline data
```

| Piece | What it is |
|---|---|
| `kestra` | `kestra/kestra:v2.0.4`, standalone server (all components in one JVM). UI/API on `127.0.0.1:${KESTRA_UI_PORT}` with basic auth; management port 8081 not published |
| `kestra-db` | `postgres:16.10-bookworm`, Kestra metadata only, volume `kestra-db-data`, no published port, own network `kestra-internal` |
| `orchestrator/flows/backfill.yml` | flow `deng.backfill`: the engine (inputs: date range, mode, force overwrite) |
| `orchestrator/flows/daily.yml` | flow `deng.daily`: schedule, computes yesterday, calls `backfill` as a subflow |
| `orchestrator/kestra/application.yml` | Kestra server configuration, no secrets (placeholders filled from `.env`) |

The flows are versioned here and loaded automatically at every start of the
`kestra` container (`--flow-path`). **The repo is the source of truth**: changes
made in the UI editor are overwritten at the next start. Deleting a file does
not delete the flow in Kestra (delete it in the UI). An invalid flow file is only
logged (`docker compose logs kestra | grep -i "unable to create flow"`), Kestra
still starts.

## Setup and start

```bash
cp .env.example .env                # set the KESTRA_* values (see below)
                                    # existing .env: append the "Orchestrator (Kestra)" block of .env.example
docker compose --profile jobs build # REQUIRED once: builds deng-datapipeline/fetcher:1.0.0 and transform:1.0.0
docker compose up -d                # object-storage, database, kestra-db, kestra
docker compose ps                   # wait until kestra is "healthy" (1–2 min on the first start)
```

Open **http://localhost:8080** and log in with `KESTRA_BASIC_AUTH_USERNAME` /
`KESTRA_BASIC_AUTH_PASSWORD`. Kestra requires an e-mail address as user name and
a password with at least 8 characters, one upper case letter, one lower case
letter and one digit (the default `ChangeMe123` qualifies). With invalid values
Kestra still starts, but the UI shows a "create user" page instead of the login.

The job images are never pulled (they only exist locally). Each run checks for
them first and fails with *"Docker image … not found. Build the job images
first: docker compose --profile jobs build"*. Rebuild after changing fetcher or
transform code.

Under **Flows → namespace `deng`** you find `backfill` and `daily`.

| `.env` variable | Default | Purpose |
|---|---|---|
| `KESTRA_BASIC_AUTH_USERNAME`, `KESTRA_BASIC_AUTH_PASSWORD` | `admin@example.com`, `ChangeMe123` | UI/API login (applied at every start) |
| `KESTRA_UI_PORT` | 8080 | UI port on 127.0.0.1 |
| `KESTRA_DB_USER`, `KESTRA_DB_PASSWORD`, `KESTRA_DB_NAME` | `kestra`, `change-me`, `kestra` | `kestra-db`; only applied when `kestra-db-data` is created |
| `DOCKER_SOCKET` | `/var/run/docker.sock` | host path of the Docker socket (rootless Docker: `/run/user/<uid>/docker.sock`) |
| `PIPELINE_NETWORK` | empty = `<project>_deng-datapipeline` | network the job containers join (`deng-datapipeline_deng-datapipeline` for this repo) |

All other settings of the job containers (`S3_*`, `POSTGRES_*`, `PERMALINK_URL_TEMPLATE`,
`ARCHIVE_URL_TEMPLATE`, `FETCHER_*`, `TRANSFORM_*`) come from the same `.env`
variables as for `docker compose run fetcher/transform`, with the same defaults.

### How the job containers are started

Kestra starts the existing images as one-shot **sibling** containers through the
mounted Docker socket (task type `io.kestra.plugin.docker.cli.Run`):

* **Network:** `PIPELINE_NETWORK`, i.e. the Compose network of this project, so
  the jobs reach `object-storage` and `database` by name. With
  `docker compose -p other …` the default follows the project name
  (`other_deng-datapipeline`), through the `COMPOSE_PROJECT_NAME` interpolation of
  the Compose specification. If your Compose version does not support it, set
  `PIPELINE_NETWORK` explicitly.
* **Host paths:** the daemon resolves bind mounts on the *host*, not inside the
  Kestra container. The setup therefore needs none for the jobs:
  `transform/config/lines.yml` is mounted into the Kestra container
  (`/app/deng/transform-config`, read-only) and copied by Kestra into each
  transform container's working directory (`inputFiles`; `TRANSFORM_LINES_FILE=lines.yml`).
  Edits to the file apply to the next run, without restart or rebuild.
  `TRANSFORM_LINES_FILE` from `.env` is not used by the flows: they always use
  the repo's `transform/config/lines.yml`, or the `lines` input per run. The
  fetcher's archive scratch space is the named volume `<project>_fetcher-scratch`
  (emptied by the fetcher after each run; without it every run would leave an
  anonymous volume behind). The only host path is the Docker socket (`DOCKER_SOCKET`).
* **Working directory:** Kestra runs the container in its own working directory,
  so the flows set `PYTHONPATH=/opt/fetcher` / `/opt/transform`; the images are unchanged.
* **Secrets:** `S3_SECRET_ACCESS_KEY` and `POSTGRES_PASSWORD` reach the jobs as
  Kestra secrets: the `kestra` entrypoint turns the plain `.env` values into
  Kestra's `SECRET_<NAME>` (base64) variables, the flows use `{{ secret('…') }}`
  (masked in logs). Non-secret settings are `ENV_<NAME>` variables, read as
  `{{ envs.<name> }}`. Nothing secret is in the flow files or in git.
* **Memory:** the transform container gets `TRANSFORM_MEM_LIMIT` (default 1g) as in Compose.

## The flows

### `backfill` (the engine)

| Input | Values | Meaning |
|---|---|---|
| `start_date`, `end_date` | YYYY-MM-DD | inclusive range; `end_date >= start_date`, not after today (Europe/Zurich) |
| `mode` | `full` (default), `fetch_only`, `transform_only` | see below |
| `force_overwrite` | false (default) | fetcher `--force-overwrite` (re-download stored days) |
| `lines` | empty (default) | transform `--lines` override, e.g. `IR75:SBB,S1:SBB`; empty = `transform/config/lines.yml` |

| Mode | Steps (one container each, for the whole range) | Changes |
|---|---|---|
| `full` | fetch `--start-date A --end-date B [--force-overwrite]` → transform `--start-date A --end-date B` → `CALL reporting.refresh_od_delay()` | missing raw files fetched; staging upserted; reporting recomputed |
| `fetch_only` | fetch only | raw files only |
| `transform_only` | transform `--retransform` → aggregation | staging of the range rebuilt from the raw files; reporting recomputed; raw files untouched |

`full` upserts without `--retransform`. After a `full` run with
`force_overwrite` (corrected source file), run `transform_only` for the same
range to also remove rows that disappeared from the file (`stale=N` in the
transform log; see the transform's known limitations).

Task order: `label_run` → `check_range`, `check_not_future` (input validation) →
image checks → `fetch` → `fetch_exit_code` → `transform` → `aggregate` →
`summary` (row counts as task output and log line).

**Failure rule:** a step fails if *any* day of the range failed in it (exit
code ≠ 0), the run is then FAILED and later steps do not run. The aggregation
therefore runs only when the transform succeeded for **every** day of the range,
exactly once per run. Days that the transform already committed before another
day failed stay in staging (each day is its own transaction); reporting keeps its
previous content until a successful run.

| Exit code | fetcher | transform |
|---|---|---|
| 0 | task OK | task OK |
| 1 (≥ 1 day FAILED) | task FAILED → **retried**: 5 attempts, 30 min apart (a day may not be published yet; already stored days are SKIPPED on retry, but with `force_overwrite` every retry downloads the whole range again) | task FAILED, no retry |
| 2 (arguments/configuration) | task `fetch_exit_code` fails the run, **no retry** | task FAILED, no retry |

Input validation errors are never retried either. The aggregation retries a
failed `CALL` twice, 1 min apart (e.g. SQLSTATE 55P03 when someone runs the
procedure by hand at the same time); it is a full recompute in one transaction,
so a retry cannot double anything. Retry settings are in the `retry:` blocks in
`backfill.yml` (Kestra does not allow expressions there); restart `kestra` after editing.

Runs are serialised (`concurrency: limit 1, QUEUE`): a manual backfill started
while the daily run is busy waits for it.

### `daily` (schedule)

* Trigger `every_morning`: cron `15 6 * * *` in **Europe/Zurich** (daylight
  saving time handled by Kestra). To change the time, edit `cron` in
  `orchestrator/flows/daily.yml` and run `docker compose up -d --force-recreate kestra`.
* Computes yesterday as the Europe/Zurich calendar day before the scheduled
  date (manual run: before the start time) and calls `backfill` with
  start = end = yesterday, mode `full`. Conversion to the Zurich date happens
  before subtracting the day, so runs shortly after midnight and on DST change days are correct.
* Missed schedules (computer off) are run afterwards, one per missed day
  (`recoverMissedSchedules: ALL`).
* The SBB data of a day is published the next morning; if it is not there yet
  at 06:15, the fetch retries for two hours.

## Running the flows

**UI:** *Flows → deng → backfill → Execute*, fill in the inputs, *Execute*.
The execution page shows the Gantt/Topology view, each task's logs (the
fetcher/transform summary lines) and *Outputs* (`summary.row`, `fetch.vars.exitCode`).

**API** (same credentials; useful for scripts):

```bash
KESTRA="-u admin@example.com:ChangeMe123 http://localhost:8080/api/v1/main/executions/deng"

# full: fetch + transform + aggregate for a range
curl -s $KESTRA/backfill -F start_date=2026-10-01 -F end_date=2026-10-07 -F mode=full

# fetch_only: just get the raw files (e.g. a corrected file: add -F force_overwrite=true)
curl -s $KESTRA/backfill -F start_date=2026-09-01 -F end_date=2026-09-30 -F mode=fetch_only

# transform_only: rebuild staging (--retransform) + reporting from stored raw files
curl -s $KESTRA/backfill -F start_date=2026-09-01 -F end_date=2026-09-30 -F mode=transform_only

# daily, manually (processes yesterday)
curl -s -X POST $KESTRA/daily
```

`curl` prints the execution (its `id`, state `CREATED`); add `?wait=true` to the
URL to wait for the end. Without `-u` the API answers 401.

### Backfill: two ways

**(a) Explicit range with `backfill`** (examples above): one execution, one
fetcher and one transform container for the whole range, one aggregation at the
end, with the mode and `force_overwrite` you choose. Use this for history and reruns.

**(b) Schedule backfill on the `daily` trigger:** *Flows → deng → daily →
Triggers → every_morning → Backfill executions*, choose a start and end date.
Kestra creates one `daily` execution for every 06:15 tick in that period, as if
the schedule had run (trigger date = tick), so each processes the day *before*
its tick (ticks 02.10.–08.10. → days 01.10.–07.10.). Each is a separate `full`
run for a single day with its own aggregation; they queue one after another.
Use it to fill gaps in the daily history exactly as the schedule would have.

## Verification

1. **Started:** `docker compose ps` → `kestra` and `kestra-db` *healthy*;
   http://localhost:8080 asks for the login; `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8080/api/v1/main/flows/search` → `401`.
2. **Flows loaded:** UI *Flows* lists `deng.backfill` and `deng.daily`. If one
   is missing, `docker compose logs kestra | grep -i "unable to create flow"`.
   Flows can be validated with Kestra's own validator:
   `curl -s -u admin@example.com:ChangeMe123 -H 'Content-Type: application/x-yaml' --data-binary @orchestrator/flows/backfill.yml http://localhost:8080/api/v1/main/flows/validate`
   (an empty `constraints` means valid).
3. **Fixture run** (no internet needed):

```bash
   docker compose run --rm --entrypoint python transform \
       -m app.seed --file dataset/excerpt_21092026_istdaten_v2.csv --date 2026-09-21
   curl -s "$KESTRA/backfill?wait=true" -F start_date=2026-09-21 -F end_date=2026-09-21 \
       -F mode=transform_only -F lines=IR75:SBB,S1:SBB
```

   UI: execution SUCCESS; `fetch` tasks *skipped*; `transform` log
   `date=2026-09-21 status=OK … rows_staged=445`; `log_summary`:
   `staging rows in range=445, trips in range=51, reporting.od_delay_by_slot rows=1914`.
   (Without `lines` the default `lines.yml` with the Rotkreuz via filter gives 361 / 39 / 1662.)

   SQL (`docker compose exec database psql -U deng -d deng`):

```sql
   SELECT count(*), count(DISTINCT fahrt_bezeichner) FROM staging.stop_events WHERE betriebstag = '2026-09-21';
   -- 445 | 51
   SELECT count(*), md5(string_agg(t::text, '|' ORDER BY linien_text COLLATE "C", betreiber_abk COLLATE "C",
                                   from_bpuic, to_bpuic, weekday, slot_hour))
   FROM reporting.od_delay_by_slot t;
   -- 1914 | b9bcbff0e27e4423869448b6f99600fe
   SELECT betriebstag, count(*), md5(string_agg(row_hash, '' ORDER BY fahrt_bezeichner, bpuic))
   FROM staging.stop_events GROUP BY 1 ORDER BY 1;   -- staging fingerprint
```

4. **Idempotent:** run the same execution again (*Replay* or the same `curl`):
   both fingerprints are unchanged. (`--retransform` deletes and reinserts the
   day, so `loaded_at` changes; the fingerprints exclude it.)
5. **Failure:** `-F end_date=2026-09-23` (no raw files for 22./23.) → execution
   FAILED at `transform` (`status=FAILED … not found - fetch the day first`),
   `aggregate` not run, reporting unchanged.
6. **Fetch against a test server** instead of SBB: set e.g.
   `PERMALINK_URL_TEMPLATE=http://sbb-mock:8000/{day}_istdaten.csv` (a container
   in the project network) and, for deterministic routing,
   `FETCHER_REFERENCE_DATE=2026-09-25` in `.env`, then
   `docker compose up -d kestra` (settings are read at container start).
   `FETCHER_REFERENCE_DATE` must not be before `end_date` (the fetcher rejects
   later days with exit 2); leave it empty in normal operation.
7. **daily:** *Triggers* tab shows the next date in Europe/Zurich; *Execute*
   once → `log_day` shows the computed day, the subflow `backfill` appears with
   label `triggered_by: daily`.

## Rerun after a failure

* Fix the cause (source not yet published, image missing, credentials …), then
  open the failed execution → **Restart**: Kestra reruns the failed task and
  everything after it, with the same inputs. Or start a new `backfill` with the
  same inputs: every step is idempotent (stored days are SKIPPED, staging is
  upserted, `transform_only` rebuilds, the aggregation recomputes everything).
* After a failed `daily` run, either restart it or run `backfill` for that day.

## Reset only the orchestrator state

Removes flows' execution history, triggers' last dates and Kestra logs; raw
files, staging and reporting stay untouched:

```bash
docker compose rm -sf kestra kestra-db
docker volume rm deng-datapipeline_kestra-db-data deng-datapipeline_kestra-data
docker compose up -d                 # flows are loaded again from orchestrator/flows/
```

`docker compose down` while a job container is running fails with "active
endpoints" on the network: wait for the run to finish (or kill it in the UI).

Never use `docker compose down -v`: it also deletes `storage-data` (all raw
files) and `db-data`. `docker volume rm deng-datapipeline_fetcher-scratch`
removes the (normally empty) fetcher scratch volume.

## Security note: Docker socket

The `kestra` container runs as root with `/var/run/docker.sock` mounted. That is
full control over the host's Docker daemon (equivalent to root on the host):
anyone who can edit and run a flow can start any container with any mount. It is
acceptable for this local course project because the UI is bound to 127.0.0.1,
protected by basic auth and the management port is not published. It is **not**
acceptable for production; there one would use a remote/rootless Docker
endpoint, a Kubernetes task runner or a separate worker with restricted rights,
real secret management and TLS.