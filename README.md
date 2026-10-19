# Data Engineering (DENG) – Data Pipeline

Course project for **Data Engineering (DENG)** at HSLU, group 01.

Commuting students at the Rotkreuz campus want to know how late their train
usually is. This pipeline processes the SBB actual data ("Ist-Daten",
Open Transport Data Swiss) for selected lines and produces one data product:
the **median departure and arrival delay per line, origin–destination stop pair,
weekday and one-hour slot** (`reporting.od_delay_by_slot`).

The pipeline currently runs locally in Docker. Follow the setup below to get it running.

> **About this documentation**
> This README covers what the project does and how to run it. Each component has
> its own README that explains how it works and why it was built that way:
> architecture decisions, processing logic and the alternatives considered.
> See the [component overview](#pipeline-overview) for the links.

![Architecture](docs/Architecture_V02.svg)

## Team

| Name | Focus |
|---|---|
| Til Fassbind | Transform, staging/reporting, orchestration |
| Alexander Türler | Fetcher, object storage |

## Pipeline overview

| Component | Responsibility | Docs |
|---|---|---|
| Fetcher Container | Downloads one CSV per operating day (permalink or monthly archive) and stores it unchanged | [fetcher/README.md](fetcher/README.md) |
| Object Storage | S3-compatible storage for raw files (SeaweedFS) | [docs/object-storage.md](docs/object-storage.md) |
| Transform Container | Filters the configured lines, validates timestamps, computes delays, upserts into staging | [transform/README.md](transform/README.md) |
| Aggregation | Stored procedure `reporting.refresh_od_delay()`, recomputes the data product | [db/README.md](db/README.md) |
| Orchestrator Container | Kestra with a scheduled `daily` flow and a parameterised `backfill` flow | [orchestrator/README.md](orchestrator/README.md) |

## Project structure

```
.
├── dataset/                 sample excerpt of the Ist-Daten
├── db/
│   └── init/                schemas, staging/reporting tables, stored procedure
├── docs/                    use case, project plan, architecture diagrams
├── fetcher/                 fetcher job (Python CLI, Dockerfile)
├── orchestrator/
│   ├── flows/               Kestra flows: daily.yml, backfill.yml
│   └── kestra/              Kestra server configuration (application.yml)
├── transform/               transform job (Python CLI, line config, Dockerfile)
├── .env.example             template for all configuration values
├── docker-compose.yml       all services
└── README.md
```

## Setup

### Prerequisites

- Docker Engine with Docker Compose v2 (`docker compose version`)
    - (Windows and macOS see: https://docs.docker.com/desktop/)
    - (Linux see: https://docs.docker.com/engine/install/)
- Git (see: https://git-scm.com/downloads)
- Internet access (the fetcher downloads from data.opentransportdata.swiss)
- Free disk space: about 650 MB per operating day for the raw files, plus room for
  one monthly archive ZIP (about 1.5 GB) while a past month is fetched.
  The end-to-end verification needs about 4.4 GB plus the archive.

### 1. Clone
#### via SSH

```bash
git clone git@github.com:tgfassbind/DataEngineering.git
cd DataEngineering
```

#### via HTTPS
```bash
git clone https://github.com/tgfassbind/DataEngineering.git
cd DataEngineering
```

### 2. Configure

```bash
cp .env.example .env
```

Set at least:

| Variable | Why |
|---|---|
| `HOST_PROJECT_DIR` | Absolute path of this repo on the host (`pwd`). Kestra starts the jobs with `docker compose run` and needs the repo mounted under the identical path. |
| `KESTRA_BASIC_AUTH_USERNAME` / `KESTRA_BASIC_AUTH_PASSWORD` | Kestra login. The user name must be an e-mail address. The password needs ≥ 8 characters with upper case, lower case and a digit. |
| `POSTGRES_PASSWORD` | Password of the pipeline database. |

All other values have working defaults and are explained in `.env.example`.

### 3. Build and start

```bash
docker compose --profile jobs build   # builds the fetcher and transform images (required once and after code changes)
docker compose up -d                  # object-storage, database, kestra-db, kestra
docker compose ps                     # wait until everything is healthy
```

Fetcher and transform are jobs (profile `jobs`). They do not run permanently.
Kestra or you start them on demand.

### 4. Load data

**Option A – Kestra**

1. Open <http://localhost:8080> and log in.
2. Go to **Flows → namespace `deng` → `backfill`** and execute it with a date range,
   e.g. `2026-09-01` to `2026-09-30`.
3. Each day runs fetch → transform, then the aggregation refreshes the reporting table.

From then on, the `daily` flow processes yesterday's data every morning (06:15 Europe/Zurich).

**Option B – run the jobs by hand**

```bash
docker compose run --rm fetcher --start-date 2026-09-01 --end-date 2026-09-30
docker compose run --rm transform --start-date 2026-09-01 --end-date 2026-09-30
docker compose exec database psql -U deng -d deng -c "CALL reporting.refresh_od_delay();"
```

or for one day

```bash
docker compose run --rm fetcher --date 2026-09-30
docker compose run --rm transform --date 2026-09-30
docker compose exec database psql -U deng -d deng -c "CALL reporting.refresh_od_delay();"
```

#### Re-running and forcing

All steps are safe to re-run. By default nothing is loaded twice:

| Step | Default on re-run | Force |
|---|---|---|
| Fetcher | Days already in object storage are skipped (`SKIPPED`) | `--force-overwrite` downloads the day again and replaces the raw file |
| Transform | Upsert by row hash: only new or changed rows are written | `--retransform` deletes the day (for the configured lines) and loads it from scratch |
| Aggregation | Always recomputes the whole reporting table | – |

Examples:

```bash
# source data for a day was corrected: fetch again, then rebuild staging
docker compose run --rm fetcher --date 2026-09-30 --force-overwrite
docker compose run --rm transform --date 2026-09-30 --retransform
docker compose exec database psql -U deng -d deng -c "CALL reporting.refresh_od_delay();"

# transform logic or line config changed: raw files are fine, only rebuild staging
docker compose run --rm transform --start-date 2026-09-01 --end-date 2026-09-30 --retransform
```

In Kestra, the `backfill` flow covers the same cases through its inputs:

| Input | Values | Effect |
|---|---|---|
| `mode` | `full` (default) | fetch → transform (upsert) → aggregation |
| | `fetch_only` | fetch only; staging and reporting stay untouched |
| | `transform_only` | transform with `--retransform` → aggregation; raw files stay untouched |
| `force_overwrite` | `true` / `false` (default) | passes `--force-overwrite` to the fetcher; ignored in `transform_only` |
| `lines` | e.g. `IR75:SBB,S1:SBB` (optional) | overrides `transform/config/lines.yml` for this run |

To fully rebuild a day after the source data was corrected, run `fetch_only` with
`force_overwrite: true`, then `transform_only` for the same range.

### 5. Check the result

```bash
docker compose exec database psql -U deng -d deng -c "
  SELECT from_stop, to_stop, weekday, slot_hour,
         median_dep_delay_min, median_arr_delay_min, n_arr_obs
  FROM reporting.od_delay_by_slot
  WHERE from_stop = 'Rotkreuz'
  ORDER BY weekday, slot_hour
  LIMIT 10;"
```

PostgreSQL is also published on `127.0.0.1:5432` for any SQL client. An optional
pgAdmin is available with `docker compose --profile tools up -d`.

## Verification

Two checks confirm that your setup produces exactly the same data as ours.
The reference values depend on the code in `fetcher/`, `transform/`
(including `config/lines.yml`), `db/init/` and `orchestrator/flows/`.
If any of these change, the values have to be regenerated.

### 1. Stack

```bash
docker compose ps
```

All services are `healthy` or `running`. In Kestra (<http://localhost:8080>) you can
log in and see the flows `daily` and `backfill` in namespace `deng`.

### 2. Processing logic (fixture, offline)

Runs the transform and the aggregation on the sample day in `dataset/`
(21.09.2026), without downloading anything.

> This needs **empty volumes**. The fixture is stored as the raw file for
> 2026-09-21 and would later be skipped by the fetcher instead of being replaced
> by the real day; the aggregation would also include any other loaded days.
> Run it on a fresh setup, or start with `docker compose down -v`
> (deletes all raw files, database data and Kestra history).

```bash
docker compose --profile jobs build
docker compose up -d object-storage database

# store the fixture as raw file for 2026-09-21
docker compose run --rm --entrypoint python transform \
    -m app.seed --file dataset/excerpt_21092026_istdaten_v2.csv --date 2026-09-21

# transform with the default lines (config/lines.yml) and aggregate
docker compose run --rm transform --date 2026-09-21
docker compose exec database psql -U deng -d deng -c "CALL reporting.refresh_od_delay();"
```

Compare with the reference values:

| Check | Where | Expected |
|---|---|---|
| rows read / after line filter | transform log, `read` | `rows_read=450  rows_after_filter=366` |
| trips excluded by via stops | transform log, `read` | `trips_excluded_by_via=12` |
| cancelled rows | transform log, `rows` | `cancelled=5` |
| rows loaded | transform log, `load` | `inserted=361` |
| reporting rows / stop pairs | aggregation NOTICE | `rows_written=1662  distinct_pairs=132` |

```bash
docker compose exec database psql -U deng -d deng -c "
  SELECT count(*)                         AS rows,
         count(DISTINCT fahrt_bezeichner) AS trips,
         count(arr_delay_s)               AS arr_delays,
         count(dep_delay_s)               AS dep_delays
  FROM staging.stop_events;"
```

```
 rows | trips | arr_delays | dep_delays
------+-------+------------+------------
  361 |    39 |        311 |        322
```

```bash
docker compose exec database psql -U deng -d deng -c "
  SELECT count(*)                               AS reporting_rows,
         count(DISTINCT (from_bpuic, to_bpuic)) AS stop_pairs
  FROM reporting.od_delay_by_slot;"
```

```
 reporting_rows | stop_pairs
----------------+------------
           1662 |        132
```

**Idempotency:** run the transform a second time.

```bash
docker compose run --rm transform --date 2026-09-21
```

The `load` line must read `inserted=0  updated=0  unchanged=361`.

Afterwards, reset with `docker compose down -v` before loading real data.

### 3. End to end (real data)

Loads a fixed, closed week through Kestra. Past months come from the monthly
archive, so every team downloads the same files.

> Needs about 4.4 GB for the raw files plus space for the temporary monthly
> archive, and starts from empty volumes (`docker compose down -v`).

```bash
docker compose down -v
docker compose --profile jobs build
docker compose up -d
```

In Kestra, execute `backfill` with `start_date = 2026-09-01`, `end_date = 2026-09-07`,
`mode = full`, `force_overwrite = false`, `lines` empty. The last log line must read:

```
Run OK (full, 2026-09-01..2026-09-07): staging rows in range=19244, trips in range=1217, reporting.od_delay_by_slot rows=87100
```

Run the checks right after the backfill; the `daily` flow adds the previous day
at 06:15 and changes the totals.

**Raw files**

```bash
docker compose run --rm --entrypoint python fetcher -m app.inventory
```

```
key                                         bytes  etag
raw/2026-09-01_istdaten.csv             670052892  3a4917041b884243e88a3e33677553de-80
raw/2026-09-02_istdaten.csv             672336538  4aa0f5668d12b7437e79de8ff8e48e9f-81
raw/2026-09-03_istdaten.csv             671958240  6f5aa18587170f6fee6e9f4abaf2feea-81
raw/2026-09-04_istdaten.csv             679017602  000e284fdc34ee7a676f1a9647cdda60-81
raw/2026-09-05_istdaten.csv             555708040  5246af815ef8ce99bc4f1e8a0f0c6044-67
raw/2026-09-06_istdaten.csv             434998310  46d0c2a56aa7787976b65603eaf52824-52
raw/2026-09-07_istdaten.csv             667246777  da92960d0abe370e20cbfaeeb77dc80b-80
7 object(s), 4351318399 bytes in s3://sbb-ist-daten/raw/
```

The ETag of a multipart upload depends on the part size used by the fetcher; it is
reproducible with this fetcher, but it is not the MD5 of the file.

**Staging, fingerprint per day**

```bash
docker compose exec database psql -U deng -d deng -c "
  SELECT betriebstag, count(*) AS rows,
         md5(string_agg(row_hash, '' ORDER BY fahrt_bezeichner, bpuic)) AS fingerprint
  FROM staging.stop_events
  GROUP BY 1 ORDER BY 1;"
```

```
 betriebstag | rows |           fingerprint
-------------+------+----------------------------------
 2026-09-01  | 2842 | 83f8e00d175f20725fe04db02beed57a
 2026-09-02  | 2857 | 6d9dfc55fe0f2e1a79596ebf034e293b
 2026-09-03  | 2858 | 849996fd85bf3497881f042a426ac3ec
 2026-09-04  | 2862 | cd1d34e7745675a8d79e258aafa29d35
 2026-09-05  | 2754 | f1db7bb7c86111508a08a7b99dd6df9f
 2026-09-06  | 2205 | 2cba9ec99f680e6190b3d0706ee552e3
 2026-09-07  | 2866 | 75ca9e1783e40ace883c75c71ef1f08e
```

The same fingerprint means the same content in staging, row by row
(`loaded_at` excluded).

**Reporting**

```bash
docker compose exec database psql -U deng -d deng -c "
  SELECT count(*)                               AS reporting_rows,
         count(DISTINCT (from_bpuic, to_bpuic)) AS stop_pairs
  FROM reporting.od_delay_by_slot;"
```

```
 reporting_rows | stop_pairs
----------------+------------
          87100 |        671
```

If a check differs, the first layer that differs (raw → staging → reporting) shows
where to look.

### Reset the database

The scripts in `db/init` only run when the database volume is empty. After
changing them, or the database credentials, recreate the volume:

```bash
docker compose down
docker volume rm deng-datapipeline_db-data
docker compose up -d
```

> Do **not** use `docker compose down -v`. It also deletes the object storage
> volume with all downloaded raw files and the Kestra history.

## Use of AI

We used Claude (Anthropic) as an assistant throughout the project.

**What the AI did**
- wrote parts of the code of the fetcher, the transform, the aggregation
  procedure and the Kestra flows
- reviewed and explained code on request
- drafted the documentation

**What we did**
- defined the use case, the data product and the architecture, and made all design
  decisions
- wrote the specifications for each component
- reviewed the generated code, went through it file by file until we understood it,
  and applied changes to the repository ourselves
- tested the pipeline on our machines with real data and reported bugs found there
  (e.g. the S1 line appearing in two networks)
- reviewed and adapted the documentation; the design decisions describe our reasoning

We are responsible for the content of this repository, including the parts written
with AI support.