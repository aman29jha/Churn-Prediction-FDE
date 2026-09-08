"""
FastAPI scoring + ingest service. See docs/architecture/04-serving.md and
docs/architecture/02-simulator.md.

Both the scoring read path and the ingest write path are the real,
deployed production paths, not stand-ins:
- `/score/{customer_id}` reads live from the `customer_scores` DynamoDB
  table first (src/service/dynamodb_client.py) — every gold_transform run
  writes each customer's current feature vector + churn_probability there,
  so a score reflects the latest Gold run with no pod restart needed.
  `models/customer_scores.json` (synced from S3 at pod startup) is kept
  only as a cold-start fallback for a customer_id DynamoDB doesn't have yet
  (e.g. a brand-new deployment before Gold has ever run).
- `/events/ingest` writes each batch as a real object under
  `s3://<data-lake-bucket>/bronze/live/...`, which is what the deployed
  S3 -> SNS -> SQS -> Lambda chain (infra/terraform/modules/messaging)
  watches to auto-trigger medallion_pipeline_dag — see
  docs/architecture/03-orchestration.md. Only falls back to the local
  `data/local_bronze/events.jsonl` file when DATA_LAKE_BUCKET isn't set
  (local dev / tests, no AWS calls made).
- A single shared-secret bearer token stands in for the real service-to-
  service auth — this one genuinely is what's deployed (a K8s Secret
  injecting the same token), not a stand-in.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import boto3
import numpy as np
import pandas as pd
import xgboost as xgb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.modeling.explain import build_explainer, explain_customer
from src.modeling.train import FEATURE_COLUMNS
from src.service.athena_client import run_athena_query
from src.service.dynamodb_client import get_customer_score
from src.service.metrics import put_metric
from src.service.schemas import ExplanationItem, IngestBatch, IngestResponse, ScoreResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "models"
BRONZE_DIR = REPO_ROOT / "data" / "local_bronze"
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "local-dev-token")
DATA_LAKE_BUCKET = os.environ.get("DATA_LAKE_BUCKET")

RATE_LIMIT_MAX_REQUESTS = 60
RATE_LIMIT_WINDOW_SECONDS = 60

_state: dict = {}
_rate_limit_buckets: dict[str, deque] = defaultdict(deque)


@asynccontextmanager
async def lifespan(app: FastAPI):
    model = xgb.XGBClassifier()
    model.load_model(str(MODELS_DIR / "xgboost_model.json"))
    _state["model"] = model
    _state["explainer"] = build_explainer(model)
    _state["feature_columns"] = json.loads((MODELS_DIR / "feature_columns.json").read_text())

    scores = pd.DataFrame(json.loads((MODELS_DIR / "customer_scores.json").read_text()))
    _state["score_cache"] = scores.set_index("customer_id").to_dict(orient="index")

    BRONZE_DIR.mkdir(parents=True, exist_ok=True)
    yield
    _state.clear()


app = FastAPI(title="Churn Prediction Service (local dev)", lifespan=lifespan)
security = HTTPBearer()


@app.middleware("http")
async def emit_request_metrics(request: Request, call_next):
    # Backs the CloudWatch dashboard's "Observability: latency / error
    # rate / throughput" panel (ChurnService/Latency, /ErrorRate,
    # /Throughput) — see this module's docstring and metrics.py's for the
    # real gap this closes. /health is excluded: it's the ALB target-group
    # + k8s probe endpoint, firing every ~10-20s regardless of real
    # traffic — including it would swamp the real request signal.
    if request.url.path == "/health":
        return await call_next(request)

    start = time.perf_counter()
    response = await call_next(request)
    latency_ms = (time.perf_counter() - start) * 1000

    put_metric("Latency", latency_ms, unit="Milliseconds")
    put_metric("Throughput", 1)
    put_metric("ErrorRate", 1.0 if response.status_code >= 500 else 0.0)
    return response


def _check_rate_limit(client_id: str):
    now = time.time()
    bucket = _rate_limit_buckets[client_id]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
        put_metric("RequestsThrottled")
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    bucket.append(now)
    put_metric("RequestsAllowed")


def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    if credentials.credentials != INGEST_TOKEN:
        put_metric("AuthFailure")
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
    put_metric("AuthSuccess")
    return credentials.credentials


@app.get("/health")
def health():
    return {"status": "ok", "customers_cached": len(_state.get("score_cache", {}))}


@app.get("/score/{customer_id}", response_model=ScoreResponse)
def score_customer(customer_id: str, request: Request, explain: bool = True):
    _check_rate_limit(request.client.host if request.client else "unknown")

    live_entry = get_customer_score(customer_id)
    if live_entry is not None:
        entry, source = live_entry, "dynamodb"
    else:
        cache = _state["score_cache"]
        if customer_id not in cache:
            raise HTTPException(status_code=404, detail=f"customer_id '{customer_id}' not found")
        entry, source = cache[customer_id], "cache"

    explanation, plain_language = None, None
    if explain:
        # SHAP is computed on-demand per docs/explainability.md, not
        # precomputed for every customer — this is still an O(1) lookup
        # (the feature row is already in the cache, no recompute from raw
        # events needed) followed by one fast TreeExplainer call.
        feature_row = pd.DataFrame([{c: entry.get(c, np.nan) for c in _state["feature_columns"]}])
        result = explain_customer(
            _state["explainer"], feature_row, top_k=5, churn_probability=entry["churn_probability"]
        )
        explanation = [ExplanationItem(**item) for item in result["explanation"]]
        plain_language = result["plain_language"]

    return ScoreResponse(
        customer_id=customer_id,
        churn_probability=entry["churn_probability"],
        rfm_segment=entry["rfm_segment"],
        source=source,
        explanation=explanation,
        plain_language=plain_language,
    )


@app.get("/analytics/kpi_daily")
def analytics_kpi_daily(request: Request):
    """Real time-series KPI data for the console's Analytics tab — see
    docs/architecture/07-analytics.md. Queries the real kpi_daily Iceberg
    table via Athena; not precomputed/cached, since this table is small
    and the console tab isn't a high-QPS path."""
    _check_rate_limit(request.client.host if request.client else "unknown")
    try:
        rows = run_athena_query(
            "SELECT event_date, dau, total_revenue, avg_revenue, purchase_count, "
            "push_open_rate, campaign_click_rate FROM kpi_daily ORDER BY event_date"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Athena query failed: {exc}") from exc
    return {"rows": rows}


@app.get("/analytics/segments")
def analytics_segments(request: Request):
    """RFM segment bucketing (Champions/Loyal/At Risk/Hibernating/Lost)
    for the console's Analytics tab — the real rfm_segments Iceberg table,
    queried live via Athena, not the training-time snapshot."""
    _check_rate_limit(request.client.host if request.client else "unknown")
    try:
        # Real bug found checking this chart's own numbers against the
        # known customer population: rfm_segments is append-only,
        # partitioned by run_date (see docs/architecture/01-data-platform.md
        # — that's the whole point, it's what makes segment-migration
        # analysis possible), so an unfiltered COUNT(*) sums every
        # historical run_date together — segment totals came back 2x the
        # real 1,280-customer population once a second run_date existed.
        # Scope to the latest snapshot only.
        rows = run_athena_query(
            "SELECT segment, COUNT(*) as customers FROM rfm_segments "
            "WHERE run_date = (SELECT MAX(run_date) FROM rfm_segments) "
            "GROUP BY segment ORDER BY customers DESC"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Athena query failed: {exc}") from exc
    return {"rows": rows}


@app.post("/events/ingest", response_model=IngestResponse)
def ingest_events(batch: IngestBatch, request: Request, _auth: str = Depends(require_auth)):
    _check_rate_limit(request.client.host if request.client else "unknown")

    if DATA_LAKE_BUCKET:
        # Real Bronze write: one object per batch, under the exact
        # "bronze/" prefix the S3 -> SNS -> SQS -> Lambda chain watches
        # (infra/terraform/modules/messaging) to auto-trigger
        # medallion_pipeline_dag. A JSON array (not NDJSON) since
        # silver_transform's Spark reader uses multiLine=true.
        #
        # Deliberately flat filenames, no date=/hour= partition-style
        # subdirectories: real failure hit live-testing this exact path —
        # Spark's partition inference chokes when a prefix mixes Hive-style
        # partitioned subdirectories with plain sibling files (the
        # bootstrap load's bronze/synthetic_events.json,
        # bronze/raw_sample_events.json), raising "AssertionError:
        # Conflicting directory structures detected" on read. silver_transform
        # reads the whole bronze/ prefix flatly every run anyway, so
        # partition directories bought nothing here regardless.
        now = datetime.now(timezone.utc)
        key = f"bronze/live/batch-{now.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
        body = "[" + ",".join(event.model_dump_json() for event in batch.events) + "]"
        boto3.client("s3").put_object(Bucket=DATA_LAKE_BUCKET, Key=key, Body=body.encode())
    else:
        with open(BRONZE_DIR / "events.jsonl", "a") as f:
            for event in batch.events:
                f.write(event.model_dump_json() + "\n")

    return IngestResponse(accepted=len(batch.events))
