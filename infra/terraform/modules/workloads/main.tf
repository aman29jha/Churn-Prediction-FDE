# api-service + console Deployments/Services/Ingress. See
# docs/architecture/04-serving.md and 06-reviewer-console.md.

resource "random_password" "ingest_token" {
  length  = 32
  special = false
}

resource "random_password" "console_password" {
  length  = 16
  special = false
}

resource "kubernetes_secret" "api_service" {
  metadata {
    name      = "api-service-secrets"
    namespace = var.namespace
  }
  data = { INGEST_TOKEN = random_password.ingest_token.result }
}

resource "kubernetes_secret" "console" {
  metadata {
    name      = "console-secrets"
    namespace = var.namespace
  }
  data = { CONSOLE_PASSWORD = random_password.console_password.result }
}

resource "kubernetes_service_account" "api_service" {
  metadata {
    name      = "api-service"
    namespace = var.namespace
    annotations = {
      "eks.amazonaws.com/role-arn" = var.api_service_role_arn
    }
  }
}

# Referenced by the Silver/Gold/Compaction/Analytics SparkApplication specs
# (airflow/dags/specs/*.yaml) and by modules/irsa's trust policy
# ("system:serviceaccount:churn-service:spark-jobs") — that trust policy
# was created before this ServiceAccount actually existed, which is fine
# since IAM trust policies don't require the K8s-side principal to exist
# yet, but the account itself was a real gap until now.
resource "kubernetes_service_account" "spark_jobs" {
  metadata {
    name      = "spark-jobs"
    namespace = var.namespace
    annotations = {
      "eks.amazonaws.com/role-arn" = var.spark_jobs_role_arn
    }
  }
}

