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


def _check_rate_limit(client_id: str):
    now = time.time()
    bucket = _rate_limit_buckets[client_id]
    while bucket and now - bucket[0] > RATE_LIMIT_WINDOW_SECONDS:
        bucket.popleft()
    if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
        raise HTTPException(status_code=429, detail="Rate limit exceeded")
    bucket.append(now)


def require_auth(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    if credentials.credentials != INGEST_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing bearer token")
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
        result = explain_customer(_state["explainer"], feature_row, top_k=5)
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


@app.post("/events/ingest", response_model=IngestResponse)
def ingest_events(batch: IngestBatch, request: Request, _auth: str = Depends(require_auth)):
    _check_rate_limit(request.client.host if request.client else "unknown")

    with open(BRONZE_DIR / "events.jsonl", "a") as f:
        for event in batch.events:
            f.write(event.model_dump_json() + "\n")

    return IngestResponse(accepted=len(batch.events))
