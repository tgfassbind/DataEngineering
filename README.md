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

Stop storage with `docker compose down` (data is kept in the `storage-data`
volume; `docker compose down -v` deletes it).

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

Set in `.env` (template: `.env.example`). Nothing is hardcoded in the code.

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