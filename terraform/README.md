# Terraform

Creates the empty cloud infrastructure of the pipeline in Google Cloud:

| Resource | Name | Purpose |
|---|---|---|
| Cloud Storage bucket | `var.bucket_name` | data lake: `raw/` (original files), `processed/` (transform output) |
| BigQuery dataset | `core` | warehouse: star schema and load tables |
| BigQuery dataset | `mart` | data mart: the data product |

All three use the same location (`EU` by default), because BigQuery can only load
from a bucket in the dataset's location.

Terraform creates only the places where data lives. Tables are created by
[`bigquery/sql/01_tables.sql`](../bigquery/sql/01_tables.sql); files and rows are
written by the pipeline.

## Files

| File | Content |
|---|---|
| `versions.tf` | Terraform and provider versions, provider configuration |
| `variables.tf` | inputs: `project_id`, `bucket_name`, `location`, `labels` |
| `main.tf` | bucket and datasets |
| `outputs.tf` | bucket URL and dataset IDs after `apply` |
| `terraform.tfvars.example` | template for your own `terraform.tfvars` (ignored by Git) |

## Prerequisites (once per project)

Two service accounts, created manually in the Console (**IAM & Admin → Service
Accounts**), each with a JSON key stored **outside the repository**, e.g. in
`~/.config/deng/keys/`:

| Service account | Used by | Project roles |
|---|---|---|
| `terraform` | Terraform, from your terminal | Storage Admin, BigQuery Admin |
| `pipeline` | Kestra, fetcher, transform | Storage Object User, Storage Bucket Viewer, BigQuery Job User, BigQuery Data Editor |

Infrastructure and pipeline use separate identities: the pipeline can read and write
data but cannot create or delete buckets and datasets. The roles are assigned
manually, because the Terraform account deliberately has no permission to change IAM.

## Usage

From this directory:

```sh
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/.config/deng/keys/terraform.json"
cp terraform.tfvars.example terraform.tfvars   # then set project_id and bucket_name

terraform init        # downloads the provider, writes .terraform.lock.hcl
terraform fmt
terraform validate
terraform plan        # first run: 3 to add, 0 to change, 0 to destroy
terraform apply
```

Always read the plan before `apply`. After the first `apply`, `terraform plan` must
report **no changes**; anything else means the infrastructure differs from the code.

Commit `.terraform.lock.hcl` (pins the exact provider build); never commit
`terraform.tfvars`, state files or keys (see `.gitignore`).

## Removing the infrastructure

`terraform destroy` refuses to delete a bucket or dataset that still contains data
(`force_destroy = false`, no `delete_contents_on_destroy`). This is intentional:
the raw files are the only copy of the source data. Empty them deliberately first
if the project is really finished.
