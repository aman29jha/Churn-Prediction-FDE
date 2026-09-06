variable "project" { type = string }
variable "environment" { type = string }
variable "cluster_version" { type = string }
variable "public_subnet_ids" { type = list(string) }
variable "private_subnet_ids" { type = list(string) }
