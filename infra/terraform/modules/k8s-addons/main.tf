# Kubernetes-level installs, all via Helm so the whole stack stays
# infrastructure-as-code (no manual `kubectl apply` steps) — see
# docs/architecture/{01-data-platform,03-orchestration}.md for why each
# of these exists and the design decisions behind their configuration.

resource "kubernetes_namespace" "churn_service" {
  metadata {
    name = "churn-service"
  }
}

# --- Fargate log shipping: real gap found by checking actual CloudWatch
# log group CONTENT, not just that modules/observability's 8 groups exist.
# All 8 had zero log streams the entire project — a standard Fluent Bit
# DaemonSet (the architecture docs' original assumption) can't fix this,
# because DaemonSets can't schedule onto Fargate at all (no persistent
# node to run on), and nearly every pod here IS Fargate-scheduled (see
# modules/eks's "apps"/"system" profiles). Fargate ships logs via its own
# built-in log router instead, activated only by this specific namespace
# name + ConfigMap name existing (both AWS-mandated, not arbitrary) plus
# the pod execution role having CloudWatch Logs permissions (added in
# modules/eks). Routes each pod to the SAME per-component log group
# modules/observability already provisions and the dashboard/docs already
# reference — not a new catch-all group.
resource "kubernetes_namespace" "aws_observability" {
  metadata {
    name = "aws-observability"
    labels = {
      aws-observability = "enabled"
    }
  }
}

resource "kubernetes_config_map" "aws_logging" {
  metadata {
    name      = "aws-logging"
    namespace = kubernetes_namespace.aws_observability.metadata[0].name
  }

  data = {
    "filters.conf" = <<-EOT
      [FILTER]
          Name parser
          Match *
          Key_name log
          Parser crio

      [FILTER]
          Name kubernetes
          Match kube.*
          Merge_Log On
          Keep_Log Off
          Buffer_Size 0
          Kube_Meta_Cache_TTL 300s

      [FILTER]
          Name rewrite_tag
          Match kube.*
          Rule $kubernetes['pod_name'] silver-transform log.spark-silver false
          Rule $kubernetes['pod_name'] gold-transform log.spark-gold false
          Rule $kubernetes['pod_name'] compaction log.spark-compaction false
          Rule $kubernetes['pod_name'] analytics-transform log.spark-analytics false
          Rule $kubernetes['pod_name'] live-simulator log.live-simulator false
          Rule $kubernetes['pod_name'] ^api-service log.api-service false
          Rule $kubernetes['pod_name'] ^console log.console false
          Rule $kubernetes['pod_name'] ^airflow log.airflow false
          Rule $kubernetes['pod_name'] ^training-dag log.airflow false
          Rule $kubernetes['pod_name'] ^medallion-pipeline-dag log.airflow false
          Rule $kubernetes['pod_name'] ^analytics-dag log.airflow false
          Emitter_Name re_emitted
    EOT

    "output.conf" = <<-EOT
      [OUTPUT]
          Name cloudwatch_logs
          Match log.api-service
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/api-service
          log_stream_prefix from-fluent-bit-
          auto_create_group false

      [OUTPUT]
          Name cloudwatch_logs
          Match log.console
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/console
          log_stream_prefix from-fluent-bit-
          auto_create_group false

      [OUTPUT]
          Name cloudwatch_logs
          Match log.spark-silver
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/spark-silver
          log_stream_prefix from-fluent-bit-
          auto_create_group false

      [OUTPUT]
          Name cloudwatch_logs
          Match log.spark-gold
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/spark-gold
          log_stream_prefix from-fluent-bit-
          auto_create_group false

      [OUTPUT]
          Name cloudwatch_logs
          Match log.spark-compaction
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/spark-compaction
          log_stream_prefix from-fluent-bit-
          auto_create_group false

      [OUTPUT]
          Name cloudwatch_logs
          Match log.spark-analytics
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/spark-analytics
          log_stream_prefix from-fluent-bit-
          auto_create_group false

      [OUTPUT]
          Name cloudwatch_logs
          Match log.live-simulator
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/live-simulator
          log_stream_prefix from-fluent-bit-
          auto_create_group false

      [OUTPUT]
          Name cloudwatch_logs
          Match log.airflow
          region ${var.aws_region}
          log_group_name /eks/${var.project}-${var.environment}/airflow
          log_stream_prefix from-fluent-bit-
          auto_create_group false
    EOT
  }
}

