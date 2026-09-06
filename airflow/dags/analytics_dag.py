"""
Daily analytics pipeline (KPI trends, segment migration, cohort
retention) — see docs/architecture/07-analytics.md. Time-based, not
event-triggered like medallion_pipeline_dag: trend analysis doesn't need
low-latency refresh, and shouldn't couple to or slow down the
churn-scoring critical path.

Note: the actual KPI/migration/cohort aggregation logic
(src/spark_jobs/analytics_transform.py) is a documented next step, not
yet implemented as a distinct module — this DAG's SparkApplication spec
currently reuses the Gold entrypoint as a placeholder so the
orchestration wiring (schedule, retries, dependency shape) is real and
demonstrable even before that module exists.
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
