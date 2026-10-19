# Orchestrator

Kestra runs the pipeline: fetch → transform → aggregation, every morning for the
previous day and on demand for any date range. It does not process data itself; it
starts the fetcher and transform jobs and calls the aggregation procedure.

## Folder structure

```
orchestrator/
├── flows/
│   ├── backfill.yml     flow deng.backfill: the pipeline for a date range
│   └── daily.yml        flow deng.daily: schedule, computes yesterday, calls backfill
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

## Usage

Open <http://localhost:8080> and log in with `KESTRA_BASIC_AUTH_USERNAME` /
`KESTRA_BASIC_AUTH_PASSWORD`. Both flows are under **Flows → namespace `deng`**.

- **`daily`** runs on its own every day at 06:15 Europe/Zurich.
- **`backfill`** is started by hand with these inputs:

| Input | Values | Effect |
|---|---|---|
| `start_date`, `end_date` | dates, inclusive | range of operating days; not in the future |
| `mode` | `full` (default) | fetch → transform (upsert) → aggregation |
| | `fetch_only` | fetch only; staging and reporting untouched |
| | `transform_only` | transform with `--retransform` → aggregation; raw files untouched |
| `force_overwrite` | `true` / `false` (default) | re-download days already stored; ignored in `transform_only` |
| `lines` | e.g. `IR75:SBB,S1:SBB@8502202` (optional) | overrides `transform/config/lines.yml` for this run |

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
with the same range, plus `--retransform` in `transform_only` and `--lines` if given.
Any failure stops the flow, so the aggregation does not run on incomplete data.

**4. Aggregation** (`full`, `transform_only`). Calls
`reporting.refresh_od_delay()` directly over JDBC. Retried up to 3 times.

**5. Summary.** Logs the staging rows and trips in the range and the size of the
reporting table.

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

The `POSTGRES_*` values of the pipeline database are passed to Kestra for the
aggregation step.

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

## Design decisions

### Kestra instead of cron jobs

The first idea was plain cron jobs. That quickly became complicated: reruns,
backfills over date ranges, retries and a history of what ran would all have had to
be built by hand. Kestra, introduced in class, provides exactly that, with flows
that are declared in YAML and versioned with the rest of the project.

### Separate database for Kestra

Kestra's metadata (flows, executions, logs) lives in its own PostgreSQL, not in the
pipeline database. A change to or failure of one system does not affect the other:
resetting the pipeline database keeps the Kestra history, and the other way round.

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
- **Aggregation** gets a few short retries, e.g. if another refresh still holds the lock.

### One execution at a time

`daily` and manual backfills queue up instead of running in parallel. The jobs
themselves have guardrails against concurrent runs (locks in the transform and the
aggregation), but a serial order is easier to follow: better safe than sorry.

### Checks before the first step

Invalid dates, missing images or a wrong `HOST_PROJECT_DIR` stop the flow at once
with a clear message, before any data is touched, instead of failing halfway with
a cryptic Docker error.

### Schedule

`daily` runs at 06:15 Europe/Zurich for the previous day, so the reporting is up to
date before students check it. This is an initial value and can be changed in
`flows/daily.yml`.