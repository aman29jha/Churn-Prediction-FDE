"""
Daily analytics pipeline (KPI trends, segment migration, cohort
retention) — see docs/architecture/07-analytics.md. Time-based, not
event-triggered like medallion_pipeline_dag: trend analysis doesn't need
low-latency refresh, and shouldn't couple to or slow down the
churn-scoring critical path.

Note: of the three tables documented, only kpi_daily is implemented
(src/spark_jobs/analytics_transform.py), writing a real Iceberg table via
the Glue Catalog. segment_migration and cohort_retention both need Gold's
tables to be append-only/partitioned by run_date first (a real
prerequisite the doc calls out) — not done in this pass, so those two
remain a documented next step rather than a placeholder standing in for
them.
"""
from datetime import datetime

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.spark_kubernetes import SparkKubernetesOperator

with DAG(
    dag_id="analytics_dag",
    description="Daily KPI trends / segment migration / cohort retention",
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["analytics", "spark"],
) as dag:
    run_analytics = SparkKubernetesOperator(
        task_id="run_analytics_transform",
        namespace="churn-service",
        application_file="specs/analytics_spark_application.yaml",
        kubernetes_conn_id="kubernetes_default",
        do_xcom_push=False,
    )
