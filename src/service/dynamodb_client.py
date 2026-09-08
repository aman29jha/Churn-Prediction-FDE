"""Thin wrapper around the `customer_scores` DynamoDB fast-lookup table —
see docs/architecture/04-serving.md. This is the real per-request serving
path: every gold_transform run writes each customer's current feature
vector + churn_probability + rfm_segment here (scripts/spark_job_entrypoint.py's
--dynamodb-table), so a score updates as soon as the next Gold run finishes,
with no pod restart needed.

Only active when CUSTOMER_SCORES_TABLE is set (the deployed environment) —
local dev/tests never call AWS and fall back to the JSON snapshot in
src/service/app.py.
"""
from __future__ import annotations

import os
from decimal import Decimal

import boto3

_TABLE_NAME = os.environ.get("CUSTOMER_SCORES_TABLE")
_table = None


def _get_table():
    global _table
    if _table is None and _TABLE_NAME:
        _table = boto3.resource("dynamodb").Table(_TABLE_NAME)
    return _table


def get_customer_score(customer_id: str) -> dict | None:
    """Live per-request read. Returns None on any failure (table not
    configured, item not found, transient AWS error) so callers can fall
    back to the cold-start snapshot rather than 500ing."""
    table = _get_table()
    if table is None:
        return None
    try:
        response = table.get_item(Key={"customer_id": customer_id})
    except Exception:
        return None
    item = response.get("Item")
    if not item:
        return None
    # DynamoDB's Python SDK returns numeric attributes as Decimal.
    return {k: (float(v) if isinstance(v, Decimal) else v) for k, v in item.items()}
