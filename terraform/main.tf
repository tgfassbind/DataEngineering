# Infrastructure only: the places where data will live. Creating, loading and
# transforming data is the pipeline's job (fetcher, transform, Kestra, SQL in
# bigquery/sql/), never Terraform's.

# Data lake. One bucket, two prefixes (created by the pipeline, not here):
#   raw/YYYY-MM-DD_istdaten.csv         original daily files, unchanged (fetcher)
#   processed/YYYY-MM-DD_*.parquet|csv  validated rows and run scope (transform)
resource "google_storage_bucket" "lake" {
  name                        = var.bucket_name
  location                    = var.location
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  # A bucket that still contains data is never deleted by terraform destroy.
  force_destroy = false
  labels        = var.labels
}

# Warehouse: star schema (fact_stop_event, dim_*) and the load tables.
# The dataset ID is fixed: bigquery/sql/ refers to it by name.
resource "google_bigquery_dataset" "core" {
  dataset_id  = "core"
  location    = var.location
  description = "Warehouse: star schema and load tables. Tables are created by bigquery/sql/01_tables.sql."
  labels      = var.labels
}

# Data mart: the aggregated data product the app reads.
resource "google_bigquery_dataset" "mart" {
  dataset_id  = "mart"
  location    = var.location
  description = "Data mart. Tables are created by bigquery/sql/01_tables.sql."
  labels      = var.labels
}
