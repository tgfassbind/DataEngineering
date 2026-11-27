# Orchestrator

Kestra runs the pipeline: fetch → transform → load into BigQuery → refresh the data
mart, every morning for the previous day and on demand for any date range. Kestra
runs locally in Docker; the data lives in Google Cloud. It starts the fetcher and
transform jobs on the host and runs the BigQuery steps itself with the GCP plugin.

```
fetch (job)        source ──► gs://…/raw/
transform (job)    raw/ ──► gs://…/processed/  (Parquet)
load_days (Kestra) processed/ ──► core.stg_* ──► 10_load_core.sql ──► core (star schema)
refresh_mart       20_refresh_mart.sql ──► mart.mart_od_delay_by_slot
```

## Folder structure

```
orchestrator/
├── flows/
│   ├── backfill.yml        flow deng.backfill: the pipeline for a date range
│   ├── daily.yml           flow deng.daily: schedule, computes yesterday, calls backfill
│   └── setup_bigquery.yml  flow deng.setup_bigquery: creates the tables once
└── kestra/
    └── application.yml  Kestra server configuration (no secrets; values come from .env)
```

The flows are loaded automatically at every start of the `kestra` container.
**The repo is the source of truth:** changes made in the UI editor are overwritten at
the next start. Deleting a file does not delete the flow in Kestra; delete it in the UI.

## Services

| Service | What it is |
|---|---|
| `kestra` | `kestra/kestra:v2.0.4`, standalone server. UI and API on `127.0.0.1:8080` with login |
| `kestra-db` | `postgres:16.10-bookworm`, Kestra's metadata only, own network, no published port |

Both start with `docker compose up -d`. On the first start Kestra needs 1–2 minutes
until it is healthy.

Mounted into `kestra` (read-only): the flows, `bigquery/sql/` (the SQL the flows run,
at `/app/deng/sql`) and the pipeline service account's key
(`GOOGLE_APPLICATION_CREDENTIALS`).

## Usage

Open <http://localhost:8080> and log in with `KESTRA_BASIC_AUTH_USERNAME` /
`KESTRA_BASIC_AUTH_PASSWORD`. The flows are under **Flows → namespace `deng`**.

- **`setup_bigquery`** is run once by hand after `terraform apply`: it creates the
  tables (`01_tables.sql`) and fills the calendar dimensions (`02_seed_calendar.sql`).
  Safe to rerun.
- **`daily`** runs on its own every day at 06:15 Europe/Zurich.
- **`backfill`** is started by hand with these inputs:

| Input | Values | Effect |
|---|---|---|
| `start_date`, `end_date` | dates, inclusive | range of operating days; not in the future |
| `mode` | `full` (default) | fetch → transform → load → refresh mart |
| | `fetch_only` | fetch only; BigQuery untouched |
| | `transform_only` | transform → load → refresh mart; raw files untouched |
| | `load_only` | load the existing files in `processed/` → refresh mart; nothing recomputed |
| `force_overwrite` | `true` / `false` (default) | re-download days already stored; only in `full` and `fetch_only` |
| `lines` | e.g. `IR75:SBB,S1:SBB@8502202` (optional) | overrides `transform/config/lines.yml` for this run; only in `full` and `transform_only` |

## How a run works

### `daily`

Computes yesterday in Europe/Zurich (correct across daylight saving changes) and
starts `backfill` as a subflow with `start_date = end_date = yesterday` and
`mode = full`. All logic lives in `backfill`.

### `backfill`

Only one execution runs at a time. Further executions (e.g. a manual backfill while
`daily` runs) wait in a queue.

**1. Checks.** Before any data is touched:
- `end_date` is not before `start_date` and not in the future;
- the needed job images are built (otherwise: "Build the job images first");
- `HOST_PROJECT_DIR` points to the repo on the host.

