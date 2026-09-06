"""
Silver -> Gold, triggered by the S3->SNS->SQS->Lambda event chain (see
docs/architecture/03-orchestration.md) via the Airflow REST API, NOT on a
fixed cron schedule (schedule=None below). Real dependency chaining
(Gold only runs if Silver succeeded) and automatic retries, unlike the
earlier "independent CronJobs with manual time offsets" design this
replaced.

Live-demo safety net: this DAG can also be triggered manually from the
Airflow UI's native "Trigger DAG" button if the automated chain hiccups
during a reviewer call — no extra code needed for that fallback.
"""
from datetime import datetime

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.spark_kubernetes import SparkKubernetesOperator

with DAG(
    dag_id="medallion_pipeline_dag",
    description="Bronze -> Silver -> Gold, event-triggered",
    schedule=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["medallion", "spark"],
) as dag:
    silver = SparkKubernetesOperator(
        task_id="silver_transform",
        namespace="churn-service",
        application_file="specs/silver_spark_application.yaml",
        kubernetes_conn_id="kubernetes_default",
        do_xcom_push=False,
    )

    gold = SparkKubernetesOperator(
        task_id="gold_transform",
        namespace="churn-service",
        application_file="specs/gold_spark_application.yaml",
        kubernetes_conn_id="kubernetes_default",
        do_xcom_push=False,
    )

    silver >> gold
