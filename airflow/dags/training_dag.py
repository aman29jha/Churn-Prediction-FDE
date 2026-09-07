"""
Training, deliberately plain Python/XGBoost (not Spark) — see
docs/modeling.md for why: the training set is ~1,200 rows, distributing
that would be cargo-culting. Manual trigger or infrequent (e.g. weekly)
schedule — decoupled from the event-triggered medallion_pipeline_dag on
purpose: train rarely, score often (docs/architecture/03-orchestration.md).

Known limitation, flagged honestly rather than silently incomplete:
scripts/run_training_pipeline.py currently reads/writes local paths
(data/synthetic/, models/) baked into the api-service image at build
time, not S3. Real model artifacts already exist in the S3 model
registry from a one-time manual upload done during initial deployment
(see docs/architecture/08-infrastructure.md) — wiring this DAG to do
that S3 read/write itself (via boto3, added to a training-specific
image or api-service's requirements) is the next real step, not yet
built.
"""
from datetime import datetime

from airflow import DAG
from kubernetes.client import models as k8s

from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

with DAG(
    dag_id="training_dag",
    description="XGBoost training (plain Python, not Spark) — manual/infrequent trigger",
    schedule=None,  # manual trigger; could be "@weekly" once the S3 I/O wiring above is done
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["training"],
) as dag:
    train = KubernetesPodOperator(
        task_id="run_training_pipeline",
        namespace="churn-service",
        name="training",
        image="784004375291.dkr.ecr.ap-south-1.amazonaws.com/churn-fde-sandbox-api-service:sha-17602ba-amd64",
        cmds=["python", "-m", "scripts.run_training_pipeline"],
        service_account_name="spark-jobs",
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "500m", "memory": "1Gi"},
            limits={"cpu": "1", "memory": "2Gi"},
        ),
        get_logs=True,
        is_delete_operator_pod=True,
    )
