# BigQuery

SQL for the warehouse (dataset `core`) and the data mart (dataset `mart`).
The datasets are created by Terraform; these scripts create and fill the tables.
Model, grain and design decisions: [docs/data_model.md](../docs/data_model.md).

| File | When | What |
|---|---|---|
| `sql/01_tables.sql` | setup | key functions, dimensions, fact, mart, data-quality results (the two load tables are created by the Kestra load from the Parquet files) |
| `sql/02_seed_calendar.sql` | setup | fills `dim_date` (2018–2035) and `dim_time_slot` |
| `sql/10_load_core.sql` | after every transform run | MERGE dimensions, replace the processed days and lines in the fact |
| `sql/20_refresh_mart.sql` | after `10_load_core.sql` | full recompute of `mart.mart_od_delay_by_slot` |
| `sql/30_check_quality.sql` | after `20_refresh_mart.sql` | monitoring checks, results in `core.dq_check_result` |

All scripts are idempotent. `10_` and `20_` run in one transaction each and check the
data before the commit: on an error or a failed check nothing changes. Data quality:
[docs/data_quality.md](../docs/data_quality.md).

Run manually (project from `gcloud config`):

```sh
bq query --use_legacy_sql=false < bigquery/sql/01_tables.sql
bq query --use_legacy_sql=false < bigquery/sql/02_seed_calendar.sql
```
