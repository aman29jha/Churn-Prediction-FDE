output "console_password" {
  value     = random_password.console_password.result
  sensitive = true
}
output "ingest_token" {
  value     = random_password.ingest_token.result
  sensitive = true
}
output "ingress_hostname" {
  value = try(kubernetes_ingress_v1.main.status[0].load_balancer[0].ingress[0].hostname, "(not yet assigned)")
}
