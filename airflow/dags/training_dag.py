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

Honest limitation: the labeled training set itself (data/synthetic/,
regenerated fresh from src/data_gen/bootstrap.py's fixed archetypes each
run) doesn't yet grow from live traffic — a live-simulator event needs a
full 60-day forward window before its outcome is even knowable, so there
isn't new ground truth to train on yet from a demo-length trickle. What's
real here is the full retrain -> S3 push -> next Gold run picks it up
loop, not (yet) the training *data* itself accumulating live signal.
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
        image="784004375291.dkr.ecr.ap-south-1.amazonaws.com/churn-fde-sandbox-api-service:sha-671a6f3-amd64",
        cmds=["python", "-m", "scripts.run_training_pipeline"],
        service_account_name="spark-jobs",
        # Real bug this DAG would have hit on its first-ever trigger (same
        # root cause already found and fixed in live_simulator_dag): the
        # "apps" Fargate profile requires this label, or the pod has
        # nowhere to schedule at all.
        labels={"fargate-scheduled": "true"},
        env_vars={"MODEL_REGISTRY_BUCKET": "churn-fde-sandbox-model-registry-784004375291"},
        container_resources=k8s.V1ResourceRequirements(
            requests={"cpu": "500m", "memory": "1Gi"},
            limits={"cpu": "1", "memory": "2Gi"},
        ),
        get_logs=True,
        is_delete_operator_pod=True,
    )
