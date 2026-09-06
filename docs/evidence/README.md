# Deployment Evidence — Localytics Churn Prediction FDE

Captured 2026-09-06 against the real AWS sandbox account (784004375291, ap-south-1),
in case account access has expired by the time this is reviewed.

- `eks_cluster_status.json` — real EKS cluster, status ACTIVE
- `pod_status.txt` — all 7 pods Running/Ready in the churn-service namespace
  (airflow-scheduler 3/3, airflow-webserver 1/1, api-service 1/1, console 1/1,
  spark-operator-controller 1/1, spark-operator-webhook 1/1, airflow-statsd 1/1)
- `ingress_status.txt` — real ALB provisioned by the AWS Load Balancer Controller
- `health_check.json` — GET /health through the real ALB
- `score_response.json` — GET /score/{customer_id} through the real ALB, with live
  SHAP explanation computed on-demand against the real trained model
- `ingest_success.json` — POST /events/ingest, authenticated, through the real ALB
- `airflow_dags_loaded.txt` — all 4 DAGs (medallion_pipeline_dag, training_dag,
  analytics_dag, live_simulator_dag) loaded successfully via git-sync from this
  repo's `airflow/dags/`, confirmed via `airflow dags list` inside the live
  scheduler pod

Also verified but not captured as a file: unauthenticated POST /events/ingest
correctly returns 401.

## Update 2026-09-07 — full medallion pipeline verified end-to-end

The initial deployment above covered the always-on services (API, console,
Airflow, Spark Operator). This update adds the Spark-on-K8s batch pipeline
itself, driven to a genuinely working state — not just designed — by
actually triggering `medallion_pipeline_dag` repeatedly against the real
account and fixing every real failure that surfaced, in order:

1. `docker/spark-jobs` was missing the `hadoop-aws` + `aws-java-sdk-bundle`
   jars — any `s3a://` access (History Server, Silver/Gold jobs alike)
   crashed with `ClassNotFoundException: S3AFileSystem`.
2. hadoop-aws's default S3A credential chain doesn't include
   `WebIdentityTokenCredentialsProvider` — IRSA's injected env vars were
   never picked up; set `spark.hadoop.fs.s3a.aws.credentials.provider`
   explicitly.
3. Airflow's chart-default RBAC never granted access to the Spark
   Operator's own CRDs — `SparkKubernetesOperator` hit a 403 creating
   `sparkapplications` as the `airflow-scheduler` ServiceAccount, then a
   second 403 on the `/status` subresource, then a **third** 403 once
   discovered that the actual KubernetesExecutor task pods run as a
   *different* ServiceAccount (`airflow-worker`) than the scheduler itself
   — each needed its own grant.
4. The `airflow-provider-cncf-kubernetes`'s `SparkKubernetesOperator`
   requires an explicit (even empty) `labels:` key under `driver`/
   `executor` in the submitted manifest, or it crashes with
   `KeyError: 'labels'` reading back the create-CR API response.
5. The EKS Fargate profile matched on namespace alone, so Fargate claimed
   Spark driver/executor pods meant for Karpenter's EC2 NodePools and
   failed them forever (`MatchNodeSelector failed`) — rescoped the
   profile to an explicit `fargate-scheduled` label, applied to every
   *other* workload in the namespace.
6. Karpenter's controller IAM role was missing IAM actions to
   self-manage an EC2 instance profile (`iam:GetInstanceProfile` et al.),
   then `ssm:GetParameter` (AMI alias resolution), then
   `ec2:DescribeImages` — the EC2NodeClass had never been Ready since
   before this session, for any Spark job, ever.
7. Even after Karpenter could launch a real EC2 instance, it never
   registered with the cluster — this EKS cluster uses the newer API
   `authentication_mode` (no aws-auth ConfigMap), which requires an
   explicit EKS Access Entry (type `EC2_LINUX`) for the node IAM role.
8. The `spark-jobs` Dockerfile's custom `ENTRYPOINT` (a hardcoded
   `spark-submit` invocation) broke Spark-on-K8s's own driver/executor
   dispatch contract with the base `apache/spark-py` image — the
   `"driver"` argument Kubernetes passes in landed in our own script's
   argparse instead of being consumed by the base image's dispatch logic.
9. The `spark-jobs` IRSA role was missing `s3:ListBucket` on the model
   registry bucket — `gold_transform`'s initContainer's `aws s3 sync`
   needs to list before it can get.

Each fix is documented with its root cause directly in the Terraform/
Dockerfile/spec that changed (see git log from `eef47d1` through the tip
of `main`).

**Final verified state**, captured in this update:
- `medallion_pipeline_dag_runs.txt` — real DAG run history via
  `airflow dags list-runs`, showing the final run's `state: success`
- `silver_output_listing.txt` / `gold_output_listing.txt` — real Parquet
  output in S3 with `_SUCCESS` markers: `silver/events/` (58,738 rows),
  `gold/rfm_features/`, `gold/churn_scores/`, `gold/rfm_segments/`
- `spark_history_applications.json` — Spark History Server's own REST
  API (`/api/v1/applications`), listing the real completed `silver-job`
  and `gold-job` runs with `"completed": true`
- `health_check_final.json` / `score_response_final.json` — the
  always-on API, re-verified live and working throughout this session
- `pod_status_final.txt` — all pods Running/Ready, including the
  previously crash-looping Spark History Server (now healthy) and no
  leftover SparkApplication CRs