# --- api-service ---
# Model artifacts are deliberately NOT baked into the image (they're
# retrained independently of service code — see docker/api-service/Dockerfile)
# — an initContainer syncs them from the S3 model registry into a shared
# emptyDir volume before the main container starts. Standard "pull
# artifacts before app starts" pattern.
resource "kubernetes_deployment_v1" "api_service" {
  metadata {
    name      = "api-service"
    namespace = var.namespace
    labels    = { app = "api-service" }
  }

  spec {
    replicas = 1 # small — sized to this exercise's actual request volume

    selector {
      match_labels = { app = "api-service" }
    }

    template {
      metadata {
        labels = { app = "api-service", "fargate-scheduled" = "true" }
      }
      spec {
        service_account_name = kubernetes_service_account.api_service.metadata[0].name

        init_container {
          name    = "sync-model-artifacts"
          image   = "public.ecr.aws/aws-cli/aws-cli:2.17.62"
          command = ["sh", "-c", "aws s3 sync s3://${var.model_registry_bucket}/ /models/ --region ${var.aws_region}"]
          volume_mount {
            name       = "models"
            mount_path = "/models"
          }
        }

        container {
          name  = "api-service"
          image = "${var.ecr_repository_urls["api-service"]}:${var.image_tag}"

          port {
            container_port = 8000
          }

          env {
            name = "INGEST_TOKEN"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.api_service.metadata[0].name
                key  = "INGEST_TOKEN"
              }
            }
          }
          env {
            name  = "ATHENA_DATABASE"
            value = var.glue_database_name
          }
          env {
            name  = "ATHENA_OUTPUT_LOCATION"
            value = "s3://${var.data_lake_bucket}/athena-results/"
          }

          volume_mount {
            name       = "models"
            mount_path = "/app/models"
          }

          resources {
            requests = { cpu = "250m", memory = "512Mi" }
            limits   = { cpu = "500m", memory = "1Gi" }
          }

          readiness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            initial_delay_seconds = 10
            period_seconds        = 10
          }
          liveness_probe {
            http_get {
              path = "/health"
              port = 8000
            }
            initial_delay_seconds = 20
            period_seconds        = 20
          }
        }

        volume {
          name = "models"
          empty_dir {}
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "api_service" {
  metadata {
    name      = "api-service"
    namespace = var.namespace
  }
  spec {
    selector = { app = "api-service" }
    port {
      port        = 80
      target_port = 8000
    }
    type = "ClusterIP"
  }
}

# --- console ---
# No init container needed: docs/ and reports/ are baked into the image
# at build time (real committed evidence artifacts), and the live-lookup
# tab calls api-service over the ClusterIP service, not S3 directly.
resource "kubernetes_deployment_v1" "console" {
  metadata {
    name      = "console"
    namespace = var.namespace
    labels    = { app = "console" }
  }

  spec {
    replicas = 1

    selector {
      match_labels = { app = "console" }
    }

    template {
      metadata {
        labels = { app = "console", "fargate-scheduled" = "true" }
      }
      spec {
        container {
          name  = "console"
          image = "${var.ecr_repository_urls["console"]}:${var.image_tag}"

          port {
            container_port = 8501
          }

          env {
            name  = "API_BASE_URL"
            value = "http://${kubernetes_service_v1.api_service.metadata[0].name}.${var.namespace}.svc.cluster.local"
          }
          env {
            name  = "SPARK_HISTORY_PATH"
            value = "/spark-history"
          }
          env {
            name  = "AIRFLOW_PATH"
            value = "/airflow"
          }
          env {
            name  = "CLOUDWATCH_DASHBOARD_URL"
            value = "https://${var.aws_region}.console.aws.amazon.com/cloudwatch/home?region=${var.aws_region}#dashboards:name=${var.dashboard_name}"
          }
          env {
            name = "CONSOLE_PASSWORD"
            value_from {
              secret_key_ref {
                name = kubernetes_secret.console.metadata[0].name
                key  = "CONSOLE_PASSWORD"
              }
            }
          }

          resources {
            requests = { cpu = "250m", memory = "512Mi" }
            limits   = { cpu = "500m", memory = "1Gi" }
          }

          readiness_probe {
            http_get {
              path = "/"
              port = 8501
            }
            initial_delay_seconds = 15
            period_seconds        = 10
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "console" {
  metadata {
    name      = "console"
    namespace = var.namespace
  }
  spec {
    selector = { app = "console" }
    port {
      port        = 80
      target_port = 8501
    }
    type = "ClusterIP"
  }
}

# --- Spark History Server ---
# Designed in docs/architecture/{01-data-platform,05-observability}.md
# but never actually deployed until now — a real gap. Reads the event
# logs the Silver/Gold/Analytics SparkApplications now write to
# s3a://.../spark-events/ (see airflow/dags/specs/*.yaml's sparkConf).
# Reuses the spark-jobs image (already has a full Spark install) and the
# spark-jobs IRSA role (already has S3 read access to the data lake
# bucket) — no new image or role needed.
# nginx sidecar strips the /spark-history prefix ALB can't rewrite itself
# before proxying to Spark locally — see the deployment's proxyBase
# comment for the full root-cause chain this fixes.
resource "kubernetes_config_map" "spark_history_nginx" {
  metadata {
    name      = "spark-history-nginx-conf"
    namespace = var.namespace
  }
  data = {
    "default.conf" = <<-EOT
      server {
        listen 8080;

        location /spark-history/ {
          rewrite ^/spark-history/(.*)$ /$1 break;
          proxy_pass http://127.0.0.1:18080;
          proxy_set_header Host $host;
          proxy_redirect off;
        }

        location = /spark-history {
          return 301 /spark-history/;
        }

        location / {
          return 404;
        }
      }
    EOT
  }
}

resource "kubernetes_deployment_v1" "spark_history" {
  metadata {
    name      = "spark-history-server"
    namespace = var.namespace
    labels    = { app = "spark-history-server" }
  }
  spec {
    replicas = 1
    selector {
      match_labels = { app = "spark-history-server" }
    }
    template {
      metadata {
        labels = { app = "spark-history-server", "fargate-scheduled" = "true" }
      }
      spec {
        service_account_name = kubernetes_service_account.spark_jobs.metadata[0].name
        container {
          name    = "spark-history-server"
          image   = "${var.ecr_repository_urls["spark-jobs"]}:${var.image_tag}"
          command = ["/opt/spark/bin/spark-class", "org.apache.spark.deploy.history.HistoryServer"]
          # fs.s3a.aws.credentials.provider must be set explicitly: real bug
          # found by actually running this against S3 — hadoop-aws 3.3.4's
          # DEFAULT credential provider chain (TemporaryAWSCredentialsProvider,
          # SimpleAWSCredentialsProvider, EnvironmentVariableCredentialsProvider,
          # IAMInstanceCredentialsProvider) does NOT include
          # WebIdentityTokenCredentialsProvider, so it never picks up the
          # AWS_ROLE_ARN / AWS_WEB_IDENTITY_TOKEN_FILE env vars that EKS's IRSA
          # webhook injects — failed with AccessDeniedException /
          # NoAuthWithAWSException despite the pod's ServiceAccount having a
          # real, correctly-scoped IAM role. Same fix required in every
          # SparkApplication spec (airflow/dags/specs/*.yaml) that reads/writes
          # s3a:// paths, for the identical reason.
          # spark.ui.proxyBase is REQUIRED behind a path-prefixed reverse
          # proxy like this ALB: real bug found visually — the app list
          # page loaded fine (HTTP 200), but rendered with zero rows even
          # though `GET /api/v1/applications` returns real completed apps
          # when hit directly. Root cause: the page's own JS calls
          # `setUIRoot('')`, so its AJAX calls to /api/v1/applications
          # target the domain ROOT, not /spark-history/api/v1/applications
          # — which the ALB's ingress rules route to the console's
          # catch-all "/" path instead of this service. spark.ui.proxyBase
          # is Spark's own documented mechanism for exactly this reverse-
          # proxy-path-prefix scenario; it makes setUIRoot (and every
          # internal link) correctly prefixed — confirmed it does fix
          # static asset links (CSS/JS now load, page was unstyled before).
          #
          # It does NOT fix the REST API servlet, though: Spark mounts
          # /api/v1/* rigidly, ignoring proxyBase for INCOMING request
          # matching (unlike the UI/static handlers, which are proxy-aware
          # both ways). ALB itself can't rewrite paths (no path-rewrite
          # action type, unlike nginx-ingress's rewrite-target annotation)
          # — so `GET /spark-history/api/v1/applications` reached this pod
          # fine but matched nothing Spark recognizes, falling through to
          # the default page. Fixed with a real nginx sidecar (below) that
          # strips the /spark-history prefix before proxying to Spark
          # locally — the standard pattern for exactly this ALB limitation.
          env {
            name  = "SPARK_HISTORY_OPTS"
            value = "-Dspark.history.fs.logDirectory=s3a://${var.data_lake_bucket}/spark-events/ -Dspark.history.ui.port=18080 -Dspark.hadoop.fs.s3a.aws.credentials.provider=com.amazonaws.auth.WebIdentityTokenCredentialsProvider -Dspark.ui.proxyBase=/spark-history"
          }
          port {
            container_port = 18080
          }
          resources {
            requests = { cpu = "250m", memory = "1Gi" }
            limits   = { cpu = "500m", memory = "2Gi" }
          }
          readiness_probe {
            http_get {
              path = "/"
              port = 18080
            }
            initial_delay_seconds = 20
            period_seconds        = 15
          }
        }
        container {
          name  = "nginx-proxy"
          image = "nginx:1.27-alpine"
          port {
            container_port = 8080
          }
          volume_mount {
            name       = "nginx-conf"
            mount_path = "/etc/nginx/conf.d"
          }
          resources {
            requests = { cpu = "50m", memory = "64Mi" }
            limits   = { cpu = "100m", memory = "128Mi" }
          }
          readiness_probe {
            http_get {
              path = "/spark-history/"
              port = 8080
            }
            initial_delay_seconds = 5
            period_seconds        = 10
          }
        }
        volume {
          name = "nginx-conf"
          config_map {
            name = kubernetes_config_map.spark_history_nginx.metadata[0].name
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "spark_history" {
  metadata {
    name      = "spark-history-server"
    namespace = var.namespace
  }
  spec {
    selector = { app = "spark-history-server" }
    port {
      # Routes through the nginx-proxy sidecar (8080), not directly at
      # Spark (18080) — nginx is what strips the /spark-history prefix.
      port        = 80
      target_port = 8080
    }
    type = "ClusterIP"
  }
}

# Routes to the Airflow webserver pod's nginx-proxy sidecar (see
# modules/k8s-addons's kubernetes_config_map.airflow_webserver_nginx and
# the airflow helm_release's webserver.extraContainers) — a separate,
# hand-written Service rather than the chart's own generated one, since
# the chart's Service targets the webserver container's port (8080)
# directly and doesn't know about this sidecar. Selector matches the
# chart's own pod labels exactly (tier/component/release), not a label
# this Terraform config controls.
resource "kubernetes_service_v1" "airflow_webserver_proxy" {
  metadata {
    name      = "airflow-webserver-proxy"
    namespace = var.namespace
  }
  spec {
    selector = { tier = "airflow", component = "webserver", release = "airflow" }
    port {
      port        = 80
      target_port = 8081
    }
    type = "ClusterIP"
  }
}

# --- Ingress: single ALB routing to both services ---
# target-type=ip is REQUIRED for Fargate — pods have no direct node IPs
# for the ALB to target the instance-mode way.
resource "kubernetes_ingress_v1" "main" {
  metadata {
    name      = "churn-service-ingress"
    namespace = var.namespace
    annotations = {
      "kubernetes.io/ingress.class"                = "alb"
      "alb.ingress.kubernetes.io/scheme"           = "internet-facing"
      "alb.ingress.kubernetes.io/target-type"      = "ip"
      "alb.ingress.kubernetes.io/healthcheck-path" = "/health"
    }
  }
  spec {
    rule {
      http {
        path {
          path      = "/score"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service_v1.api_service.metadata[0].name
              port { number = 80 }
            }
          }
        }
        path {
          path      = "/events"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service_v1.api_service.metadata[0].name
              port { number = 80 }
            }
          }
        }
        path {
          path      = "/health"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service_v1.api_service.metadata[0].name
              port { number = 80 }
            }
          }
        }
        path {
          path      = "/spark-history"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service_v1.spark_history.metadata[0].name
              port { number = 80 }
            }
          }
        }
        path {
          path      = "/airflow"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service_v1.airflow_webserver_proxy.metadata[0].name
              port { number = 80 }
            }
          }
        }
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service_v1.console.metadata[0].name
              port { number = 80 }
            }
          }
        }
      }
    }
  }
}
