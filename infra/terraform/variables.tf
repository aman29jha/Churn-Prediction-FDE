variable "project" {
  description = "Short name used to prefix all resources, keeping sandbox/official-account and multi-run naming collision-free."
  type        = string
  default     = "churn-fde"
}

variable "environment" {
  description = "e.g. sandbox, official — appended to resource names/tags."
  type        = string
  default     = "sandbox"
}

variable "aws_region" {
  description = "ap-south-1 for the personal sandbox; re-set per -var when applying to the official account."
  type        = string
  default     = "ap-south-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "availability_zone_count" {
  description = "2 is enough for this exercise's data volume; keeps NAT/subnet cost down vs. 3."
  type        = number
  default     = 2
}

variable "eks_cluster_version" {
  description = "Real finding: the cluster came up as 1.31 despite this being set to 1.30 at creation time (AWS likely didn't have 1.30 available in ap-south-1 at that moment, or auto-selected the nearest supported version) — Terraform's state recorded our requested 1.30, so later applies tried to 'correct' the live 1.31 cluster back to 1.30, which EKS refused (`InvalidParameterException: Cluster is not eligible for rollback`). Aligned to the actual running version rather than forcing a real version-change API call."
  type        = string
  default     = "1.31"
}

variable "budget_alarm_email" {
  description = "Where CloudWatch billing/error alarms are sent."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  description = "Explicit retention on every CloudWatch Log Group — defaults to never-expire otherwise, a real cost leak on a strict-budget account (see docs/architecture/05-observability.md)."
  type        = number
  default     = 14
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "airflow_api_token" {
  description = "Bearer token the trigger Lambda uses to call Airflow's REST API. Generated post-deploy (Airflow API auth setup) and passed via -var or a tfvars file that's gitignored — never committed."
  type        = string
  sensitive   = true
  default     = ""
}

variable "dags_git_repo" {
  description = "Git repo Airflow's git-sync sidecar pulls airflow/dags/ from."
  type        = string
  default     = "https://github.com/aman29jha/Churn-Prediction-FDE.git"
}

variable "image_tag" {
  description = "Git-SHA-based tag for the 3 Docker images already pushed to ECR (see docs/architecture/08-infrastructure.md). Suffixed -amd64: the first push (sha-83887be) was built natively on Apple Silicon (arm64) and crashed on Fargate's amd64 nodes with 'exec format error' — rebuilt with `docker buildx build --platform linux/amd64`. Bumped to sha-671a6f3-amd64: spark-jobs's _write_iceberg now evolves the target Iceberg schema (ALTER TABLE ADD COLUMNS) before MERGE, fixing a live failure when gold_transform's new run_date column didn't exist on tables bootstrapped before it was added; api-service and console content unchanged, rebuilt/retagged only to keep all 3 images on one shared per-commit tag."
  type        = string
  default     = "sha-671a6f3-amd64"
}
