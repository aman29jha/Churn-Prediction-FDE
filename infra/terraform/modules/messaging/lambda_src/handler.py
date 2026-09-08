"""
Lambda handler for the event-driven pipeline trigger: S3 -> SNS -> SQS ->
this Lambda (native SQS batching: BatchSize=20 OR MaxBatchingWindow=300s)
-> Airflow REST API. See docs/architecture/03-orchestration.md.

Deliberately minimal: this function's only job is deciding WHEN to
trigger the pipeline (which SQS's own batching semantics mostly handle
already) and making the actual trigger call. All real transform logic
lives in src/spark_jobs/, not here.
"""
import base64
import json
import os
import urllib.request

AIRFLOW_API_URL = os.environ["AIRFLOW_API_URL"]  # e.g. http://airflow-webserver.churn-service.svc:8080/api/v1
AIRFLOW_API_USERNAME = os.environ["AIRFLOW_API_USERNAME"]
AIRFLOW_API_PASSWORD = os.environ["AIRFLOW_API_PASSWORD"]
DAG_ID = "medallion_pipeline_dag"

# Real bug found before this Lambda's first-ever real invocation: it used
# to send "Authorization: Bearer <token>", but Airflow's REST API auth
# backend defaults to session (cookie-based) — a bearer token can never
# satisfy that, regardless of its value. infra/terraform/modules/k8s-addons
# now also enables basic_auth, which this matches.
_credentials = base64.b64encode(f"{AIRFLOW_API_USERNAME}:{AIRFLOW_API_PASSWORD}".encode()).decode()


def handler(event, context):
    message_count = len(event.get("Records", []))
    print(f"Triggering {DAG_ID}: {message_count} SQS message(s) in this batch")

    request = urllib.request.Request(
        url=f"{AIRFLOW_API_URL}/dags/{DAG_ID}/dagRuns",
        data=json.dumps({}).encode(),
        headers={
            "Authorization": f"Basic {_credentials}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        print(f"Airflow responded: {response.status}")

    return {"triggered": True, "message_count": message_count}
