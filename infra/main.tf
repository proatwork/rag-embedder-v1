# The Cloudflare resources behind the norms MCP Worker (worker/).
#
# Tofu creates the D1 database, the R2 bucket with its public r2.dev URL, and the Vectorize index. The provider
# has no Vectorize resource, so wrangler makes that one, once, from here.
# Wrangler still does what Tofu can't: the D1 migrations (npm run db:migrate:remote) and the deploy (npm run deploy),
# which bundles the TypeScript.
#
#   (cd ../worker && npm ci)              # the wrangler that the Vectorize step runs
#   export CLOUDFLARE_API_TOKEN=...        # never in a .tfvars file; the provider and wrangler both read it
#   tofu init
#   tofu apply -var account_id=<your account id>
#
# Then put the outputs where they go: d1_database_id into worker/wrangler.jsonc, figures_base_url into its
# FIGURES_BASE_URL var.

terraform {
  required_version = ">= 1.6"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.29"
    }
  }
}

provider "cloudflare" {} # takes CLOUDFLARE_API_TOKEN from the environment

variable "account_id" {
  description = "The Cloudflare account that holds everything."
  type        = string
}

variable "location" {
  description = "Where D1 and R2 keep their data: wnam, enam, weur, eeur, apac or oc. Read only when they are created."
  type        = string
  default     = "eeur"
}

variable "bucket_name" {
  description = "The R2 bucket for the figure PNGs, as upload_to_cloudflare.py --bucket takes it."
  type        = string
  default     = "norms-figures"
}

locals {
  # Fixed, not variables: worker/wrangler.jsonc names them too, so change both together.
  database_name = "norms"
  index_name    = "norms"

  # What the Worker and the uploader expect: Qwen3-Embedding-0.6B vectors, filtered by the norm's code.
  embedding_dimensions = 1024 # worker/src/store.ts EMBEDDING_DIMENSIONS
  embedding_metric     = "cosine"
  norm_filter_key      = "norm" # worker/src/store.ts NORM_FILTER_KEY
  worker_dir           = "${path.module}/../worker"
}

# Replacing this deletes every row: change its name or location only on purpose.
resource "cloudflare_d1_database" "norms" {
  account_id            = var.account_id
  name                  = local.database_name
  primary_location_hint = var.location

  lifecycle {
    prevent_destroy = true
  }
}

resource "cloudflare_r2_bucket" "figures" {
  account_id = var.account_id
  name       = var.bucket_name
  location   = var.location
}

# The public r2.dev URL the figure links point to.
resource "cloudflare_r2_managed_domain" "figures" {
  account_id  = var.account_id
  bucket_name = cloudflare_r2_bucket.figures.name
  enabled     = true
}

# Runs once, on the first apply. Tofu cannot see the index after that: it won't notice if someone deletes it, and
# changing the settings here does nothing to an index that exists. To change them, delete the index with
# wrangler (and with it every vector), then `tofu apply -replace=terraform_data.vectorize_index`.
# The index is made only if `get` doesn't find it, so a run whose metadata index failed can simply be applied again.
resource "terraform_data" "vectorize_index" {
  input = {
    name       = local.index_name
    dimensions = local.embedding_dimensions
    metric     = local.embedding_metric
    filter_key = local.norm_filter_key
  }

  provisioner "local-exec" {
    working_dir = local.worker_dir
    environment = {
      CLOUDFLARE_ACCOUNT_ID = var.account_id
    }
    # The metadata index must exist before the first upsert, or the norm filter misses those vectors.
    command = <<-EOT
      { npx --no-install wrangler vectorize get ${self.input.name} >/dev/null 2>&1 ||
        npx --no-install wrangler vectorize create ${self.input.name} --dimensions=${self.input.dimensions} --metric=${self.input.metric}; } &&
      npx --no-install wrangler vectorize create-metadata-index ${self.input.name} --propertyName=${self.input.filter_key} --type=string
    EOT
  }
}

output "d1_database_id" {
  description = "For d1_databases[0].database_id in worker/wrangler.jsonc."
  value       = cloudflare_d1_database.norms.id
}

output "figures_base_url" {
  description = "For FIGURES_BASE_URL in worker/wrangler.jsonc."
  value       = "https://${cloudflare_r2_managed_domain.figures.domain}"
}

output "bucket_name" {
  description = "For upload_to_cloudflare.py --bucket."
  value       = cloudflare_r2_bucket.figures.name
}
