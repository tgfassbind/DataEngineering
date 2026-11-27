variable "project_id" {
  description = "Google Cloud project in which to create the resources (project ID, not the display name)."
  type        = string
}

variable "bucket_name" {
  description = "Globally unique name of the Cloud Storage bucket (data lake). Holds raw/ and processed/."
  type        = string
}

variable "location" {
  description = "Location of the bucket and both BigQuery datasets. Must be the same for all three: BigQuery load jobs require the dataset in the bucket's location."
  type        = string
  default     = "EU"
}

variable "labels" {
  description = "Labels attached to the bucket and the datasets."
  type        = map(string)
  default = {
    course  = "deng"
    project = "deng"
  }
}
