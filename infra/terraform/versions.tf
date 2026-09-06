terraform {
  required_version = ">= 1.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.60"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.32"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.15"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.6"
    }
    http = {
      source  = "hashicorp/http"
      version = "~> 3.4"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    # kubectl_manifest (not the native kubernetes_manifest) for the
    # Karpenter NodePool/EC2NodeClass CRs: the native resource needs a
    # LIVE cluster to validate manifest schema even during `plan`, which
    # fails when the same apply also creates the EKS cluster itself
    # ("cannot create REST client: no client config" — caught by
    # actually running `terraform plan` against a real account, not
    # just `terraform validate`). kubectl_manifest applies more like raw
    # YAML and doesn't have this limitation — the standard community
    # workaround for exactly this EKS bootstrapping scenario.
    kubectl = {
      source  = "alekc/kubectl"
      version = "~> 2.0"
    }
  }

  # Personal sandbox first (see docs/architecture/00-overview.md); the same
  # config is re-applied unchanged to the official Localytics account once
  # that AWS invite arrives, with a fresh `terraform init` (no state
  # continuity assumed between the two accounts — see the staff-engineer
  # execution plan's sequencing risks).
  backend "s3" {
    # bucket/key/region supplied via -backend-config at init time so the
    # same config works for both the sandbox and the official account
    # without hardcoding an account-specific bucket name here.
  }
}
