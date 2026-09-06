# S3 (Iceberg data lake + model registry + Spark event logs), Glue Data
# Catalog (Iceberg catalog for bronze/silver/gold), DynamoDB
# (customer_scores fast-lookup cache). See
# docs/architecture/{01-data-platform,04-serving}.md.

resource "aws_s3_bucket" "data_lake" {
  bucket = "${var.project}-${var.environment}-data-lake-${var.account_id}"
}

resource "aws_s3_bucket_public_access_block" "data_lake" {
  bucket                  = aws_s3_bucket.data_lake.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_lifecycle_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id
  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {} # applies to the whole bucket; AWS provider requires an explicit filter/prefix block
    noncurrent_version_expiration {
      noncurrent_days = 30 # bounds storage cost from Iceberg's own snapshot history + S3 versioning stacking
    }
  }
}

resource "aws_s3_bucket" "model_registry" {
  bucket = "${var.project}-${var.environment}-model-registry-${var.account_id}"
}

resource "aws_s3_bucket_public_access_block" "model_registry" {
  bucket                  = aws_s3_bucket.model_registry.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Glue Data Catalog database for the Iceberg tables (bronze/silver/gold),
# queried via Athena. See docs/architecture/01-data-platform.md.
resource "aws_glue_catalog_database" "churn" {
  name = replace("${var.project}_${var.environment}", "-", "_")
}

# DynamoDB — operational fast-lookup layer for the real-time API, distinct
# from the Athena/Iceberg analytical layer (docs/architecture/04-serving.md).
resource "aws_dynamodb_table" "customer_scores" {
  name         = "${var.project}-${var.environment}-customer-scores"
  billing_mode = "PAY_PER_REQUEST" # unpredictable, bursty access pattern (live simulator + on-demand API reads) — pay-per-use avoids over-provisioning throughput
  hash_key     = "customer_id"

  attribute {
    name = "customer_id"
    type = "S"
  }
}
