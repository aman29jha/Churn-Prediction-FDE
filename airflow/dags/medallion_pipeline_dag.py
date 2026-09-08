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

max_active_runs=1: real bug found live-testing the fixed event trigger
chain — two ingest batches landing close together produced two
near-simultaneous Lambda invocations, both triggering this DAG, and the
two silver_transform tasks' concurrent SparkApplication submissions
raced on a driver configmap ("configmap ... not found", driver failed).
Beyond that specific k8s race, running two Silver->Gold passes
concurrently against the same Iceberg tables is a correctness risk on
its own (overlapping MERGE INTOs) — serializing runs is the right fix
either way, matching the pattern already used in live_simulator_dag and
training_dag.

Task-level retries, not the SparkApplication's own restartPolicy: a
second real bug, independent of the concurrency one above — a SINGLE,
non-concurrent Silver submission still occasionally hit the same
"configmap not found" driver-mount race (a known Spark Operator timing
issue: the driver pod's admission webhook can schedule it fractionally
before the controller's own async ConfigMap-creation call lands). The
Operator's in-place restartPolicy retries the SAME SparkApplication
object via a PENDING_RERUN mutation — but Airflow's own status polling
sometimes observes the transient FAILED state in between and gives up,
marking the Airflow task (and the whole DAG run) "failed" even though
the SparkApplication goes on to succeed under its own restarts a few
minutes later, orphaned from Airflow's perspective. Functionally the
data ends up correct either way, but Airflow's own DAG run history
showing spurious failures undermines the exact "this looks like a real,
trustworthy production pipeline" bar this whole fix pass is about.
Fixed by moving retries to the Airflow task level instead (retries=3
below; specs/{silver,gold}_spark_application.yaml's own
onFailureRetries set to 0) — each Airflow retry submits a genuinely NEW
SparkApplication object (Kubernetes generateName gives it a fresh
random suffix), sidestepping the specific PENDING_RERUN-mutation race
entirely, and Airflow's run history now accurately reflects what
actually happened.
"""
from datetime import datetime, timedelta

from airflow import DAG
from airflow.providers.cncf.kubernetes.operators.spark_kubernetes import SparkKubernetesOperator

with DAG(
    dag_id="medallion_pipeline_dag",
    description="Bronze -> Silver -> Gold, event-triggered",
    schedule=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["medallion", "spark"],
) as dag:
    silver = SparkKubernetesOperator(
        task_id="silver_transform",
        namespace="churn-service",
        application_file="specs/silver_spark_application.yaml",
        kubernetes_conn_id="kubernetes_default",
        do_xcom_push=False,
        retries=3,
        retry_delay=timedelta(seconds=30),
    )

    gold = SparkKubernetesOperator(
        task_id="gold_transform",
        namespace="churn-service",
        application_file="specs/gold_spark_application.yaml",
        kubernetes_conn_id="kubernetes_default",
        do_xcom_push=False,
        retries=3,
        retry_delay=timedelta(seconds=30),
    )

    silver >> gold
