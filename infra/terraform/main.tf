data "aws_caller_identity" "current" {}

module "networking" {
  source                  = "./modules/networking"
  project                 = var.project
  environment             = var.environment
  aws_region              = var.aws_region
  vpc_cidr                = var.vpc_cidr
  availability_zone_count = var.availability_zone_count
}

module "eks" {
  source             = "./modules/eks"
  project            = var.project
  environment        = var.environment
  cluster_version    = var.eks_cluster_version
  public_subnet_ids  = module.networking.public_subnet_ids
  private_subnet_ids = module.networking.private_subnet_ids
}

# Real race condition hit in practice: the kubernetes_namespace resource
# fired its API call before the new EKS access entry (created in
# module.eks) had finished propagating, and failed with "Unauthorized" —
# while the LB controller Helm release, created in the same apply,
# survived because Helm's own install-wait logic retries internally. A
# plain `depends_on` on module.eks isn't enough by itself since this is
# an eventual-consistency delay, not just an ordering problem — hence the
# explicit sleep.
resource "time_sleep" "wait_for_eks_access_entry" {
  depends_on      = [module.eks]
  create_duration = "20s"
}

module "storage" {
  source      = "./modules/storage"
  project     = var.project
  environment = var.environment
  account_id  = data.aws_caller_identity.current.account_id
}

module "ecr" {
  source      = "./modules/ecr"
  project     = var.project
  environment = var.environment
}

module "irsa" {
  source                    = "./modules/irsa"
  project                   = var.project
  environment               = var.environment
  oidc_provider_arn         = module.eks.oidc_provider_arn
  oidc_provider_url         = module.eks.oidc_provider_url
  data_lake_bucket_arn      = module.storage.data_lake_bucket_arn
  model_registry_bucket_arn = module.storage.model_registry_bucket_arn
  customer_scores_table_arn = module.storage.customer_scores_table_arn
}

module "observability" {
  source                     = "./modules/observability"
  project                    = var.project
  environment                = var.environment
  aws_region                 = var.aws_region
  log_retention_days         = var.log_retention_days
  alarm_email                = var.budget_alarm_email
  customer_scores_table_name = module.storage.customer_scores_table_name
}

module "messaging" {
  source                        = "./modules/messaging"
  project                       = var.project
  environment                   = var.environment
  data_lake_bucket              = module.storage.data_lake_bucket
  data_lake_bucket_arn          = module.storage.data_lake_bucket_arn
  vpc_id                        = module.networking.vpc_id
  private_subnet_ids            = module.networking.private_subnet_ids
  eks_cluster_security_group_id = module.eks.cluster_security_group_id
  # Real bug found live-testing this Lambda's first-ever invocation: the
  # internal cluster-DNS hostname (airflow-webserver.churn-service.svc)
  # is only resolvable via CoreDNS, which Kubernetes pods get through
  # kubelet-injected /etc/resolv.conf — a VPC-attached Lambda has no such
  # resolver and can never reach it, regardless of auth. Pointed instead
  # at the same public ALB + nginx-prefix-strip path everything else in
  # this service already uses (console, API, Spark History).
  airflow_api_url               = "http://${module.workloads.ingress_hostname}/airflow/api/v1"
  airflow_api_username          = var.airflow_api_username
  airflow_api_password          = var.airflow_api_password
  alarm_topic_arn               = module.observability.alarm_topic_arn
}

module "database" {
  source                        = "./modules/database"
  project                       = var.project
  environment                   = var.environment
  vpc_id                        = module.networking.vpc_id
  private_subnet_ids            = module.networking.private_subnet_ids
  eks_cluster_security_group_id = module.eks.cluster_security_group_id
}

module "k8s_addons" {
  source                    = "./modules/k8s-addons"
  project                   = var.project
  environment               = var.environment
  aws_region                = var.aws_region
  cluster_name              = module.eks.cluster_name
  vpc_id                    = module.networking.vpc_id
  private_subnet_ids        = module.networking.private_subnet_ids
  cluster_security_group_id = module.eks.cluster_security_group_id
  oidc_provider_arn         = module.eks.oidc_provider_arn
  oidc_provider_url         = module.eks.oidc_provider_url
  dags_git_repo             = var.dags_git_repo
  airflow_db_endpoint       = module.database.endpoint
  airflow_db_name           = module.database.db_name
  airflow_db_username       = module.database.username
  airflow_db_password       = module.database.password

  depends_on = [time_sleep.wait_for_eks_access_entry]
}

module "workloads" {
  source                     = "./modules/workloads"
  aws_region                 = var.aws_region
  image_tag                  = var.image_tag
  ecr_repository_urls        = module.ecr.repository_urls
  model_registry_bucket      = module.storage.model_registry_bucket
  data_lake_bucket           = module.storage.data_lake_bucket
  customer_scores_table_name = module.storage.customer_scores_table_name
  glue_database_name         = module.storage.glue_database_name
  dashboard_name             = module.observability.dashboard_name
  api_service_role_arn       = module.irsa.api_service_role_arn
  spark_jobs_role_arn        = module.irsa.spark_jobs_role_arn

  depends_on = [module.k8s_addons]
}
