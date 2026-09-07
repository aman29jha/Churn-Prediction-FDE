"""
FastAPI scoring + ingest service. See docs/architecture/04-serving.md and
docs/architecture/02-simulator.md.

Stand-ins still in place in the DEPLOYED service, not just locally — see
SUBMISSION.md and docs/architecture/04-serving.md's "Current implementation
status" for the honest gap this leaves (no live DynamoDB read, no
cold-start fallback for a customer_id outside the snapshot):
- `models/customer_scores.json` stands in for a live DynamoDB fast-lookup
  read — it's a snapshot synced from the S3 model registry at pod startup
  (refreshed from the real Gold Spark job's output), not a per-request read.
- `data/local_bronze/events.jsonl` stands in for the Bronze Iceberg table
  (real ingest in production appends directly to Bronze via a lightweight
  Iceberg writer, not a local file).
- A single shared-secret bearer token stands in for the real service-to-
  service auth — this one genuinely is what's deployed (a K8s Secret
  injecting the same token), not a stand-in.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.modeling.explain import build_explainer, explain_customer
from src.modeling.train import FEATURE_COLUMNS
from src.service.athena_client import run_athena_query
from src.service.metrics import put_metric
from src.service.schemas import ExplanationItem, IngestBatch, IngestResponse, ScoreResponse

REPO_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = REPO_ROOT / "models"
BRONZE_DIR = REPO_ROOT / "data" / "local_bronze"
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "local-dev-token")

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

    cache = _state["score_cache"]
    if customer_id not in cache:
        raise HTTPException(status_code=404, detail=f"customer_id '{customer_id}' not found in score cache")

    entry = cache[customer_id]
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
        source="cache",
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
        rows = run_athena_query(
            "SELECT segment, COUNT(*) as customers FROM rfm_segments GROUP BY segment ORDER BY customers DESC"
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Athena query failed: {exc}") from exc
    return {"rows": rows}


@app.post("/events/ingest", response_model=IngestResponse)
def ingest_events(batch: IngestBatch, request: Request, _auth: str = Depends(require_auth)):
    _check_rate_limit(request.client.host if request.client else "unknown")

    with open(BRONZE_DIR / "events.jsonl", "a") as f:
        for event in batch.events:
            f.write(event.model_dump_json() + "\n")

    return IngestResponse(accepted=len(batch.events))
