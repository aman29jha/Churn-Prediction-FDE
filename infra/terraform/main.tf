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
  airflow_api_url               = "http://airflow-webserver.churn-service.svc:8080/api/v1"
  airflow_api_token             = var.airflow_api_token
  alarm_topic_arn               = module.observability.alarm_topic_arn
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
}
