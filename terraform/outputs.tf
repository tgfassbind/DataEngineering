output "bucket_url" {
  description = "Data lake bucket, as used in gs:// URIs."
  value       = google_storage_bucket.lake.url
}

output "datasets" {
  description = "BigQuery datasets (project:dataset)."
  value = {
    core = google_bigquery_dataset.core.id
    mart = google_bigquery_dataset.mart.id
  }
}
