"""
Lambda handler for the event-driven pipeline trigger: S3 -> SNS -> SQS ->
this Lambda (native SQS batching: BatchSize=20 OR MaxBatchingWindow=300s)
-> Airflow REST API. See docs/architecture/03-orchestration.md.

Deliberately minimal: this function's only job is deciding WHEN to
trigger the pipeline (which SQS's own batching semantics mostly handle
already) and making the actual trigger call. All real transform logic
lives in src/spark_jobs/, not here.
"""
import json
import os
import urllib.request

AIRFLOW_API_URL = os.environ["AIRFLOW_API_URL"]  # e.g. http://airflow-webserver.churn-service.svc:8080/api/v1
AIRFLOW_API_TOKEN = os.environ["AIRFLOW_API_TOKEN"]
DAG_ID = "medallion_pipeline_dag"


def handler(event, context):
    message_count = len(event.get("Records", []))
    print(f"Triggering {DAG_ID}: {message_count} SQS message(s) in this batch")

    request = urllib.request.Request(
        url=f"{AIRFLOW_API_URL}/dags/{DAG_ID}/dagRuns",
        data=json.dumps({}).encode(),
        headers={
            "Authorization": f"Bearer {AIRFLOW_API_TOKEN}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        print(f"Airflow responded: {response.status}")

    return {"triggered": True, "message_count": message_count}
