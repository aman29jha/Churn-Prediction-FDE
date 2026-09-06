# Kubernetes-level installs, all via Helm so the whole stack stays
# infrastructure-as-code (no manual `kubectl apply` steps) — see
# docs/architecture/{01-data-platform,03-orchestration}.md for why each
# of these exists and the design decisions behind their configuration.

resource "kubernetes_namespace" "churn_service" {
  metadata {
    name = "churn-service"
  }
}

# --- AWS Load Balancer Controller: provisions the ALB for our ingress ---

data "aws_iam_policy_document" "lb_controller_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = ["system:serviceaccount:kube-system:aws-load-balancer-controller"]
    }
  }
}

resource "aws_iam_role" "lb_controller" {
  name               = "${var.project}-${var.environment}-lb-controller-irsa"
  assume_role_policy = data.aws_iam_policy_document.lb_controller_assume.json
}

# The official policy JSON is long-lived at a stable URL published by the
# aws-load-balancer-controller project; referencing it by version keeps
# this in sync with the exact permissions the controller actually needs
# rather than hand-maintaining a drift-prone copy.
data "http" "lb_controller_policy" {
  url = "https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/v2.9.0/docs/install/iam_policy.json"
}

resource "aws_iam_role_policy" "lb_controller" {
  name   = "lb-controller-policy"
  role   = aws_iam_role.lb_controller.id
  policy = data.http.lb_controller_policy.response_body
}

resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  version    = "1.8.1"
  namespace  = "kube-system"

  set {
    name  = "clusterName"
    value = var.cluster_name
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.lb_controller.arn
  }
  set {
    name  = "region"
    value = var.aws_region
  }
  set {
    name  = "vpcId"
    value = var.vpc_id
  }
}

# --- Karpenter: on-demand NodePool for Spark drivers, spot for executors ---

data "aws_iam_policy_document" "karpenter_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    effect  = "Allow"
    principals {
      type        = "Federated"
      identifiers = [var.oidc_provider_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${var.oidc_provider_url}:sub"
      values   = ["system:serviceaccount:kube-system:karpenter"]
    }
  }
}

resource "aws_iam_role" "karpenter_controller" {
  name               = "${var.project}-${var.environment}-karpenter-irsa"
  assume_role_policy = data.aws_iam_policy_document.karpenter_assume.json
}

resource "aws_iam_role_policy" "karpenter_controller" {
  name = "karpenter-controller-policy"
  role = aws_iam_role.karpenter_controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "EC2NodeLifecycle"
        Effect = "Allow"
        Action = [
          "ec2:CreateLaunchTemplate", "ec2:CreateFleet", "ec2:RunInstances", "ec2:TerminateInstances",
          "ec2:DescribeLaunchTemplates", "ec2:DescribeInstances", "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups", "ec2:DescribeInstanceTypes", "ec2:DescribeInstanceTypeOfferings",
          "ec2:DescribeAvailabilityZones", "ec2:DeleteLaunchTemplate", "ec2:CreateTags"
        ]
        Resource = "*"
      },
      {
        Sid      = "PricingForSpotDecisions"
        Effect   = "Allow"
        Action   = ["pricing:GetProducts", "ec2:DescribeSpotPriceHistory"]
        Resource = "*"
      },
      {
        Sid      = "PassNodeInstanceRole"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = aws_iam_role.node_instance.arn
      },
      {
        Sid      = "EKSClusterRead"
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster"]
        Resource = "*"
      }
    ]
  })
}

# Instance profile Karpenter-launched EC2 nodes actually run as.
resource "aws_iam_role" "node_instance" {
  name = "${var.project}-${var.environment}-karpenter-node-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "node_instance" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
  ])
  role       = aws_iam_role.node_instance.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "node_instance" {
  name = "${var.project}-${var.environment}-karpenter-node-profile"
  role = aws_iam_role.node_instance.name
}

resource "helm_release" "karpenter" {
  name             = "karpenter"
  repository       = "oci://public.ecr.aws/karpenter"
  chart            = "karpenter"
  version          = "1.0.6"
  namespace        = "kube-system"
  create_namespace = false

  set {
    name  = "settings.clusterName"
    value = var.cluster_name
  }
  set {
    name  = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
    value = aws_iam_role.karpenter_controller.arn
  }
}

