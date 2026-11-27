terraform {
  required_version = ">= 1.11.4, < 2.0.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "6.44.0"
    }
  }
}

# Authentication: the Terraform service account's JSON key, found through
# GOOGLE_APPLICATION_CREDENTIALS in the terminal (see README). The key itself
# never goes into this directory or into Git.
provider "google" {
  project = var.project_id
}
