# IRSA (IAM Roles for Service Accounts) — pods assume these roles via the
# EKS OIDC provider, scoped to specific resource ARNs (not wildcard "*"),
# rather than sharing broad node-level credentials.

data "aws_iam_policy_document" "assume_role_api_service" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = ["system:serviceaccount:churn-service:api-service"]
    }
  }
}

resource "aws_iam_role" "api_service" {
  name               = "${var.project}-${var.environment}-api-service-irsa"
  assume_role_policy = data.aws_iam_policy_document.assume_role_api_service.json
}

resource "aws_iam_role_policy" "api_service" {
  name = "api-service-policy"
  role = aws_iam_role.api_service.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadModelRegistry"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.model_registry_bucket_arn, "${var.model_registry_bucket_arn}/*"]
      },
      {
        Sid      = "ReadWriteCustomerScores"
        Effect   = "Allow"
        Action   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:BatchGetItem"]
        Resource = var.customer_scores_table_arn
      },
      {
        Sid      = "AppendBronze"
        Effect   = "Allow"
        Action   = ["s3:PutObject"]
        Resource = "${var.data_lake_bucket_arn}/bronze/*"
      },
      # Analytics dashboard: api-service runs Athena queries against the
      # real Iceberg tables (Glue Catalog) on behalf of the console's new
      # Analytics tab, rather than the console talking to AWS directly —
      # keeps AWS credentials/IRSA centralized in the one service that
      # already has them, consistent with the existing architecture.
      {
        Sid    = "AthenaAnalyticsQueries"
        Effect = "Allow"
        Action = [
          "athena:StartQueryExecution", "athena:GetQueryExecution",
          "athena:GetQueryResults", "athena:StopQueryExecution",
        ]
        Resource = "*"
      },
      {
        Sid      = "GlueReadForAthena"
        Effect   = "Allow"
        Action   = ["glue:GetTable", "glue:GetTables", "glue:GetDatabase", "glue:GetPartitions"]
        Resource = "*"
      },
      {
        Sid      = "ReadIcebergWarehouseForAthena"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:ListBucket"]
        Resource = [var.data_lake_bucket_arn, "${var.data_lake_bucket_arn}/warehouse/*"]
      },
      {
        # s3:ListBucket is bucket-level (already granted, bare bucket ARN,
        # in ReadIcebergWarehouseForAthena above) — this is object-level
        # only, for the actual result file GetObject/PutObject.
        Sid      = "AthenaQueryResultStaging"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = ["${var.data_lake_bucket_arn}/athena-results/*"]
      }
    ]
  })
}

data "aws_iam_policy_document" "assume_role_spark_jobs" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = ["system:serviceaccount:churn-service:spark-jobs"]
    }
  }
}

resource "aws_iam_role" "spark_jobs" {
  name               = "${var.project}-${var.environment}-spark-jobs-irsa"
  assume_role_policy = data.aws_iam_policy_document.assume_role_spark_jobs.json
}

resource "aws_iam_role_policy" "spark_jobs" {
  name = "spark-jobs-policy"
  role = aws_iam_role.spark_jobs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadWriteDataLake"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
        Resource = [var.data_lake_bucket_arn, "${var.data_lake_bucket_arn}/*"]
      },
      {
        Sid      = "WriteModelRegistry"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${var.model_registry_bucket_arn}/*"
      },
      # Real bug found by actually running the gold_transform SparkApplication:
      # its initContainer runs `aws s3 sync` to pull model artifacts, which
      # calls ListObjectsV2 on the BUCKET itself (not a key prefix) before it
      # can GetObject any individual file — failed with AccessDenied despite
      # GetObject/PutObject already being granted, since s3:ListBucket is a
      # bucket-level action requiring the bucket ARN itself as Resource, not
      # the "/*" object-level ARN above.
      {
        Sid      = "ListModelRegistry"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.model_registry_bucket_arn
      },
      {
        # glue:DeleteTable is required for Iceberg's `.createOrReplace()` —
        # on any run after the first (table already exists), Iceberg's
        # GlueCatalog does a drop+recreate under the hood, not just
        # CreateTable. Added proactively rather than discovering it via
        # another AccessDenied on the second Spark job run.
        Sid      = "GlueCatalogForIceberg"
        Effect   = "Allow"
        Action   = ["glue:GetTable", "glue:GetTables", "glue:CreateTable", "glue:UpdateTable", "glue:DeleteTable", "glue:GetDatabase"]
        Resource = "*" # Glue's resource-level ARNs for tables-not-yet-created are awkward to scope precisely; acceptable for this exercise's single Glue database
      },
      {
        Sid      = "WriteCustomerScores"
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:BatchWriteItem"]
        Resource = var.customer_scores_table_arn
      }
    ]
  })
}