# EC2NodeClass: shared AMI/instance config for both NodePools below.
# kubectl_manifest, not the native kubernetes_manifest — the native
# resource needs a LIVE cluster to validate manifest schema even during
# `plan`, which fails when this same apply also creates the EKS cluster
# ("cannot create REST client: no client config" — caught by actually
# running `terraform plan` against a real account, not just
# `terraform validate`). See versions.tf's provider comment.
resource "kubectl_manifest" "spark_ec2_node_class" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata   = { name = "spark-jobs" }
    spec = {
      amiFamily = "AL2023"
      # Required as of Karpenter v1's EC2NodeClass API (v1beta1 accepted
      # amiFamily alone; v1 requires an explicit selector even for the
      # standard alias) — caught by the real API rejecting the manifest,
      # not by anything `terraform validate` could see.
      amiSelectorTerms = [{ alias = "al2023@latest" }]
      role             = aws_iam_role.node_instance.name
      subnetSelectorTerms = [
        for id in var.private_subnet_ids : { id = id }
      ]
      securityGroupSelectorTerms = [{ id = var.cluster_security_group_id }]
      tags                       = { "karpenter.sh/managed" = "true", Project = var.project, Environment = var.environment }
    }
  })
  depends_on = [helm_release.karpenter]
}

# On-demand NodePool: Spark DRIVERS — losing a driver kills the whole job,
# so it needs stable capacity, not spot.
resource "kubectl_manifest" "spark_driver_nodepool" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "spark-driver-on-demand" }
    spec = {
      template = {
        metadata = { labels = { "workload-type" = "spark-driver" } }
        spec = {
          requirements = [
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["on-demand"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
          ]
          nodeClassRef = { group = "karpenter.k8s.aws", kind = "EC2NodeClass", name = "spark-jobs" }
          taints       = [{ key = "workload-type", value = "spark-driver", effect = "NoSchedule" }]
        }
      }
      limits     = { cpu = "16" } # small — sized to this exercise's actual data volume, not hypothetical big-data scale
      disruption = { consolidationPolicy = "WhenEmpty", consolidateAfter = "1m" }
    }
  })
  depends_on = [kubectl_manifest.spark_ec2_node_class]
}

# Spot NodePool: Spark EXECUTORS — Spark natively retries lost tasks on
# preemption, so spot's interruption risk is cheap to absorb here, and
# spot pricing meaningfully cuts cost for bursty, on-demand-triggered jobs.
resource "kubectl_manifest" "spark_executor_nodepool" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "spark-executor-spot" }
    spec = {
      template = {
        metadata = { labels = { "workload-type" = "spark-executor" } }
        spec = {
          requirements = [
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["spot"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
          ]
          nodeClassRef = { group = "karpenter.k8s.aws", kind = "EC2NodeClass", name = "spark-jobs" }
          taints       = [{ key = "workload-type", value = "spark-executor", effect = "NoSchedule" }]
        }
      }
      limits     = { cpu = "32" }
      disruption = { consolidationPolicy = "WhenEmpty", consolidateAfter = "1m" }
    }
  })
  depends_on = [kubectl_manifest.spark_ec2_node_class]
}

# --- Spark Operator: SparkApplication / ScheduledSparkApplication CRDs ---

resource "helm_release" "spark_operator" {
  name             = "spark-operator"
  repository       = "https://kubeflow.github.io/spark-operator"
  chart            = "spark-operator"
  version          = "2.0.2"
  namespace        = "churn-service"
  create_namespace = false

  set {
    name  = "webhook.enable"
    value = "true"
  }

  # Real bug found only by actually triggering a DAG run: the chart's
  # controller/webhook both default --namespaces=default (confirmed via
  # `kubectl logs` on the operator pod's startup args) — installing the
  # release INTO churn-service does NOT make it watch that namespace. Every
  # SparkApplication we submitted there sat with zero Events/Status forever;
  # the operator's controller-runtime watch never saw it. Must be set
  # explicitly to the namespace our SparkApplication CRs actually live in.
  set {
    name  = "spark.jobNamespaces[0]"
    value = "churn-service"
  }

  depends_on = [kubernetes_namespace.churn_service]
}

# --- Airflow: self-hosted, deliberately minimal/lite config ---
# Single scheduler, no HA, no worker autoscaling — see
# docs/architecture/03-orchestration.md for why this is a deliberate
# scope decision, not an oversight.
#
# Metadata DB is RDS, not the chart's built-in postgresql subchart — see
# modules/database/main.tf for why (Fargate can't attach the EBS volume
# that subchart's PVC needs; found by actually deploying, not designed
# in advance).

resource "kubernetes_secret" "airflow_metadata_db" {
  metadata {
    name      = "airflow-metadata-secret"
    namespace = kubernetes_namespace.churn_service.metadata[0].name
  }
  data = {
    connection = "postgresql://${var.airflow_db_username}:${var.airflow_db_password}@${var.airflow_db_endpoint}:5432/${var.airflow_db_name}?sslmode=require"
  }
  type = "Opaque"
}

