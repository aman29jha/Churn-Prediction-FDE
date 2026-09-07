"""
Live trickle generator, run as an Airflow DAG rather than a raw K8s
CronJob specifically so start/pause uses Airflow's native per-DAG toggle
— see docs/architecture/02-simulator.md.

kubectl equivalent for reference:
  airflow dags pause live_simulator_dag    # pause
  airflow dags unpause live_simulator_dag  # resume
  (or the toggle switch in the Airflow UI, or "Trigger DAG" for a manual
  one-off run outside the schedule)
"""
from datetime import datetime

from airflow import DAG
from kubernetes.client import models as k8s

from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

with DAG(
    dag_id="live_simulator_dag",
    description="Recurring live trickle of synthetic events into /events/ingest",
    schedule="*/10 * * * *",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["simulator"],
) as dag:
    run_simulator = KubernetesPodOperator(
        task_id="run_live_simulator",
        namespace="churn-service",
        name="live-simulator",
        image="784004375291.dkr.ecr.ap-south-1.amazonaws.com/churn-fde-sandbox-api-service:sha-129bc09-amd64",
        cmds=["python", "-m", "src.data_gen.live_simulator"],
        service_account_name="spark-jobs",  # reuses the S3-read-capable IRSA role; ingest itself is auth'd via bearer token, not IAM
        # Real bug found by actually triggering this DAG: the "apps" Fargate
        # profile now requires this label (see modules/eks's Fargate profile
        # fix, needed so Fargate stops claiming Spark driver/executor pods
        # meant for Karpenter) — this pod has no Karpenter NodePool of its
        # own, so without the label it has nowhere to schedule at all:
        # "0/14 nodes are available ... untolerated taint
        # {eks.amazonaws.com/compute-type: fargate}".
        labels={"fargate-scheduled": "true"},
        env_vars={
            "API_BASE_URL": "http://api-service.churn-service.svc.cluster.local",
            "INGEST_TOKEN": "{{ var.value.ingest_token }}",  # set once via `airflow variables set ingest_token <value>` post-deploy
        },
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "100m", "memory": "256Mi"},
            limits={"cpu": "250m", "memory": "512Mi"},
        ),
        get_logs=True,
        is_delete_operator_pod=True,
    )
