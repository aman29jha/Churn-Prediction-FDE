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
