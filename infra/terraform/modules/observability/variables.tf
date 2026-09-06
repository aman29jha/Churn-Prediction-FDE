variable "project" { type = string }
variable "environment" { type = string }
variable "aws_region" { type = string }
variable "log_retention_days" { type = number }
variable "alarm_email" {
  type    = string
  default = ""
}
variable "customer_scores_table_name" { type = string }
