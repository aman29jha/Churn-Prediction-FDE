output "cluster_name" { value = module.eks.cluster_name }
output "cluster_endpoint" { value = module.eks.cluster_endpoint }
output "data_lake_bucket" { value = module.storage.data_lake_bucket }
output "model_registry_bucket" { value = module.storage.model_registry_bucket }
output "glue_database_name" { value = module.storage.glue_database_name }
output "customer_scores_table_name" { value = module.storage.customer_scores_table_name }
output "ecr_repository_urls" { value = module.ecr.repository_urls }
output "dashboard_name" { value = module.observability.dashboard_name }
output "kubeconfig_command" {
  value = "aws eks update-kubeconfig --name ${module.eks.cluster_name} --region ${var.aws_region}"
}
output "ingress_hostname" { value = module.workloads.ingress_hostname }
output "console_password" {
  value     = module.workloads.console_password
  sensitive = true
}
