output "namespace" { value = kubernetes_namespace.churn_service.metadata[0].name }