resource "helm_release" "airflow" {
  name             = "airflow"
  repository       = "https://airflow.apache.org"
  chart            = "airflow"
  version          = "1.15.0"
  namespace        = "churn-service"
  create_namespace = false

  values = [yamlencode({
    executor = "KubernetesExecutor"
    # Keep failed KubernetesExecutor task pods around (chart default deletes
    # them immediately) — without this, a failing SparkKubernetesOperator/
    # KubernetesPodOperator task's pod vanishes in seconds with no remote
    # logging configured, making root-causing any real failure impossible
    # via kubectl. Found this the hard way debugging the first real
    # medallion_pipeline_dag run.
    config = {
      kubernetes_executor = {
        delete_worker_pods_on_failure = "False"
      }
    }
    scheduler = {
      replicas = 1 # minimal/lite — no HA, sized for a demo not production scale
      # Real finding: the chart's default startupProbe only allows 60s
      # (6 * 10s) for the container to start listening — Fargate's cold
      # start + gunicorn/scheduler boot time exceeds that here, causing
      # the kubelet to kill and restart the pod in a loop before it ever
      # gets a chance to finish starting. Widened to a 5-minute budget.
      startupProbe = {
        failureThreshold = 30
        periodSeconds    = 10
        timeoutSeconds   = 20
      }
      # Second, deeper real finding: this chart sets NO resource requests
      # by default. On Fargate (unlike a normal EC2 node with idle burst
      # capacity to spare), no request means the kubelet gives the pod a
      # minimal CPU share, which throttled gunicorn/Flask-AppBuilder's
      # cold-start RBAC table setup badly enough that it never finished
      # booting before even the widened startup probe timeout. Confirmed
      # via `kubectl get pod ... -o jsonpath='{.spec.containers[0].resources}'`
      # returning literally `{}` before this fix.
      resources = {
        requests = { cpu = "500m", memory = "1Gi" }
        limits   = { cpu = "1", memory = "2Gi" }
      }
    }
    webserver = {
      replicas = 1
      startupProbe = {
        failureThreshold = 30
        periodSeconds    = 10
        timeoutSeconds   = 20
      }
      resources = {
        requests = { cpu = "500m", memory = "1Gi" }
        limits   = { cpu = "1", memory = "2Gi" }
      }
    }
    postgresql = {
      enabled = false # RDS instead — see modules/database and the kubernetes_secret above
    }
    data = {
      metadataSecretName = kubernetes_secret.airflow_metadata_db.metadata[0].name
    }
    redis = {
      enabled = false # not needed: KubernetesExecutor doesn't use the Celery/Redis queue
    }
    triggerer = {
      # Real finding from actually deploying: this chart version's
      # Triggerer StatefulSet has an UNCONDITIONAL volumeClaimTemplate
      # for its logs (ignores logs.persistence.enabled=false), which is
      # EBS-backed and can't attach on Fargate — the pod sat Pending
      # forever (same root cause as the postgresql subchart, different
      # component). Disabled outright rather than fighting the chart's
      # PVC template: our DAGs use SparkKubernetesOperator and
      # KubernetesPodOperator, neither of which are deferrable operators
      # that would need the Triggerer.
      enabled = false
    }
    dags = {
      gitSync = {
        enabled = true
        repo    = var.dags_git_repo
        branch  = "main"
        subPath = "airflow/dags"
      }
    }
  })]

  timeout    = 600 # first-run DB migrations + git-sync clone can exceed the 300s default
  depends_on = [kubernetes_namespace.churn_service, kubernetes_secret.airflow_metadata_db]
}

# Real bug found by actually triggering medallion_pipeline_dag: the Airflow
# chart's default RBAC only covers core Airflow resources (pods, its own
# CRDs) — it grants nothing for the Spark Operator's CRDs. SparkKubernetesOperator
# calls the Kubernetes custom-objects API directly as the airflow-scheduler
# ServiceAccount, which failed with a 403:
# "sparkapplications.sparkoperator.k8s.io is forbidden: User
# system:serviceaccount:churn-service:airflow-scheduler cannot create
# resource sparkapplications" — confirmed via `airflow tasks test`, which
# runs the operator synchronously and surfaces the real traceback (the
# KubernetesExecutor pod route hid this: the pod's own process exits
# non-zero fast enough that only the DAG-level "failed" status was visible).
resource "kubernetes_role" "airflow_spark_operator_access" {
  metadata {
    name      = "airflow-spark-operator-access"
    namespace = "churn-service"
  }
  rule {
    api_groups = ["sparkoperator.k8s.io"]
    resources = [
      "sparkapplications", "sparkapplications/status",
      "scheduledsparkapplications", "scheduledsparkapplications/status",
    ]
    verbs = ["get", "list", "watch", "create", "update", "patch", "delete"]
  }
}

resource "kubernetes_role_binding" "airflow_spark_operator_access" {
  metadata {
    name      = "airflow-spark-operator-access"
    namespace = "churn-service"
  }
  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role.airflow_spark_operator_access.metadata[0].name
  }
  subject {
    kind      = "ServiceAccount"
    name      = "airflow-scheduler"
    namespace = "churn-service"
  }
  depends_on = [helm_release.airflow]
}
