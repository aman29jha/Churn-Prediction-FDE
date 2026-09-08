variable "project" { type = string }
variable "environment" { type = string }
variable "data_lake_bucket" { type = string }
variable "data_lake_bucket_arn" { type = string }
variable "vpc_id" { type = string }
variable "private_subnet_ids" { type = list(string) }
variable "eks_cluster_security_group_id" { type = string }
variable "airflow_api_url" {
  type        = string
  description = "Internal ClusterIP/service URL for the Airflow webserver API, e.g. http://airflow-webserver.churn-service.svc:8080/api/v1 — only resolvable from inside the VPC, which is why this Lambda runs with vpc_config."
}
variable "airflow_api_username" {
  type    = string
  default = "admin"
}
variable "airflow_api_password" {
  type      = string
  sensitive = true
}
variable "alarm_topic_arn" {
  type    = string
  default = ""
}
