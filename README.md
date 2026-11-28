# Data Engineering (DENG) – Data Pipeline

Course project for **Data Engineering (DENG)** at HSLU, group 01.

Commuting students at the Rotkreuz campus want to know how late their train
usually is. This pipeline processes the SBB actual data ("Ist-Daten",
Open Transport Data Swiss) for selected lines and produces one data product:
the **median departure and arrival delay per line, origin–destination stop pair,
weekday and one-hour slot** (`mart.mart_od_delay_by_slot` in BigQuery).

The data lives in Google Cloud (Cloud Storage as data lake, BigQuery as warehouse),
provisioned with Terraform. The jobs and the orchestrator (Kestra) run locally in
Docker. Follow the setup below to run it in your own Google Cloud project.

> **About this documentation**
> This README covers what the project does and how to run it. Each component has
> its own README that explains how it works and why it was built that way:
> processing logic, design decisions and the alternatives considered.
> See the [component overview](#pipeline-overview) for the links.

![Architecture v1.0](docs/Architecture_V10.svg)

## Team

| Name | Focus |
|---|---|
| Til Fassbind | Transform, data model and BigQuery, orchestration |
| Alexander Türler | Fetcher, data lake |

## Pipeline overview

```
SBB Ist-Daten ──fetch──► gs://<bucket>/raw/            data lake, original daily files
              ──transform──► gs://<bucket>/processed/  validated rows (Parquet)
              ──load──► BigQuery core                  star schema
              ──refresh──► BigQuery mart               data product
```

| Component | Responsibility | Docs |
|---|---|---|
| Terraform | Creates the bucket and the BigQuery datasets `core` and `mart` | [terraform/README.md](terraform/README.md) |
| Fetcher | Downloads one CSV per operating day (permalink or monthly archive) and stores it unchanged in `raw/` | [fetcher/README.md](fetcher/README.md) |
| Transform | Filters the configured lines, validates timestamps, computes delays, writes Parquet to `processed/` | [transform/README.md](transform/README.md) |
| BigQuery SQL | Star schema (fact and dimensions), data mart, data-quality checks | [bigquery/README.md](bigquery/README.md) |
| Orchestrator | Kestra: scheduled `daily` flow, parameterised `backfill` flow, one-time `setup_bigquery` | [orchestrator/README.md](orchestrator/README.md) |

Cross-cutting documentation:

| Topic | Docs |
|---|---|
| Data model: grain of every table, keys, partitioning, design decisions | [docs/data_model.md](docs/data_model.md) |
| Data quality: checks of every step, failure handling, reruns | [docs/data_quality.md](docs/data_quality.md) |
| Architecture and its evolution since v0.1 | [docs/architecture.md](docs/architecture.md) |

## Project structure

```
.
├── bigquery/sql/            tables, load, data mart and quality-check SQL
├── dataset/                 sample excerpt of the Ist-Daten (test fixture)
├── docs/                    use case, project plan, architecture, data model, data quality
├── fetcher/                 fetcher job (Python CLI, Dockerfile)
├── orchestrator/
│   ├── flows/               Kestra flows: daily, backfill, setup_bigquery
│   └── kestra/              Kestra server configuration (application.yml)
├── terraform/               Google Cloud infrastructure (bucket, datasets)
├── transform/               transform job (Python CLI, line config, Dockerfile)
├── .env.example             template for all configuration values
├── docker-compose.yml       jobs and Kestra
└── README.md
```

## Setup

> **Ran the earlier local version (Postgres) before?** Remove its containers and
> volumes first, so that Kestra does not start on the old database. In the project
> folder, ideally before `git pull`:
>
> ```bash
> docker compose down -v --remove-orphans
> ```
>
> This deletes only local data (the old Postgres tables and the Kestra execution
> history). Files in the project folder (e.g. `dataset/`) and everything in Google
> Cloud stay untouched.

### Prerequisites

- A **Google Cloud project** with billing enabled (a billing account or the course
  coupon)
- **Terraform** ≥ 1.11.4 (see: https://developer.hashicorp.com/terraform/install)
- **Docker Engine** with Docker Compose v2 (`docker compose version`)
    - (macOS see: https://docs.docker.com/desktop/)
    - (Linux see: https://docs.docker.com/engine/install/)
    - (Windows see: https://docs.docker.com/desktop/setup/install/windows-install/)
- **Git** (see: https://git-scm.com/downloads)
- A text editor for the configuration files. The steps below use **nano**
  (preinstalled on macOS and most Linux distributions); on Windows, **Notepad**.
- Internet access (the fetcher downloads from opentransportdata.swiss)
- Free local disk space for one monthly archive ZIP (about 1.5 GB) while a past
  month is fetched; the raw files themselves go straight to the bucket.

Each step shows the commands for **macOS / Linux** (Terminal) and, where they
differ, for **Windows PowerShell**. Commands without a Windows block are the same
on both.

### 1. Clone

```bash
cd ~
git clone https://github.com/tgfassbind/DataEngineering.git
cd DataEngineering
pwd        # absolute path of the repo, needed in step 4
```

The same commands work in PowerShell. There, `pwd` prints e.g.
`C:\Users\til\DataEngineering`; step 4 needs it as `/c/Users/til/DataEngineering`.

(or via SSH: `git clone git@github.com:tgfassbind/DataEngineering.git`)

### 2. Google Cloud: project ID and service accounts

**Project ID.** Terraform and the pipeline need the project **ID**, not the project
name (they can differ, e.g. name `HSLU DENG`, ID `hslu-hs26-deng`). In the
[Google Cloud Console](https://console.cloud.google.com/), click the project picker
in the top bar (left of the search field): the dialog lists every project with
**Name** and **ID**. The ID is also shown on the **Dashboard** in the card
**Project info**. Note it down.

**Service accounts.** In the Console, go to **IAM & Admin → Service Accounts →
Create service account** and create two accounts with these roles:

| Service account | Used by | Project roles |
|---|---|---|
| `terraform` | Terraform, from your terminal | Storage Admin, BigQuery Admin |
| `pipeline` | Kestra, fetcher, transform | Storage Object User, Storage Bucket Viewer, BigQuery Job User, BigQuery Data Editor |

For each account: open it → **Keys → Add key → Create new key → JSON**. The browser
downloads a file named like `<project-id>-<12 characters>.json`. Note which file
belongs to which account.

Move both keys **outside the repository**, into one folder:

macOS / Linux:

```bash
mkdir -p ~/.config/deng/keys
mv ~/Downloads/<terraform-key-file>.json ~/.config/deng/keys/
mv ~/Downloads/<pipeline-key-file>.json ~/.config/deng/keys/
ls ~/.config/deng/keys/        # both files listed
```

Windows PowerShell:

```powershell
mkdir $HOME\.config\deng\keys
mv $HOME\Downloads\<terraform-key-file>.json $HOME\.config\deng\keys\
mv $HOME\Downloads\<pipeline-key-file>.json $HOME\.config\deng\keys\
ls $HOME\.config\deng\keys\       # both files listed
```

Tip: type the first letters of a file name and press **Tab** to complete it.

Details and reasoning: [terraform/README.md](terraform/README.md).

### 3. Cloud infrastructure

Point Terraform to the **terraform** key. Use the full path and complete the file
name with **Tab**:

macOS / Linux:

```bash
cd terraform
export GOOGLE_APPLICATION_CREDENTIALS=$HOME/.config/deng/keys/<terraform-key-file>.json
ls -l $GOOGLE_APPLICATION_CREDENTIALS      # must list the file, not "No such file"
```

Windows PowerShell (the quotes are needed here; Tab completion works inside them):

```powershell
cd terraform
$env:GOOGLE_APPLICATION_CREDENTIALS = "$HOME\.config\deng\keys\<terraform-key-file>.json"
Test-Path $env:GOOGLE_APPLICATION_CREDENTIALS     # must print True
```

The variable is only set in this terminal window. In a new window, set it again
before using Terraform.

Create your Terraform settings from the template and edit them.

macOS / Linux:

```bash
cp terraform.tfvars.example terraform.tfvars
nano terraform.tfvars
```

Windows PowerShell:

```powershell
cp terraform.tfvars.example terraform.tfvars
notepad terraform.tfvars
```

Replace `YOUR_PROJECT_ID` with your project ID, in both lines:

```hcl
project_id  = "hslu-hs26-deng"
bucket_name = "hslu-hs26-deng-lake" # must be globally unique
location    = "EU"
```

nano: save with **Ctrl+O**, **Enter**, exit with **Ctrl+X**. Notepad: save with
**Ctrl+S** and close the window. Keep the quotes: in this file they are required. If Terraform later reports that the bucket name is taken,
choose another one, e.g. with a suffix.

```bash
terraform init
terraform plan        # 3 to add: bucket, datasets core and mart
terraform apply       # type "yes" when asked
terraform output      # bucket_url, datasets
cd ..
```

### 4. Configure

Create the configuration from the template and open it.

macOS / Linux:

```bash
cp .env.example .env
nano .env
```

Windows PowerShell:

```powershell
cp .env.example .env
notepad .env
```

`.env` is a hidden file: it does not show up in Finder / Explorer by default. Edit it
with nano, Notepad or a code editor, not with TextEdit or Word: they can replace
straight quotes with typographic ones and break the file. Notepad keeps the name
`.env` when saving (no `.txt`).

Set at least these values (example):

```dotenv
GCS_BUCKET=hslu-hs26-deng-lake
GCP_PROJECT_ID=hslu-hs26-deng
GCP_LOCATION=EU
GCP_PIPELINE_KEY_FILE=/Users/til/.config/deng/keys/hslu-hs26-deng-0123456789ab.json
HOST_PROJECT_DIR=/Users/til/DataEngineering
KESTRA_BASIC_AUTH_USERNAME=til@example.com
KESTRA_BASIC_AUTH_PASSWORD=MyKestra2026
KESTRA_DB_PASSWORD=a-long-random-password
```

**On Windows**, write both paths with forward slashes and `/c/` instead of `C:\`:

```dotenv
GCP_PIPELINE_KEY_FILE=/c/Users/til/.config/deng/keys/hslu-hs26-deng-0123456789ab.json
HOST_PROJECT_DIR=/c/Users/til/DataEngineering
```

Kestra starts the jobs through Docker and mounts the repo under the same path inside
a Linux container, where `C:\...` is not a valid path. Docker Desktop translates
`/c/Users/...` to the Windows drive.

| Variable | What to enter |
|---|---|
| `GCS_BUCKET` | `bucket_url` from `terraform output`, without `gs://`. |
| `GCP_PROJECT_ID` | Your project ID (step 2). |
| `GCP_LOCATION` | Same as `location` in `terraform.tfvars` (default `EU`). |
| `GCP_PIPELINE_KEY_FILE` | Full path of the **pipeline** key (not the Terraform key). Write the path out: `~` and `$HOME` do not work here. Get it with `ls ~/.config/deng/keys/*` (macOS: `/Users/<you>/...`, Linux: `/home/<you>/...`, Windows: `/c/Users/<you>/...`). |
| `HOST_PROJECT_DIR` | Output of `pwd` in the repo folder (step 1), no trailing `/`. Kestra starts the jobs with `docker compose run` and needs the repo under the identical path. |
| `KESTRA_BASIC_AUTH_USERNAME` / `KESTRA_BASIC_AUTH_PASSWORD` | Kestra login. The user name must be an e-mail address. The password needs ≥ 8 characters with upper case, lower case and a digit. |
| `KESTRA_DB_PASSWORD` | Password of Kestra's own metadata database. **Set it before the first start** and do not change it afterwards (see [Troubleshooting](#troubleshooting)). |

No quotes are needed in `.env`. Avoid `$` in passwords: Docker Compose reads it as
the start of a variable. All other values have working defaults and are explained
in `.env.example`.

Save (nano: **Ctrl+O**, **Enter**, **Ctrl+X**; Notepad: **Ctrl+S**) and check that
Compose reads the file without errors.

macOS / Linux:

```bash
docker compose config --quiet && echo "configuration OK"
```

Windows PowerShell (no output means OK):

```powershell
docker compose config --quiet
```

### 5. Build and start

```bash
docker compose --profile jobs build   # fetcher and transform images (once and after code changes)
docker compose up -d                  # kestra-db, kestra
docker compose ps                     # wait until both are healthy (1–2 minutes)
```

Fetcher and transform are jobs (profile `jobs`). They do not run permanently;
Kestra or you start them on demand.

### 6. Create the BigQuery tables

Open <http://localhost:8080>, log in, go to **Flows → namespace `deng` →
`setup_bigquery`** and execute it once. It creates the tables and fills the calendar
dimensions; the last log line shows the number of tables in `core` and `mart` and the
rows of `dim_date` (6574) and `dim_time_slot` (24). Safe to rerun.

> **Note:** The Kestra UI sometimes shows that it has lost the connection to the
> server. Reload the browser window; running executions are not affected.

### 7. Load data

**Option A – Kestra (normal operation)**

Execute **`backfill`** with a date range, e.g. `2026-09-01` to `2026-09-07`,
mode `full`. Each run fetches, transforms and loads every day of the range, then
refreshes the data mart and runs the data-quality checks. The example week takes
about 7 minutes (mostly the download of the archive).

From then on, the `daily` flow processes yesterday's data every morning
(06:15 Europe/Zurich), as long as Kestra is running.

**Option B – run the jobs by hand**

The jobs write to the bucket only; loading into BigQuery is a Kestra step.

```bash
docker compose run --rm fetcher --start-date 2026-09-01 --end-date 2026-09-07
docker compose run --rm transform --start-date 2026-09-01 --end-date 2026-09-07
```

then `backfill` with mode `load_only` for the same range.

#### Re-running and forcing

All steps are safe to re-run:

| Step | On re-run | Force |
|---|---|---|
| Fetch | Days already in `raw/` are skipped (`SKIPPED`) | `--force-overwrite` / `force_overwrite: true` downloads the day again and replaces the raw file |
| Transform | Writes the day's files in `processed/` again | – |
| Load | Replaces the processed days and lines in the fact table | – |
| Data mart | Always recomputed in full | – |

The `backfill` flow covers the usual cases through its `mode`:

| `mode` | Steps | Typical use |
|---|---|---|
| `full` (default) | fetch → transform → load → mart → quality checks | normal backfill |
| `fetch_only` | fetch | download only |
| `transform_only` | transform → load → mart → quality checks | transform logic or `lines.yml` changed |
| `load_only` | load → mart → quality checks | BigQuery SQL changed, or a load failed |

To fully rebuild a day after the source data was corrected: `full` with
`force_overwrite: true` for that day.

### 8. Check the result

**Running a query in BigQuery Studio**

1. Open <https://console.cloud.google.com/bigquery> and select your project in the
   project picker at the top (the same project ID as in `.env`).
2. In the **Explorer** on the left, open your project: the datasets `core` and
   `mart` are listed with their tables.
3. Click **+** (or **Compose new query**) to open an empty query tab.
4. Paste the query and click **Run**. The result appears below the editor.

The queries name the tables as `dataset.table` (e.g. `mart.mart_od_delay_by_slot`);
BigQuery adds the selected project. All queries in this README run this way.

The data mart must return rows:

```sql
SELECT linien_text, from_stop, to_stop, weekday, slot_hour,
       median_dep_delay_min, median_arr_delay_min, n_arr_obs
FROM mart.mart_od_delay_by_slot
WHERE from_stop = 'Rotkreuz'
ORDER BY linien_text, to_stop, weekday, slot_hour
LIMIT 20;
```

The exact values are checked in the [Verification](#verification).

Data-quality findings of the last run:

```sql
SELECT check_name, status, betriebstag, details
FROM core.dq_check_result
WHERE checked_at = (SELECT MAX(checked_at) FROM core.dq_check_result)
ORDER BY status DESC, check_name;
```

More queries: [docs/data_quality.md](docs/data_quality.md#queries).

### Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `kestra` restarts, log shows `password authentication failed for user "kestra"` or `Connection to kestra-db … refused` | The Kestra database is created **once**, with the password in `.env` at the first start. A later change of `KESTRA_DB_PASSWORD` (or `KESTRA_DB_USER` / `KESTRA_DB_NAME`) does not reach the existing database. Either set the old values again, or reset Kestra's metadata (flows are reloaded from the repo, the execution history is lost): `docker compose down -v` then `docker compose up -d`. |
| Same error after cloning the repo a **second time** on the same machine (e.g. for another Google Cloud project) | Both clones use the same Compose project name and therefore the same containers and volumes, incl. the Kestra database of the first clone. Stop the first one (`docker compose down` in its folder), or give the second clone its own name and port in `.env`: `COMPOSE_PROJECT_NAME=deng-datapipeline-2` and `KESTRA_UI_PORT=8081`. |
| `docker compose` reports `set GCP_PIPELINE_KEY_FILE …` or `bind source path does not exist` | `GCP_PIPELINE_KEY_FILE` is empty, not an absolute path, or contains `~`. Write the full path (step 4). |
| `docker compose` reports `port is already allocated` | Port 8080 is in use. Set another `KESTRA_UI_PORT` in `.env`. |
| A Kestra task fails with `Permission denied` / `403` | The pipeline service account lacks a role (step 2), or `GCP_PIPELINE_KEY_FILE` points to the Terraform key. |
| Kestra UI shows that the connection to the server was lost | Reload the browser window. Running executions are not affected. |
| `terraform plan`: `could not find default credentials` | `GOOGLE_APPLICATION_CREDENTIALS` is not set in this terminal; set it again as in step 3 (`export` or `$env:`). |

More error messages of the flows: [orchestrator/README.md](orchestrator/README.md).

## Verification

Checks that your setup works and produces the same data as ours.

### 1. Infrastructure and stack

```bash
cd terraform
terraform plan        # "No changes."
cd ..
docker compose ps     # kestra-db and kestra healthy
```

On Windows, set `$env:GOOGLE_APPLICATION_CREDENTIALS` first (setup step 3).

In Kestra, the flows `daily`, `backfill` and `setup_bigquery` are listed in namespace
`deng`, and `setup_bigquery` ran green.

### 2. End to end (real data)

Loads a fixed, closed week through Kestra. Past months come from the monthly
archive, so every team downloads the same files.

> If you already loaded `2026-09-01` to `2026-09-07` with mode `full` in setup
> step 7, skip this run and continue with the checks below.

Execute `backfill` with `start_date = 2026-09-01`, `end_date = 2026-09-07`,
`mode = full`, `force_overwrite = false`, `lines` empty. All tasks must be green.

**Raw files**

macOS / Linux:

```bash
docker compose run --rm --entrypoint python fetcher -m app.inventory | grep '2026-09-0[1-7]'
```

Windows PowerShell:

```powershell
docker compose run --rm --entrypoint python fetcher -m app.inventory | Select-String '2026-09-0[1-7]'
```

The filter keeps the reference week, so other loaded days do not matter.

```
raw/2026-09-01_istdaten.csv             670052892  747541de660e1866bf8285a01736edcd
raw/2026-09-02_istdaten.csv             672336538  6f00aaa0805686ef4bb3774470e1c4ba
raw/2026-09-03_istdaten.csv             671958240  a0d353e1c2e69fa3f5944c169cd871cb
raw/2026-09-04_istdaten.csv             679017602  74fc2de2bd28bfdfcc3dcd79b138f904
raw/2026-09-05_istdaten.csv             555708040  7483afbabc7a1961be1c987c994d4d27
raw/2026-09-06_istdaten.csv             434998310  bdb981980a849b0772d9e928f8966831
raw/2026-09-07_istdaten.csv             667246777  c4f8ea3350dbbd07fb5a4e5c620d3dd2
```

**Fact table, fingerprint per day**

Independent of other loaded days. The fingerprint covers every column except
`loaded_at`, so the same fingerprint means the same content, row by row.

```sql
SELECT betriebstag, COUNT(*) AS fact_rows,
       BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(f))) AS fingerprint
FROM (SELECT * EXCEPT (loaded_at) FROM core.fact_stop_event
      WHERE betriebstag BETWEEN '2026-09-01' AND '2026-09-07') AS f
GROUP BY betriebstag
ORDER BY betriebstag;
```

| betriebstag | fact_rows | fingerprint |
|---|---|---|
| 2026-09-01 | 2842 | -6043014123570813980 |
| 2026-09-02 | 2857 | 5076244706989203856 |
| 2026-09-03 | 2858 | 7447574047709995743 |
| 2026-09-04 | 2862 | -2108112285638568410 |
| 2026-09-05 | 2754 | 6603785114222836667 |
| 2026-09-06 | 2205 | -3507826482310514249 |
| 2026-09-07 | 2866 | 6686182917701743649 |

**Dimensions**

The four dimensions of the star schema. `dim_stop` and `dim_line` are limited to the
members used by the reference week; `source_day` and `updated_at` are left out,
because they depend on when and in which order days were loaded.

```sql
SELECT 'dim_stop' AS dimension, COUNT(*) AS row_count,
       BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(d))) AS fingerprint
FROM (SELECT stop_key, bpuic, haltestellen_name FROM core.dim_stop
      WHERE stop_key IN (SELECT stop_key FROM core.fact_stop_event
                         WHERE betriebstag BETWEEN '2026-09-01' AND '2026-09-07')) AS d
UNION ALL
SELECT 'dim_line', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(d)))
FROM (SELECT line_key, linien_text, betreiber_abk, betreiber_name,
             verkehrsmittel_text, produkt_id FROM core.dim_line
      WHERE line_key IN (SELECT line_key FROM core.fact_stop_event
                         WHERE betriebstag BETWEEN '2026-09-01' AND '2026-09-07')) AS d
UNION ALL
SELECT 'dim_date', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(d)))
FROM core.dim_date AS d
UNION ALL
SELECT 'dim_time_slot', COUNT(*), BIT_XOR(FARM_FINGERPRINT(TO_JSON_STRING(d)))
FROM core.dim_time_slot AS d
ORDER BY dimension;
```

| dimension | row_count | fingerprint |
|---|---|---|
| dim_date | 6574 | -5810570563403347798 |
| dim_line | 2 | -435106981609598615 |
| dim_stop | 32 | -37740699015524814 |
| dim_time_slot | 24 | 8299245875725392440 |

Stops and lines are SCD type 1: if a stop was renamed after the reference week and
later days are loaded, its current name differs and so does the fingerprint.

**Referential integrity**

Every fact row of the reference week has its dimension rows. All values must be 0.

```sql
SELECT
  COUNTIF(s.stop_key IS NULL) AS missing_stop,
  COUNTIF(l.line_key IS NULL) AS missing_line,
  COUNTIF(d.date IS NULL) AS missing_date,
  COUNTIF(f.arr_slot_hour IS NOT NULL AND ta.slot_hour IS NULL)
    + COUNTIF(f.dep_slot_hour IS NOT NULL AND td.slot_hour IS NULL) AS missing_slot
FROM core.fact_stop_event AS f
LEFT JOIN core.dim_stop AS s ON s.stop_key = f.stop_key
LEFT JOIN core.dim_line AS l ON l.line_key = f.line_key
LEFT JOIN core.dim_date AS d ON d.date = f.betriebstag
LEFT JOIN core.dim_time_slot AS ta ON ta.slot_hour = f.arr_slot_hour
LEFT JOIN core.dim_time_slot AS td ON td.slot_hour = f.dep_slot_hour
WHERE f.betriebstag BETWEEN '2026-09-01' AND '2026-09-07';
```

| missing_stop | missing_line | missing_date | missing_slot |
|---|---|---|---|
| 0 | 0 | 0 | 0 |

**Star schema query**

An analysis through fact and dimensions: arrivals at Rotkreuz in the reference
week, per line and weekday / weekend. It joins `dim_stop` (filter by name),
`dim_line` (line text) and `dim_date` (weekend flag), and checks that the joins give
the same figures everywhere.

```sql
SELECT l.linien_text,
       d.is_weekend,
       COUNT(*)                                      AS served_stops,
       COUNT(f.arr_delay_s)                          AS arrivals_measured,
       ROUND(AVG(f.arr_delay_s) / 60, 2)             AS avg_arr_delay_min,
       ROUND(100 * COUNTIF(f.arr_delay_s >= 180)
             / COUNT(f.arr_delay_s), 1)              AS pct_late_3min_plus
FROM core.fact_stop_event AS f
JOIN core.dim_stop AS s ON s.stop_key = f.stop_key
JOIN core.dim_line AS l ON l.line_key = f.line_key
JOIN core.dim_date AS d ON d.date = f.betriebstag
WHERE f.betriebstag BETWEEN '2026-09-01' AND '2026-09-07'
  AND s.haltestellen_name = 'Rotkreuz'
GROUP BY l.linien_text, d.is_weekend
ORDER BY l.linien_text, d.is_weekend;
```

| linien_text | is_weekend | served_stops | arrivals_measured | avg_arr_delay_min | pct_late_3min_plus |
|---|---|---|---|---|---|
| IR75 | false | 188 | 188 | 0.76 | 2.1 |
| IR75 | true | 76 | 76 | 0.67 | 3.9 |
| S1 | false | 731 | 571 | 0.75 | 6.1 |
| S1 | true | 213 | 185 | 1.08 | 10.8 |

**Data product for the reference week**

The mart covers every loaded day. This query computes its row count for the
reference week alone, with the same logic, so it can be compared even if other
days are loaded:

```sql
WITH pairs AS (
  SELECT a.line_key, a.stop_key AS from_stop_key, b.stop_key AS to_stop_key,
         MOD(EXTRACT(DAYOFWEEK FROM a.sched_dep) + 5, 7) + 1 AS weekday,
         a.dep_slot_hour AS slot_hour, a.dep_delay_s, b.arr_delay_s
  FROM core.fact_stop_event AS a
  JOIN core.fact_stop_event AS b
    ON b.betriebstag = a.betriebstag AND b.fahrt_bezeichner = a.fahrt_bezeichner
   AND b.stop_sequence > a.stop_sequence
  WHERE a.sched_dep IS NOT NULL
    AND a.betriebstag BETWEEN '2026-09-01' AND '2026-09-07'
)
SELECT COUNT(*) AS mart_rows,
       COUNT(DISTINCT FORMAT('%d-%d', from_stop_key, to_stop_key)) AS stop_pairs
FROM (SELECT line_key, from_stop_key, to_stop_key, weekday, slot_hour
      FROM pairs
      GROUP BY 1, 2, 3, 4, 5
      HAVING COUNT(dep_delay_s) > 0 OR COUNT(arr_delay_s) > 0);
```

| mart_rows | stop_pairs |
|---|---|
| 87100 | 671 |

If a check differs, the first layer that differs (raw → fact → dimensions → mart)
shows where to look.

## Known limitations

**Data product**

- **Only served stops.** Cancelled stops are excluded by design, so a line with
  many cancellations looks more reliable than it is
  ([data model](docs/data_model.md#design-decisions)).
- **Extreme delays are dropped.** Delays over 3 h and segments over 6 h are treated
  as invalid measurements. Real major disruptions are missing, the medians tend to
  be slightly optimistic ([transform](transform/README.md)).
- **Small groups.** Some combinations of line, stop pair, weekday and hour rest on
  few measurements; the app should show `n_dep_obs` / `n_arr_obs` with the median.
- **All loaded days count equally.** No weighting by age and no distinction of
  holidays or timetable periods; after the timetable change in December, old and
  new timetable mix.
- **Only the configured lines** (IR75 and S1 via Rotkreuz). Another line needs an
  entry in `transform/config/lines.yml` and a `transform_only` run for its days.

**Data and model**

- **Removed lines stay.** A line removed from `lines.yml` keeps its rows in the fact
  table and the mart until they are deleted by hand.
- **No history of names.** Stops and lines are SCD type 1: a renamed stop replaces
  the old name.
- **Source corrections need a re-fetch.** A corrected source file reaches BigQuery
  only after the day is fetched again with `force_overwrite`.
- **Schema changes are manual.** `CREATE TABLE IF NOT EXISTS` does not change an
  existing table; a new column needs a migration.
- **No automated tests for the BigQuery SQL.** Fetcher and transform have unit and
  end-to-end tests; the SQL is covered by the blocking checks and the reference week
  of the [verification](#verification).

**Operations and infrastructure**

- **Kestra runs locally.** `daily` runs only while the machine and Docker are up;
  missed days are caught up at the next start.
- **One run at a time.** A long backfill delays `daily` until it is done.
- **Warnings are not sent.** Monitoring findings are logged in Kestra and stored in
  `core.dq_check_result`; nobody is notified.
- **Raw files grow without limit.** About 650 MB per day, no lifecycle rule on the
  bucket.
- **The mart is always recomputed in full.** Fine at the current size; time and
  scanned data grow with the history.
- **Service account keys as JSON files.** As in class; rotating and storing them
  safely is manual. In production, keyless authentication (e.g. Workload Identity)
  would be preferable.
- **Archive ZIP on local disk.** Past months are downloaded as one ZIP to a temporary
  local folder before the days are uploaded (agreed with the lecturer as temporary
  processing); this needs about 1.5 GB of free space.

## Use of AI

We used Claude (Anthropic) as an assistant throughout the project.

**What the AI did**
- wrote parts of the code of the fetcher, the transform, the BigQuery SQL, the
  Terraform configuration and the Kestra flows
- reviewed and explained code on request
- drafted the documentation

**What we did**
- defined the use case, the data product and the architecture, and made all design
  decisions
- wrote the specifications for each component
- reviewed the generated code, went through it file by file until we understood it,
  and applied changes to the repository ourselves
- set up the Google Cloud project, service accounts and infrastructure, and tested
  the pipeline on our machines and in the cloud with real data, reporting the bugs
  found there (e.g. the S1 line appearing in two networks)
- reviewed and adapted the documentation; the design decisions describe our reasoning

We are responsible for the content of this repository, including the parts written
with AI support.
