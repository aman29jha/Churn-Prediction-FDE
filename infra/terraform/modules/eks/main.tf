# EKS cluster. Compute is deliberately NOT a managed node group:
# - Fargate profile hosts the steady, lightweight workloads (api-service,
#   console, Spark History Server, kube-system/CoreDNS, the ALB
#   controller, and Karpenter's own controller pod).
# - Karpenter (installed via Helm in modules/k8s-addons) provisions EC2
#   NodePools on-demand specifically for Spark driver/executor pods
#   (on-demand for drivers, spot for executors) — see
#   docs/architecture/01-data-platform.md.
# This split is the "smallest viable footprint" the assignment asks for:
# no static, always-paid-for node group sized for a peak that mostly
# doesn't happen.

data "tls_certificate" "eks_oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks_oidc.certificates[0].sha1_fingerprint]
}

resource "aws_iam_role" "cluster" {
  name = "${var.project}-${var.environment}-eks-cluster-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "cluster_policy" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "this" {
  name     = "${var.project}-${var.environment}"
  role_arn = aws_iam_role.cluster.arn
  version  = var.cluster_version

  vpc_config {
    subnet_ids              = concat(var.public_subnet_ids, var.private_subnet_ids)
    endpoint_private_access = true
    endpoint_public_access  = true # simplifies kubectl/CI access for a short-lived exercise account; would restrict in a real prod platform
  }

  access_config {
    authentication_mode = "API"
    # NOTE: bootstrap_cluster_creator_admin_permissions is a create-time-only
    # field — setting it on an EXISTING cluster forces a full replacement
    # (confirmed via `terraform plan` before applying anything here: it
    # showed the cluster and its OIDC provider both being destroyed and
    # recreated). Left at its default (false) deliberately; the explicit
    # access entry below is the non-destructive fix for a cluster that
    # already exists. A fresh cluster (e.g. the official account later)
    # could set this to true instead of needing the explicit entry, but
    # the explicit entry works either way and is what's actually in use here.
  }

  depends_on = [aws_iam_role_policy_attachment.cluster_policy]
}

# Explicit access entry for the IAM principal actually running Terraform —
# the fix for a real gap this hit in practice: `terraform apply`
# succeeded on every AWS-side resource, but every Kubernetes-touching
# resource (Helm releases, the namespace, Karpenter NodePools) failed
# with "Unauthorized" / "the server has asked for the client to provide
# credentials" — `aws eks list-access-entries` confirmed the calling IAM
# user had no access entry at all. This resource is what actually fixes
# that, and stays correct even if a different principal re-applies this
# to the official account later.
data "aws_caller_identity" "current" {}

resource "aws_eks_access_entry" "terraform_principal" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_caller_identity.current.arn
}

resource "aws_eks_access_policy_association" "terraform_principal_admin" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = data.aws_caller_identity.current.arn
  policy_arn    = "arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"

  access_scope {
    type = "cluster"
  }
}

# --- Fargate: steady lightweight services + system pods ---

resource "aws_iam_role" "fargate_pod_execution" {
  name = "${var.project}-${var.environment}-fargate-pod-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks-fargate-pods.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "fargate_pod_execution" {
  role       = aws_iam_role.fargate_pod_execution.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSFargatePodExecutionRolePolicy"
}

resource "aws_eks_fargate_profile" "system" {
  cluster_name           = aws_eks_cluster.this.name
  fargate_profile_name   = "system"
  pod_execution_role_arn = aws_iam_role.fargate_pod_execution.arn
  subnet_ids             = var.private_subnet_ids

  selector {
    namespace = "kube-system"
  }
}

resource "aws_eks_fargate_profile" "apps" {
  cluster_name           = aws_eks_cluster.this.name
  fargate_profile_name   = "apps"
  pod_execution_role_arn = aws_iam_role.fargate_pod_execution.arn
  subnet_ids             = var.private_subnet_ids

  # Real bug found by actually triggering medallion_pipeline_dag: a plain
  # namespace selector (no labels) claims EVERY pod in churn-service,
  # including the Spark driver/executor pods that SparkKubernetesOperator
  # creates there — which carry a `workload-type: spark-driver/executor`
  # nodeSelector meant for Karpenter's EC2 NodePools, not Fargate. Fargate's
  # own scheduler grabbed them anyway (its admission webhook matches on
  # namespace before anything else gets a say) and then failed forever:
  # "MatchNodeSelector failed: Fargate profile apps cannot satisfy pod's
  # node selector/affinity requirements". Scoped to an explicit label
  # instead, applied to every OTHER workload in this namespace (api-service,
  # console, spark-history-server, Airflow's chart-wide `labels`, Spark
  # Operator's controller/webhook `labels`) — Spark driver/executor pods
  # deliberately don't carry it, so they fall through to Karpenter.
  selector {
    namespace = "churn-service"
    labels = {
      "fargate-scheduled" = "true"
    }
  }
}
