"""
Live trickle generator — the recurring half of the simulator (see
docs/architecture/02-simulator.md). Runs as `live_simulator_dag` in
Airflow (KubernetesPodOperator), not a raw K8s CronJob, so start/pause
uses Airflow's native per-DAG toggle.

Each run: reads the customer registry (written once by bootstrap.py),
picks a handful of customers weighted by archetype activity rate,
generates 1-3 new events per customer using the SAME distributions as
the bootstrap generator but timestamped to NOW (not backdated), and
POSTs the batch to /events/ingest.
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
import requests

from src.data_gen.bootstrap import ARCHETYPES, IN_APP_EVENT_NAMES, SUPPORT_CATEGORIES, _archetype_by_name, _purchase_amount

API_BASE_URL = os.environ.get("API_BASE_URL", "http://api-service.churn-service.svc.cluster.local")
INGEST_TOKEN = os.environ["INGEST_TOKEN"]
REGISTRY_PATH = os.environ.get(
    "REGISTRY_PATH", "s3://churn-fde-sandbox-data-lake-784004375291/sim-state/customer_registry.json"
)
MIN_CUSTOMERS_PER_RUN = 5
MAX_CUSTOMERS_PER_RUN = 20


def _load_registry() -> pd.DataFrame:
    if REGISTRY_PATH.startswith("s3://"):
        return pd.read_json(REGISTRY_PATH)
    with open(REGISTRY_PATH) as f:
        return pd.DataFrame(json.load(f))


def _generate_event(rng: np.random.Generator, customer_id: str, archetype_name: str, event_id: str) -> dict:
    archetype = _archetype_by_name(archetype_name)
    now = pd.Timestamp.now(tz="UTC")

    weights = {
        # Inversely related to session_gap_scale_days: a smaller gap
        # (high-engagement archetype) means sessions happen more often.
        "session": 10.0 / archetype.session_gap_scale_days,
        "purchase": archetype.purchase_propensity * 0.2,
        "push_open": archetype.push_engagement_multiplier * 0.15,
        "campaign_click": archetype.push_engagement_multiplier * 0.1,
        "in_app_event": archetype.in_app_rate_multiplier * 0.2,
    }
    event_type = rng.choice(list(weights.keys()), p=np.array(list(weights.values())) / sum(weights.values()))

    if event_type == "session":
        properties = {"duration_sec": round(float(max(9, rng.normal(183, 57))))}
    elif event_type == "purchase":
        properties = {"amount_usd": float(_purchase_amount(rng, 1)[0])}
    elif event_type == "push_open":
        properties = {"campaign_id": f"camp_{rng.integers(1, 21):02d}"}
    elif event_type == "campaign_click":
        properties = {"campaign_id": f"camp_{rng.integers(1, 21):02d}"}
    else:
        properties = {"event_name": str(rng.choice(IN_APP_EVENT_NAMES))}

    return {
        "event_id": event_id,
        "customer_id": customer_id,
        "event_type": event_type,
        "timestamp": now.isoformat(),
        "properties": properties,
    }


def run_once(seed: int | None = None) -> int:
    rng = np.random.default_rng(seed)
    registry = _load_registry()

    n = rng.integers(MIN_CUSTOMERS_PER_RUN, MAX_CUSTOMERS_PER_RUN + 1)
    weights = registry["archetype"].map(lambda a: _archetype_by_name(a).population_weight)
    selected = registry.sample(n=min(n, len(registry)), weights=weights, random_state=None)

    events = []
    for _, row in selected.iterrows():
        n_events = rng.integers(1, 4)
        for i in range(n_events):
            event_id = f"evt_live_{row['customer_id']}_{pd.Timestamp.now().value}_{i}"
            events.append(_generate_event(rng, row["customer_id"], row["archetype"], event_id))

    response = requests.post(
        f"{API_BASE_URL}/events/ingest",
        json={"events": events},
        headers={"Authorization": f"Bearer {INGEST_TOKEN}"},
        timeout=10,
    )
    response.raise_for_status()
    accepted = response.json()["accepted"]
    print(f"Live simulator: generated {len(events)} events for {len(selected)} customers, ingest accepted {accepted}")
    return accepted


if __name__ == "__main__":
    run_once()