**2. Fetch** (`full`, `fetch_only`). Runs `docker compose run --rm fetcher` with the
whole date range. Depending on the fetcher's exit code:
- 0: continue;
- 1 (at least one day failed): retry every 30 minutes, up to 5 attempts; stored days are skipped;
- 2 (invalid configuration): stop immediately, no retry.

**3. Transform** (`full`, `transform_only`). Runs `docker compose run --rm transform`
with the same range, plus `--lines` if given. It writes
`processed/<day>_stop_events.parquet` and `processed/<day>_run_scope.parquet`.
Any failure stops the flow, so nothing is loaded from an incomplete run.

**4. Load** (all modes except `fetch_only`). The task `days` lists the operating days
of the range; the loop `load_days` then handles one day after the other:

1. `load_stop_events`, `load_run_scope`: `LoadFromGcs` of the two Parquet files into
   `core.stg_stop_event` and `core.stg_run_scope` (`WRITE_TRUNCATE`);
2. `load_core`: `bigquery/sql/10_load_core.sql` merges the dimensions and replaces
   the day's processed lines in `core.fact_stop_event`, in one transaction.

Days run strictly one after the other (`concurrencyLimit: 1`), because the load
tables are shared. Each task is retried up to 3 times (temporary BigQuery errors);
all three are safe to repeat.

**5. Data mart.** `refresh_mart` runs `bigquery/sql/20_refresh_mart.sql` once after
the loop: `mart.mart_od_delay_by_slot` is recomputed in full. Retried up to 3 times.

**6. Summary.** Logs the fact rows and trips in the range and the size of the mart.

### How Kestra starts the jobs

Kestra runs a small `docker` CLI container with access to the host's Docker socket
and the repo, and from there calls `docker compose run` against the project's
`docker-compose.yml`. The jobs therefore start exactly as they would by hand, with
the same images, networks and `.env`.

This requires the repo to be mounted inside Kestra under the **same absolute path**
as on the host. That is why `HOST_PROJECT_DIR` must be set in `.env`.

Multi-line job output is joined into single lines so that it stays readable in the
Kestra log view.

## Configuration

| Variable | Purpose |
|---|---|
| `HOST_PROJECT_DIR` | absolute path of the repo on the host, no trailing slash |
| `KESTRA_BASIC_AUTH_USERNAME`, `KESTRA_BASIC_AUTH_PASSWORD` | UI login; user name must be an e-mail, password ≥ 8 characters with upper/lower case and a digit |
| `KESTRA_UI_PORT` | host port of the UI (127.0.0.1 only) |
| `KESTRA_DB_USER`, `KESTRA_DB_PASSWORD`, `KESTRA_DB_NAME` | Kestra's metadata database |
| `DOCKER_SOCKET` | host path of the Docker socket (rootless Docker: e.g. `/run/user/1000/docker.sock`) |
| `GCS_BUCKET`, `GCP_PROJECT_ID`, `GCP_LOCATION` | bucket, project and location of the BigQuery steps (`envs.gcs_bucket`, `envs.gcp_project_id`, `envs.gcp_location`) |
| `GCP_PIPELINE_KEY_FILE` | host path of the pipeline service account's JSON key, mounted read-only |

After changing any of these in `.env`, recreate the container:

```bash
docker compose up -d --force-recreate kestra
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| UI shows "create user" instead of a login | user name or password do not meet Kestra's rules |
| "Docker image … not found" | `docker compose --profile jobs build` |
| "HOST_PROJECT_DIR … is not the repo on the host" | wrong path in `.env`, then recreate `kestra` |
| "Unable to find `host_project_dir`" | `kestra` was not recreated after changing `.env` |
| flow missing or outdated in the UI | invalid flow file: `docker compose logs kestra \| grep -i "unable to create flow"` |
| "Only files inside the working directory are allowed" | `bigquery/sql` not mounted or `local-files.allowed-paths` missing in `kestra/application.yml`; recreate `kestra` |
| `load_stop_events`: "Not found: URI gs://…" | the day was not transformed: run `transform_only` (or `full`) for it |
| `load_core`: "Table … stg_… was not found" / dimension or fact table missing | run `setup_bigquery` once |
| "Permission denied" / 403 in a BigQuery task | pipeline service account lacks BigQuery Job User or BigQuery Data Editor |

## Design decisions

### Kestra instead of cron jobs

The first idea was plain cron jobs. That quickly became complicated: reruns,
backfills over date ranges, retries and a history of what ran would all have had to
be built by hand. Kestra, introduced in class, provides exactly that, with flows
that are declared in YAML and versioned with the rest of the project.

### Kestra local, data in the cloud

Kestra runs locally in Docker, as in class; only the data lives in Google Cloud.
The pipeline needs no Kestra server in the cloud: it reaches Cloud Storage and
BigQuery through the APIs, with the pipeline service account. The same key is
mounted into the jobs and into Kestra, so all steps act as the same identity with
the same, limited rights (objects and tables, no buckets or datasets).

Kestra keeps its own metadata (flows, executions, logs) in its own PostgreSQL
(`kestra-db`). It is not part of the data pipeline.

### Loading as Kestra tasks, SQL from the repo

Fetch and transform are our own containers; loading and modelling are BigQuery
jobs. They run as Kestra GCP tasks (`LoadFromGcs`, `Query`), as in class, so every
step is visible on its own in the execution: rows loaded per file, the result of
the load script, the mart refresh. The SQL itself stays in `bigquery/sql/` and is
read at run time (`read('file:///app/deng/sql/…')`), so there is one copy of each
statement and it is versioned with the code.

### One day at a time

The loop loads day by day instead of all files at once. A failed day is visible as
such, the load tables always hold exactly one day, and `10_load_core.sql` replaces
exactly that day. The mart is refreshed once at the end, not per day, because it is
recomputed in full anyway.

### `daily` calls `backfill`

The pipeline logic exists once, in `backfill`. `daily` only computes yesterday and
calls it. There is no need to build the same thing twice, a change is made in one
place, and whatever works for a backfill works for the daily run.

### Jobs started via `docker compose run`

Kestra starts the jobs through `docker compose run`, as covered in class. Jobs then
use the same definitions (`docker-compose.yml`, `.env`) as when started by hand.
This requires the repo to be mounted under the same absolute path
(`HOST_PROJECT_DIR`).

### Access to the Docker socket

To start containers, Kestra has access to the host's Docker socket. This effectively
gives it full control over the host's Docker. For a local school project this is
acceptable; in a shared or production environment it would not be.

### Flows in the repo, not in the UI

Flows are loaded from the repo at every start; edits in the UI are overwritten. Every
change is visible in the Git history. Allowing changes in the UI quickly leads to
deviations from the original that nobody remembers making.

### Retries depending on the failure

- **Fetch** is retried when a day failed (exit code 1), because the cause is usually
  temporary: network, or an archive not yet published. It is not retried on invalid
  configuration (exit code 2), because a retry cannot help. To achieve this, the fetch
  task ends successfully on exit code 2 so that Kestra does not retry it, and the next
  task fails the flow with a clear message.
- **Transform** is not retried: its failures are deterministic and would repeat.
- **BigQuery tasks** get a few short retries for temporary API errors. Every one of
  them can be repeated without harm: the loads replace the load tables, the SQL
  scripts are idempotent and run in a transaction.

### One execution at a time

`daily` and manual backfills queue up instead of running in parallel. This is
required, not only convenient: all runs share the load tables `core.stg_*`, and two
runs loading at the same time would overwrite each other's day.

### Checks before the first step

Invalid dates, missing images or a wrong `HOST_PROJECT_DIR` stop the flow at once
with a clear message, before any data is touched, instead of failing halfway with
a cryptic Docker error.

### Schedule

`daily` runs at 06:15 Europe/Zurich for the previous day, so the data mart is up to
date before students check it. This is an initial value and can be changed in
`flows/daily.yml`.