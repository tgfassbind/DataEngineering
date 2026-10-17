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
│
├── db/
│   └── init/01_schemas.sql # schemas
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
created. Changing them later, or changing `db/init/01_schemas.sql`, needs
`docker compose down -v`, which **deletes all database data** (object storage
data is in its own volume and is also deleted by `-v`).

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
