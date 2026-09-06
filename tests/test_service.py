"""
Integration tests for the FastAPI service, run against the real persisted
model artifacts (requires `python -m scripts.run_training_pipeline` to
have been run at least once — skipped otherwise, since these artifacts
are a reproducible build output, not something committed to the repo).
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MODELS_DIR = Path(__file__).resolve().parents[1] / "models"

pytestmark = pytest.mark.skipif(
    not (MODELS_DIR / "xgboost_model.json").exists(),
    reason="model artifacts not found — run `python -m scripts.run_training_pipeline` first",
)


@pytest.fixture(scope="module")
def client():
    from src.service.app import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def a_real_customer_id():
    scores = json.loads((MODELS_DIR / "customer_scores.json").read_text())
    return scores[0]["customer_id"]


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["customers_cached"] > 0


def test_score_known_customer_returns_explanation(client, a_real_customer_id):
    response = client.get(f"/score/{a_real_customer_id}")
    assert response.status_code == 200
    body = response.json()
    assert 0.0 <= body["churn_probability"] <= 1.0
    assert body["source"] == "cache"
    assert body["rfm_segment"] in {"Champions", "Loyal", "At Risk", "Hibernating", "Lost"}
    assert len(body["explanation"]) > 0
    assert body["plain_language"]


def test_score_unknown_customer_404s(client):
    response = client.get("/score/definitely_not_a_real_customer")
    assert response.status_code == 404


def test_ingest_requires_auth(client):
    response = client.post("/events/ingest", json={"events": []})
    assert response.status_code in (401, 403)


def test_ingest_rejects_wrong_token(client):
    response = client.post(
        "/events/ingest", json={"events": []}, headers={"Authorization": "Bearer wrong-token"}
    )
    assert response.status_code == 401


def test_ingest_accepts_valid_event_with_correct_token(client):
    payload = {
        "events": [{
            "event_id": "evt_test_service_1", "customer_id": "cust_test",
            "event_type": "session", "timestamp": "2024-06-01T13:00:00Z",
            "properties": {"duration_sec": 120},
        }]
    }
    response = client.post(
        "/events/ingest", json=payload, headers={"Authorization": "Bearer local-dev-token"}
    )
    assert response.status_code == 200
    assert response.json()["accepted"] == 1


def test_ingest_rejects_invalid_event_type(client):
    payload = {
        "events": [{
            "event_id": "evt_bad", "customer_id": "cust_test",
            "event_type": "not_a_real_type", "timestamp": "2024-06-01T13:00:00Z",
            "properties": {},
        }]
    }
    response = client.post(
        "/events/ingest", json=payload, headers={"Authorization": "Bearer local-dev-token"}
    )
    assert response.status_code == 422
