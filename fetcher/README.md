# Fetcher

Batch job that downloads the SBB actual data ("Ist-Daten", Open Transport Data Swiss)
for one or more operating days and stores each day **unchanged** in object storage.
It is the first step of the pipeline; the transform job reads its output.

## Folder structure

```
fetcher/
├── app/
│   ├── __main__.py      entry point for `python -m app` (container command)
│   ├── cli.py           argument parsing, date validation, summary, exit codes
│   ├── config.py        reads all settings from environment variables, storage key contract
│   ├── routing.py       pure logic: which source (permalink / archive) for which date
│   ├── fetch.py         per-date processing: existence check, download, ZIP extraction, upload
│   ├── storage.py       thin wrapper around the S3 API (boto3)
│   └── inventory.py     lists the stored raw files (key, size, ETag)
├── tests/
│   ├── test_routing.py  unit tests of the routing rules (no I/O)
│   └── test_e2e.py      runs the real CLI against an S3 emulator (moto) and mocked SBB sources
├── Dockerfile           python:3.12.7-slim-bookworm, pinned dependencies
├── requirements.txt     runtime dependencies incl. pinned transitive packages
└── requirements-dev.txt test-only dependencies
```

## Usage

The fetcher is a job (Compose profile `jobs`): it runs, processes the requested
days and exits. In normal operation Kestra starts it. By hand:

```bash
docker compose run --rm fetcher --date 2026-09-30
docker compose run --rm fetcher --start-date 2026-09-01 --end-date 2026-09-30
docker compose run --rm fetcher --date 2026-09-30 --force-overwrite
```

| Argument | Meaning |
|---|---|
| `--date YYYY-MM-DD` | one operating day |
| `--start-date` / `--end-date` | inclusive date range |
| `--force-overwrite` | download again and replace days that are already stored |

List what is stored:

```bash
docker compose run --rm --entrypoint python fetcher -m app.inventory
```

## How it works

Each date in the request is processed on its own. A failing date does not stop the others.

### 1. Validate the request

Dates after "today" are rejected (exit code 2). "Today" is the current date in
`FETCHER_TIMEZONE` (default Europe/Zurich).

### 2. Existence check

Before anything is downloaded, the fetcher checks whether
`raw/YYYY-MM-DD_istdaten.csv` already exists:

| Object exists? | `--force-overwrite` | Result |
|---|---|---|
| no | – | download → `FETCHED` |
| yes | no | `SKIPPED`, the source is not contacted |
| yes | yes | download → `OVERWRITTEN` |
| error | – | `FAILED`, the other dates continue |

### 3. Choose the source

| Requested date | Source |
|---|---|
| in the current calendar month | daily permalink (`…/ist-daten-v2/resource_permalink/YYYY-MM-DD_istdaten.csv`) |
| today is the 1st and the date is yesterday | daily permalink |
| any other date in an earlier month | monthly archive ZIP (`…/istdaten/YYYY/ist-daten-YYYY-MM.zip`) |

A date range across a month boundary therefore uses both sources.

### 4. Download and store

- **Permalink:** the CSV is streamed directly into object storage, without temp files.
- **Archive:** the ZIP is downloaded once per month into a scratch volume
  (`/tmp/fetcher`). Only the requested days (`YYYY-MM-DD_istdaten.csv` at the ZIP root)
  are extracted and uploaded. The scratch directory is removed afterwards, on success
  and on failure. If all days of a month are skipped, the ZIP is not downloaded.

Before uploading, the first line is checked for the header `BETRIEBSTAG`. An HTML
error page delivered with HTTP 200 is therefore never stored as data.

### Storage contract

What the transform job can rely on:

- Key `raw/YYYY-MM-DD_istdaten.csv`, one object per operating day.
- Content is the original CSV, byte for byte, no filtering.
- The source (permalink or archive) is not visible in the key.
- An object exists completely or not at all. A failed `--force-overwrite` keeps the old object.
- `Content-Type: text/csv`.

