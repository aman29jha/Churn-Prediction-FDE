"""
Training, deliberately plain Python/XGBoost (not Spark) — see
docs/modeling.md for why: the training set is ~1,200 rows, distributing
that would be cargo-culting. Real recurring schedule now (@daily) —
decoupled from the event-triggered medallion_pipeline_dag on purpose:
train on a schedule, score continuously as fresh data arrives
(docs/architecture/03-orchestration.md).

Real production retraining loop: scripts/run_training_pipeline.py now
pushes its trained artifacts (xgboost_model.json, baseline.pkl,
customer_scores.json) to the S3 model registry when MODEL_REGISTRY_BUCKET
is set (below) — that's the exact bucket gold_transform's
sync-model-artifacts initContainer reads from before every scoring run
(airflow/dags/specs/gold_spark_application.yaml), so a retrain here
genuinely changes what medallion_pipeline_dag scores customers with next,
not just a local artifact nobody reads.

The training set itself now genuinely grows from live traffic too:
DATA_LAKE_BUCKET (below) makes run_training_pipeline.py read the real,
live-accumulating Silver Iceberg table instead of regenerating a fixed
synthetic snapshot. Whether that translates into a *different* model
each day depends on select_as_of's guardrail (src/modeling/train.py):
it tries training with as_of=now() first, and only uses it if the
resulting label balance is healthy — live_simulator's trickle touches
too few customers per run for that yet, so today this still falls back
to the original bootstrap's validated historical cutoff. The day real
traffic is dense enough, this starts training on genuinely fresh data
automatically, no code change needed — see reports/training_diagnostics.json
(pushed to the model registry alongside the model) for exactly which
as_of was used on any given run, and why.
"""
from datetime import datetime

from airflow import DAG
from kubernetes.client import models as k8s

from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator

with DAG(
    dag_id="training_dag",
    description="XGBoost training (plain Python, not Spark) — scheduled retrain, pushes to S3 model registry",
    schedule="@daily",
    start_date=datetime(2024, 1, 1),
    catchup=False,
    max_active_runs=1,
    tags=["training"],
) as dag:
    train = KubernetesPodOperator(
        task_id="run_training_pipeline",
        namespace="churn-service",
        name="training",
        image="784004375291.dkr.ecr.ap-south-1.amazonaws.com/churn-fde-sandbox-api-service:sha-06086a5-amd64",
        cmds=["python", "-m", "scripts.run_training_pipeline"],
        service_account_name="spark-jobs",
        # Real bug this DAG would have hit on its first-ever trigger (same
        # root cause already found and fixed in live_simulator_dag): the
        # "apps" Fargate profile requires this label, or the pod has
        # nowhere to schedule at all.
        labels={"fargate-scheduled": "true"},
        env_vars={
            "MODEL_REGISTRY_BUCKET": "churn-fde-sandbox-model-registry-784004375291",
            "DATA_LAKE_BUCKET": "churn-fde-sandbox-data-lake-784004375291",
        },
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "500m", "memory": "1Gi"},
            limits={"cpu": "1", "memory": "2Gi"},
        ),
        get_logs=True,
        is_delete_operator_pod=True,
    )
