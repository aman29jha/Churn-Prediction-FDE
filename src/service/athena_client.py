"""
Thin Athena query runner backing the console's Analytics tab. See
docs/architecture/07-analytics.md.

Runs queries against the real Iceberg tables (Glue Catalog, database
`churn_fde_sandbox`) via boto3 — synchronous start/poll/fetch, appropriate
here because every query in practice runs against a few-hundred-row table
and completes in ~1-2s; a long-running-query async pattern would be
solving a problem this dataset doesn't have. Requires ATHENA_DATABASE and
ATHENA_OUTPUT_LOCATION (an S3 prefix Athena stages results into) — see
infra/terraform/modules/workloads/main.tf for how those are set on the
deployed api-service, and modules/irsa/main.tf for the IAM grants this
needs (Athena query execution, Glue read, S3 read on the Iceberg
warehouse + the results-staging prefix).
"""
from __future__ import annotations

import os
import time

import boto3

ATHENA_DATABASE = os.environ.get("ATHENA_DATABASE", "churn_fde_sandbox")
ATHENA_OUTPUT_LOCATION = os.environ.get("ATHENA_OUTPUT_LOCATION", "")
ATHENA_POLL_INTERVAL_SECONDS = 0.5
ATHENA_QUERY_TIMEOUT_SECONDS = 30

_TERMINAL_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def run_athena_query(query: str) -> list[dict]:
    if not ATHENA_OUTPUT_LOCATION:
        raise RuntimeError("ATHENA_OUTPUT_LOCATION is not configured for this environment")

    client = boto3.client("athena")
    query_id = client.start_query_execution(
        QueryString=query,
        QueryExecutionContext={"Database": ATHENA_DATABASE},
        ResultConfiguration={"OutputLocation": ATHENA_OUTPUT_LOCATION},
    )["QueryExecutionId"]

    deadline = time.time() + ATHENA_QUERY_TIMEOUT_SECONDS
    state = "QUEUED"
    while time.time() < deadline:
        execution = client.get_query_execution(QueryExecutionId=query_id)["QueryExecution"]
        state = execution["Status"]["State"]
        if state in _TERMINAL_STATES:
            break
        time.sleep(ATHENA_POLL_INTERVAL_SECONDS)
    else:
        client.stop_query_execution(QueryExecutionId=query_id)
        raise TimeoutError(f"Athena query did not complete within {ATHENA_QUERY_TIMEOUT_SECONDS}s")

    if state != "SUCCEEDED":
        reason = execution["Status"].get("StateChangeReason", "no reason given")
        raise RuntimeError(f"Athena query {state}: {reason}")

    rows = client.get_query_results(QueryExecutionId=query_id)["ResultSet"]["Rows"]
    if not rows:
        return []
    columns = [cell.get("VarCharValue") for cell in rows[0]["Data"]]
    return [
        dict(zip(columns, [cell.get("VarCharValue") for cell in row["Data"]]))
        for row in rows[1:]
    ]