### 5. Logs and exit codes

One line per date and a summary:

```
INFO app.fetch: date=2026-09-30 source=archive status=FETCHED 37200058 bytes from 2026-09-30_istdaten.csv -> raw/2026-09-30_istdaten.csv
INFO app.fetch: date=2026-10-01 source=permalink status=SKIPPED raw/2026-10-01_istdaten.csv already exists
INFO fetcher: Summary: fetched=1, skipped=1, overwritten=0, failed=0
```

| Exit code | Meaning |
|---|---|
| 0 | all dates `FETCHED`, `OVERWRITTEN` or `SKIPPED` |
| 1 | at least one date `FAILED`; safe to rerun, finished dates are skipped |
| 2 | invalid arguments or configuration (e.g. bucket missing) |

Kestra uses these codes: on 1 it retries the fetch, on 2 it stops without retry.

## Configuration

All settings come from environment variables (`.env`, template `.env.example`).
Nothing is hardcoded.

| Variable | Purpose |
|---|---|
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` | storage credentials, used by the storage service and the fetcher |
| `S3_ENDPOINT_URL` | storage endpoint; empty = AWS S3 |
| `S3_BUCKET`, `S3_REGION` | bucket (default `sbb-ist-daten`) and region |
| `S3_CREATE_BUCKET` | let the fetcher create a missing bucket; keep `false` locally (the storage service creates it) |
| `S3_BUCKET_WAIT_SECONDS` | how long the fetcher waits for the bucket after storage starts |
| `S3_API_PORT` | host port of the S3 API (127.0.0.1 only) |
| `PERMALINK_URL_TEMPLATE`, `ARCHIVE_URL_TEMPLATE` | source URLs |
| `HTTP_TIMEOUT_SECONDS` | timeout per request |
| `FETCHER_TIMEZONE` | defines "today" and "current month" |
| `FETCHER_REFERENCE_DATE` | leave empty; see design decisions |

## Tests

No Docker or internet needed:

```bash
cd fetcher
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m unittest discover -s tests -t . -v
```

- `test_routing`: routing rules, incl. month boundaries and the first-of-month rule.
- `test_e2e`: the real CLI against moto and mocked SBB sources: skip/overwrite,
  failure isolation, temp cleanup, bucket handling, inventory, exit codes.

## Design decisions

### Archive or permalink: as few calls as possible

The fetcher aims to keep the number of requests to the source low:

- **Past months come from the archive.** One request per month delivers every day
  of that month. Fetching the same days via the permalink would need up to 31
  requests.
- **Stored days cause no request.** The existence check runs before any download
  (see below). If all requested days of a month are already stored, the ZIP is not
  downloaded at all.
- **One ZIP per call.** Within one call each monthly ZIP is downloaded only once,
  however many of its days are requested.

The price is volume: a date in an earlier month downloads the whole ZIP, even with
`--date` for a single day. We accept this because a backfill usually covers most of
a month, and Kestra passes the whole range in one call. Avoiding it would need an
extra parameter that changes when the permalink and when the archive is used, which
makes the routing harder to understand and to test. The expensive case is fetching
the same past month in many separate single-day calls; don't do that.

The permalink also serves days several months back, so using the archive for past
months is a choice, not a constraint: we trade download volume for fewer requests.

### First day of the month

The archive of the previous month is only published by the first working day of
the new month. On the 1st, "yesterday" is therefore still fetched from the
permalink, so the daily run never depends on the archive.

**Known limitation:** from the 2nd of a month until the archive is published, all
other days of the previous month route to the archive. If it is not online yet,
these days fail with a clear log line and are not stored. Rerun them later; days
that were fetched in the meantime are skipped.

### Raw data stored unchanged

The fetcher stores the CSV byte for byte as delivered: no filtering, no parsing,
no reformatting. The interface between fetcher and transform is the original CSV
under a fixed key (`raw/YYYY-MM-DD_istdaten.csv`).

This keeps both components independent. The fetcher only has to get the file
right; the transform owns every decision about content (lines, validation, delays).
A change to the transform logic never requires fetching again: the raw files are
the source of truth and staging can always be rebuilt from them.

### No overwrite by default

Before downloading, the fetcher checks whether the day is already stored and skips
it if so. Reruns and overlapping backfills are therefore safe and cheap, and a
failed run can simply be repeated: finished days are skipped.

We assume published raw data rarely changes. If it does, `--force-overwrite`
replaces specific days on purpose, followed by a transform with `--retransform`
(in Kestra: `fetch_only` with `force_overwrite`, then `transform_only`).
Overwriting is never the default because it replaces the original record.

It also saves requests: a skipped day never contacts the source.

### Header check before upload

When something goes wrong, the source can answer with an HTML page and HTTP 200.
Without a check, that page would be stored as raw data and, because of the
existence check above, never replaced. The fetcher therefore verifies that the
first line starts with `BETRIEBSTAG` before uploading; otherwise the day fails
and nothing is stored.

### All or nothing in storage

An object only becomes visible once its upload is complete. A failed
`--force-overwrite` keeps the old object. Together with the existence check this
guarantees: whatever is in storage is a complete file, and an interrupted run never
leaves a partial file behind that would then be skipped.

### Failures isolated per day, exit codes as contract

Each date is processed on its own; one failing day does not stop the others.
The exit code tells the orchestrator what kind of failure happened:

- **1** – at least one day failed, typically a temporary problem (network, archive
  not yet published). Kestra retries; finished days are skipped.
- **2** – invalid arguments or configuration (credentials, bucket, dates). A retry
  cannot help, so Kestra stops immediately instead of waiting through its retries.

### Streaming for the permalink, scratch volume for the archive

A single day from the permalink is streamed directly into storage, without
touching the disk. The archive cannot be streamed this way: reading a ZIP needs
random access, so it is downloaded into a scratch volume (`/tmp/fetcher`) first.
The volume is anonymous and the directory is removed after every run, on success
and on failure, so disk usage never accumulates across runs.

### Object storage behind the S3 API

Raw files go to S3-compatible object storage, accessed only through the S3 API
(boto3). Locally this is a SeaweedFS container. Later in the project the storage
moves to a cloud provider (AWS S3): this only needs changes in `.env`
(empty `S3_ENDPOINT_URL`, real credentials and region), not in the code.

The bucket belongs to the storage service, not to the fetcher. Locally the storage
service creates it and the fetcher only waits for it (`S3_BUCKET_WAIT_SECONDS`).
`S3_CREATE_BUCKET=true` exists for cloud setups where the bucket is not provisioned
in advance.

### A job, not a service

The fetcher runs, processes the requested days and exits (Compose profile `jobs`,
always with `--rm`). It keeps no state of its own: everything it needs to know is
in the arguments and in object storage. This matches the batch nature of the data
(one file per day) and lets Kestra start it like any other command.

### Reproducibility

Another team should end up with the same raw files. Therefore:

- the base image is pinned (`python:3.12.7-slim-bookworm`);
- all Python packages are pinned, including transitive ones, and installed with
  `--no-deps` followed by `pip check`;
- all configuration comes from `.env`;
- `python -m app.inventory` lists every stored file with size and ETag, so two teams
  can compare their results directly.

The upstream data is outside our control: a day can only be fetched while it is
published. The object storage is the reproducible snapshot.

### `FETCHER_REFERENCE_DATE`

Routing depends on "today", so the same command behaves differently depending on
the day it runs. `FETCHER_REFERENCE_DATE` pins "today" to a fixed date. This makes
the routing rules (month boundaries, the 1st of a month) testable on any day and
lets us repeat the routing decisions of a past run. It does not bring back data
that is no longer published. In normal operation it stays empty; when set, the
first log line says so.