variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "cluster_name" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "cluster_security_group_id" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider_url" { type = string }
variable "dags_git_repo" {
  type        = string
  description = "Git repo Airflow's git-sync sidecar pulls airflow/dags/ from — this fork, e.g. https://github.com/aman29jha/Churn-Prediction-FDE.git"
}
