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
import time
from http.client import HTTPConnection
from urllib.parse import urlsplit

AIRFLOW_API_URL = os.environ["AIRFLOW_API_URL"]  # e.g. http://<alb-hostname>/airflow/api/v1
AIRFLOW_API_USERNAME = os.environ["AIRFLOW_API_USERNAME"]
AIRFLOW_API_PASSWORD = os.environ["AIRFLOW_API_PASSWORD"]
DAG_ID = "medallion_pipeline_dag"

# Real bug found before this Lambda's first-ever real invocation: it used
# to send "Authorization: Bearer <token>", but Airflow's REST API auth
# backend defaults to session (cookie-based) — a bearer token can never
# satisfy that, regardless of its value. infra/terraform/modules/k8s-addons
# now also enables basic_auth, which this matches.
_credentials = base64.b64encode(f"{AIRFLOW_API_USERNAME}:{AIRFLOW_API_PASSWORD}".encode()).decode()

# Second real bug found live-testing this same invocation, independent of
# the auth fix above: urllib.request.urlopen raised "OSError: [Errno 16]
# Device or resource busy" on every attempt — cold AND warm invocations
# alike, so not a one-off ENI cold-start race. Confirmed it wasn't a
# reachability problem either (same error against both the internal
# cluster-DNS URL and, after fixing that, a plain public ALB hostname).
# That combination points at urllib's OpenerDirector/proxy-detection
# machinery misbehaving in Lambda's network sandbox specifically, not at
# this handler's actual target. http.client is the same standard-library
# HTTP client one layer lower, without urlopen's automatic proxy/handler
# resolution — switched to it here, with a short bounded retry kept as
# defense-in-depth for genuine transient network blips.
_url = urlsplit(AIRFLOW_API_URL)


def _trigger_dag_run() -> int:
    conn = HTTPConnection(_url.hostname, _url.port or 80, timeout=10)
    try:
        conn.request(
            "POST",
            f"{_url.path}/dags/{DAG_ID}/dagRuns",
            body=json.dumps({}),
            headers={
                "Authorization": f"Basic {_credentials}",
                "Content-Type": "application/json",
            },
        )
        response = conn.getresponse()
        response.read()
        return response.status
    finally:
        conn.close()


def handler(event, context):
    message_count = len(event.get("Records", []))
    print(f"Triggering {DAG_ID}: {message_count} SQS message(s) in this batch")

    last_error = None
    for attempt in range(3):
        try:
            status = _trigger_dag_run()
            print(f"Airflow responded: {status}")
            return {"triggered": True, "message_count": message_count}
        except OSError as exc:
            last_error = exc
            print(f"Attempt {attempt + 1}/3 failed: {exc}")
            time.sleep(0.5 * (attempt + 1))
    raise last_error
