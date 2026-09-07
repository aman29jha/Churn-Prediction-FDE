"""
Real CloudWatch custom metrics for the "ChurnService" namespace — see
docs/architecture/05-observability.md and
infra/terraform/modules/observability/main.tf's dashboard/alarm
definitions, which were querying this exact namespace/these exact metric
names for the "Auth," "Rate limiting," and "Observability" panels from
the moment the dashboard was first deployed, with nothing ever actually
publishing to it (found from the dashboard itself: all three panels
showed "No data available" — confirmed by grepping app.py for any
CloudWatch/EMF code at all and finding none).

Deliberately synchronous, direct `put_metric_data` calls rather than the
`aws-embedded-metrics` EMF library or an async/batched approach: this
service's request volume is low enough (a take-home exercise, not real
production traffic) that the extra ~50-100ms per call is an acceptable,
explicitly-noted trade-off for keeping the instrumentation simple and
easy to verify — batching/async would be the right call at real scale.
Every call is wrapped so a CloudWatch API failure can never break the
actual request it's instrumenting.
"""
from __future__ import annotations

import boto3

NAMESPACE = "ChurnService"

_client = boto3.client("cloudwatch")


def put_metric(name: str, value: float = 1.0, unit: str = "Count") -> None:
    try:
        _client.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[{"MetricName": name, "Value": value, "Unit": unit}],
        )
    except Exception:
        # A metrics-emission failure must never fail the request it's
        # instrumenting — this is observability, not the request's own logic.
        pass