# nginx sidecar on the Airflow webserver pod strips the /airflow prefix
# before proxying locally — same real ALB limitation as the Spark History
# Server fix in modules/workloads (ALB can't rewrite paths; nginx-ingress
# could, ALB can't). Airflow's own enable_proxy_fix (chart default: True)
# handles the rest via the X-Forwarded-Prefix header this config sets, so
# Flask's url_for() generates correctly-prefixed links without needing
# AIRFLOW__WEBSERVER__BASE_URL set (that's mainly for absolute links in
# outbound emails, which this deployment never sends).
resource "kubernetes_config_map" "airflow_webserver_nginx" {
  metadata {
    name      = "airflow-webserver-nginx-conf"
    namespace = "churn-service"
  }
  data = {
    # A FULL nginx.conf (mounted over /etc/nginx/nginx.conf, not just a
    # conf.d snippet) — real bug found from actually deploying: Airflow's
    # chart applies a restrictive non-root PodSecurityContext to every
    # container in this pod, sidecar included. Plain nginx:1.27-alpine's
    # default entrypoint tries to write its pid file, logs, and temp dirs
    # under /var/{run,log,cache}/nginx, all owned by root — crashed with
    # `mkdir() "/var/cache/nginx/client_temp" failed (13: Permission
    # denied)`. Every writable path nginx needs is redirected to /tmp,
    # which stays world-writable (1777) regardless of the UID a container
    # actually runs as.
    "nginx.conf" = <<-EOT
      worker_processes 1;
      pid /tmp/nginx.pid;
      error_log /tmp/nginx_error.log warn;

      events {
        worker_connections 1024;
      }

      http {
        client_body_temp_path /tmp/client_temp;
        proxy_temp_path       /tmp/proxy_temp;
        fastcgi_temp_path     /tmp/fastcgi_temp;
        uwsgi_temp_path       /tmp/uwsgi_temp;
        scgi_temp_path        /tmp/scgi_temp;
        access_log            /tmp/nginx_access.log;

        server {
          listen 8081;

          # Without this, nginx's automatic/return redirects build the
          # Location header from $server_port (8081, this sidecar's own
          # internal listen port) instead of the port the client actually
          # connected on. Real bug found by actually clicking the
          # console's "Open Airflow directly" link: it points at bare
          # `/airflow` (no trailing slash), nginx 301-redirected to
          # `http://<alb-host>:8081/airflow/`, and port 8081 isn't open on
          # the ALB (only 80 is) — so the link hung/failed to connect.
          # This makes the redirect port-agnostic so it matches whatever
          # port the client used (80 via the ALB).
          port_in_redirect off;

          location /airflow/ {
            rewrite ^/airflow/(.*)$ /$1 break;
            proxy_pass http://127.0.0.1:8080;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Prefix /airflow;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_redirect off;
          }

          location = /airflow {
            return 301 /airflow/;
          }

          location / {
            return 404;
          }
        }
      }
    EOT
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
          "ec2:DescribeAvailabilityZones", "ec2:DeleteLaunchTemplate", "ec2:CreateTags",
          # Found peeling back the previous IAM fix one layer at a time:
          # once InstanceProfile access worked, EC2NodeClass got past that
          # to AMI resolution — SSM parameter lookup succeeded, but the
          # follow-up call to fetch that AMI's full details failed with
          # its own separate AccessDenied.
          "ec2:DescribeImages"
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
      },
      # Real bug found only by actually triggering medallion_pipeline_dag:
      # the EC2NodeClass sat "Unknown"/never-ready from the moment it was
      # created (this had NEVER worked, since before this session's Spark
      # jobs were ever actually run) — Karpenter v1 self-manages the EC2
      # instance profile it launches nodes under (rather than requiring a
      # pre-created one), which needs these IAM actions; without them every
      # reconcile failed with `AccessDenied ... iam:GetInstanceProfile`,
      # which cascaded into "nodePool not ready" for both NodePools, which
      # is why driver/executor pods sat Pending forever even after the
      # Fargate/label fix correctly stopped Fargate from claiming them.
      # ssm:GetParameter is separately required for `amiSelectorTerms:
      # alias: al2023@latest` to resolve to a real AMI ID via SSM.
      {
        # Resource = "*" here is broader than AWS's own published Karpenter
        # controller policy, which scopes these to instance profiles matching
        # a naming/tag convention. Accepted as-is for this personal sandbox
        # account (same trade-off already made explicitly for the Glue
        # Catalog "*" in modules/irsa/main.tf) — worth tightening to a
        # condition on the "${var.project}-${var.environment}-*" naming
        # convention already used for aws_iam_instance_profile.node_instance
        # below before reusing this policy against a shared/production account.
        Sid    = "InstanceProfileManagement"
        Effect = "Allow"
        Action = [
          "iam:CreateInstanceProfile", "iam:TagInstanceProfile",
          "iam:AddRoleToInstanceProfile", "iam:RemoveRoleFromInstanceProfile",
          "iam:DeleteInstanceProfile", "iam:GetInstanceProfile",
        ]
        Resource = "*"
      },
      {
        Sid      = "AmiResolution"
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
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

# Real bug found only by actually triggering medallion_pipeline_dag, one
# layer deeper than the IAM fixes above: once Karpenter could actually
# launch an EC2 instance (visible as a real i-xxxx in `aws ec2
# describe-instances` and a Launched=True NodeClaim condition), it STILL
# never joined the cluster — `kubectl get nodeclaims` showed
# Registered=Unknown / "Node not registered with cluster" forever. This
# cluster uses EKS's newer API authentication_mode (no aws-auth ConfigMap;
# see modules/eks's access-entry resources for the deploying IAM
# principal) — under that mode, EVERY node IAM role needs its own EKS
# Access Entry of type EC2_LINUX, or its kubelet's bootstrap handshake is
# silently rejected with no error surfaced anywhere in Karpenter's own
# logs (it looks identical to a slow-to-boot node from Karpenter's side).
resource "aws_eks_access_entry" "karpenter_node" {
  cluster_name  = var.cluster_name
  principal_arn = aws_iam_role.node_instance.arn
  type          = "EC2_LINUX"
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

  # See eks module's "apps" Fargate profile: it now requires this label to
  # claim a pod (Spark driver/executor pods deliberately don't carry it, so
  # Karpenter provisions their nodes instead). The operator's own
  # controller/webhook pods must carry it explicitly or they'd be left
  # unscheduled themselves.
  # type="string" is REQUIRED: Terraform's helm_release `set` block
  # auto-coerces a bare "true"/"false" value to a YAML boolean, which then
  # fails applying with `json: cannot unmarshal bool into ... labels of
  # type string` — Kubernetes label VALUES must be strings, never bools.
  set {
    name  = "controller.labels.fargate-scheduled"
    value = "true"
    type  = "string"
  }
  set {
    name  = "webhook.labels.fargate-scheduled"
    value = "true"
    type  = "string"
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
    # Chart-wide label merged into every pod template this chart renders
    # (scheduler/webserver/statsd AND the KubernetesExecutor's own task-pod
    # template) — see eks module's "apps" Fargate profile for why this is
    # required for Fargate to claim these pods at all.
    labels = {
      "fargate-scheduled" = "true"
    }
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
      # Real bug found before this Lambda's first-ever real invocation
      # (it had never fired — Bronze got no real writes until
      # src/service/app.py's ingest fix): the default auth backend here
      # is session (cookie-based), which a Lambda calling the REST API
      # with a bearer token can never satisfy. basic_auth lets it
      # authenticate as the chart's own stock admin/admin user (already
      # documented in SUBMISSION.md) — session stays enabled too so the
      # browser UI login is unaffected.
      api = {
        auth_backends = "airflow.api.auth.backend.basic_auth,airflow.api.auth.backend.session"
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
      # See kubernetes_config_map.airflow_webserver_nginx above for why:
      # exposes Airflow's UI at /airflow through the same ALB the rest of
      # this service already uses (console/API/Spark History), instead of
      # kubectl port-forward-only access.
      extraContainers = [
        {
          name  = "nginx-proxy"
          image = "nginx:1.27-alpine"
          ports = [{ containerPort = 8081 }]
          volumeMounts = [
            { name = "nginx-conf", mountPath = "/etc/nginx/nginx.conf", subPath = "nginx.conf" },
          ]
          resources = {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = "100m", memory = "128Mi" }
          }
        },
      ]
      extraVolumes = [
        {
          name = "nginx-conf"
          configMap = {
            name = kubernetes_config_map.airflow_webserver_nginx.metadata[0].name
          }
        },
      ]
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
  # Real bug found by actually triggering medallion_pipeline_dag for real
  # (not via `airflow tasks test`, which runs inline as the scheduler
  # process and so incorrectly appeared to work): KubernetesExecutor's
  # actual task pods run under a SEPARATE ServiceAccount, "airflow-worker"
  # — not "airflow-scheduler" — so granting only the latter left every
  # real DAG run hitting its own fresh 403 Forbidden creating
  # sparkapplications, indistinguishable from the earlier RBAC bug except
  # for which ServiceAccount the error named. Both need this grant: the
  # scheduler for direct CLI/test invocations, the worker for real runs.
  subject {
    kind      = "ServiceAccount"
    name      = "airflow-scheduler"
    namespace = "churn-service"
  }
  subject {
    kind      = "ServiceAccount"
    name      = "airflow-worker"
    namespace = "churn-service"
  }
  depends_on = [helm_release.airflow]
}
