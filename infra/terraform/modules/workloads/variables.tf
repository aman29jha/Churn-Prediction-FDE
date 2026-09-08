variable "namespace" {
  type    = string
  default = "churn-service"
}
variable "aws_region" { type = string }
variable "image_tag" {
  type        = string
  description = "Git-SHA-based tag (e.g. sha-83887be) — see docs/architecture/08-infrastructure.md's image versioning section. ECR repos are IMMUTABLE tag mutability, so this must be a real, already-pushed tag."
}
variable "ecr_repository_urls" { type = map(string) }
variable "model_registry_bucket" { type = string }
variable "data_lake_bucket" { type = string }
variable "customer_scores_table_name" { type = string }
variable "glue_database_name" { type = string }
variable "dashboard_name" { type = string }
variable "api_service_role_arn" { type = string }
variable "spark_jobs_role_arn" { type = string }
