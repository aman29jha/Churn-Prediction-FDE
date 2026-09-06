"""
Synthetic data generator (bootstrap / one-time historical load).

Per docs/modeling.md: fits event-level statistics (event mix, inter-arrival
timing, session durations, purchase amounts) from the real 80-customer
sample, but DELIBERATELY injects a persistent per-customer engagement
archetype driving both pre-cutoff behavior and the post-cutoff churn
outcome — the real sample doesn't exhibit that persistence (see the
AUC-0.56 finding in docs/modeling.md), so without injecting it the
synthetic dataset would inherit the same unlearnable-label problem.

This is an explicit, documented deviation from "faithfully replicate the
raw sample's temporal structure" — event-level marginal shapes (mix,
timing, amounts) are preserved; the archetype/persistence mechanism is
new and intentional.

Output: two DataFrames — `events` (same schema as data/events.json) and
`registry` (customer_id -> archetype + synthetic segment fields, reused
by the live trickle simulator and the fairness analysis).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

AS_OF = pd.Timestamp("2024-06-01T12:00:00Z")
FEATURE_WINDOW_DAYS = 60
HISTORY_SPAN_DAYS = 640  # matches the real sample's ~647-day max customer span

IN_APP_EVENT_NAMES = ["search", "feature_use", "screen_view", "add_to_cart", "share"]
SUPPORT_CATEGORIES = ["billing", "bug", "account", "other"]
PLAN_TIERS = ["free", "basic", "premium"]
REGIONS = ["north_america", "europe", "apac", "latam"]


@dataclass(frozen=True)
class Archetype:
    name: str
    population_weight: float
    churn_probability: float
    session_gap_scale_days: float  # gamma scale for inter-session gap; smaller = more frequent
    purchase_propensity: float  # P(customer ever purchases)
    push_engagement_multiplier: float  # scales push_sent volume and open rate
    in_app_rate_multiplier: float


# Calibrated so the population-weighted average churn rate lands at ~25%
# (an explicit, documented generation parameter — not an emergent accident).
ARCHETYPES = [
    Archetype("high_engagement", population_weight=0.35, churn_probability=0.10,
              session_gap_scale_days=6.0, purchase_propensity=0.70,
              push_engagement_multiplier=1.4, in_app_rate_multiplier=1.5),
    Archetype("medium_engagement", population_weight=0.40, churn_probability=0.25,
              session_gap_scale_days=14.0, purchase_propensity=0.50,
              push_engagement_multiplier=1.0, in_app_rate_multiplier=1.0),
    Archetype("low_engagement", population_weight=0.25, churn_probability=0.50,
              session_gap_scale_days=30.0, purchase_propensity=0.30,
              push_engagement_multiplier=0.6, in_app_rate_multiplier=0.5),
]

# Acquisition channel carries a plausible, modest, DOCUMENTED behavioral
# correlation with archetype (paid acquisition skews toward lower
# engagement) — invented for the fairness check, not observed data. See
# docs/fairness.md.
_ARCHETYPE_NAMES = [a.name for a in ARCHETYPES]
CHANNEL_ARCHETYPE_TILT = {
    "organic": {"high_engagement": 0.45, "medium_engagement": 0.40, "low_engagement": 0.15},
    "referral": {"high_engagement": 0.40, "medium_engagement": 0.40, "low_engagement": 0.20},
    "social": {"high_engagement": 0.30, "medium_engagement": 0.40, "low_engagement": 0.30},
    "paid": {"high_engagement": 0.20, "medium_engagement": 0.35, "low_engagement": 0.45},
}


def _gamma_gaps(rng: np.random.Generator, scale_days: float, n: int) -> np.ndarray:
    # shape=2 gives a mode > 0 with a right tail, matching the real sample's
    # dormancy-tail pattern (median ~18d, 90th pct ~86d, max 300+d) better
    # than a plain exponential.
    return rng.gamma(shape=2.0, scale=scale_days / 2.0, size=n)


def _purchase_amount(rng: np.random.Generator, n: int) -> np.ndarray:
    # Two-component mixture: small in-app buys vs. larger purchases,
    # matching the real sample's bimodality (25th pct $1.78, 75th pct
    # $17.65 — a single lognormal would smear this away).
    is_large = rng.random(n) < 0.35
    small = rng.uniform(0.99, 4.0, n)
    large = rng.uniform(8.0, 60.0, n)
    return np.where(is_large, large, small).round(2)


def _assign_archetypes(rng: np.random.Generator, n: int) -> np.ndarray:
    weights = [a.population_weight for a in ARCHETYPES]
    return rng.choice(_ARCHETYPE_NAMES, size=n, p=weights)


def _assign_segments(rng: np.random.Generator, archetypes: np.ndarray) -> pd.DataFrame:
    n = len(archetypes)
    plan_tier = rng.choice(PLAN_TIERS, size=n, p=[0.5, 0.35, 0.15])
    region = rng.choice(REGIONS, size=n)

    # Sample acquisition_channel conditioned on archetype (inverting the
    # documented channel->archetype tilt) so the correlation is real in the
    # generated data, not just aspirational.
    channels = list(CHANNEL_ARCHETYPE_TILT.keys())
    channel_weights_by_archetype = {
        arch: np.array([CHANNEL_ARCHETYPE_TILT[ch][arch] for ch in channels])
        for arch in _ARCHETYPE_NAMES
    }
    for w in channel_weights_by_archetype.values():
        w /= w.sum()

    acquisition_channel = np.empty(n, dtype=object)
    for arch in _ARCHETYPE_NAMES:
        mask = archetypes == arch
        count = mask.sum()
        if count:
            acquisition_channel[mask] = rng.choice(channels, size=count, p=channel_weights_by_archetype[arch])

    return pd.DataFrame({"plan_tier": plan_tier, "acquisition_channel": acquisition_channel, "region": region})


def _archetype_by_name(name: str) -> Archetype:
    return next(a for a in ARCHETYPES if a.name == name)


def _generate_customer_events(
    rng: np.random.Generator, customer_id: str, archetype: Archetype, churn: int, event_id_start: int
) -> tuple[list[dict], int]:
    events: list[dict] = []
    eid = event_id_start

    start_date = AS_OF - pd.Timedelta(days=int(rng.uniform(200, HISTORY_SPAN_DAYS)))
    if churn == 1:
        # Went quiet before T; how long before varies so recency has real spread.
        T = AS_OF - pd.Timedelta(days=FEATURE_WINDOW_DAYS)
        end_date = T - pd.Timedelta(days=rng.uniform(0, 150))
        end_date = max(end_date, start_date + pd.Timedelta(days=5))
    else:
        end_date = AS_OF

    # Sessions: gamma inter-arrival gaps from start_date to end_date.
    t = start_date
    while t < end_date:
        gap = _gamma_gaps(rng, archetype.session_gap_scale_days, 1)[0]
        t = t + pd.Timedelta(days=float(gap))
        if t >= end_date:
            break
        duration = max(9, rng.normal(183, 57))
        events.append({
            "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "session",
            "timestamp": t.isoformat(), "properties": {"duration_sec": round(float(duration))},
        })
        eid += 1

    if churn == 0:
        # Guarantee at least one session strictly after T, consistent with
        # the churn=0 label (this is what makes the label learnable from
        # recency/frequency rather than accidental).
        T = AS_OF - pd.Timedelta(days=FEATURE_WINDOW_DAYS)
        forced_ts = T + pd.Timedelta(days=float(rng.uniform(1, FEATURE_WINDOW_DAYS - 1)))
        if forced_ts < AS_OF:
            events.append({
                "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "session",
                "timestamp": forced_ts.isoformat(),
                "properties": {"duration_sec": round(float(max(9, rng.normal(183, 57))))},
            })
            eid += 1

    # Purchases.
    if rng.random() < archetype.purchase_propensity:
        n_purchases = rng.integers(1, 4)
        purchase_times = pd.to_datetime(
            rng.uniform(start_date.value, end_date.value, n_purchases)
        )
        amounts = _purchase_amount(rng, n_purchases)
        for ts, amt in zip(purchase_times, amounts):
            events.append({
                "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "purchase",
                "timestamp": pd.Timestamp(ts, tz="UTC").isoformat(), "properties": {"amount_usd": float(amt)},
            })
            eid += 1

    # Push sent/open.
    n_sent = rng.poisson(3 * archetype.push_engagement_multiplier)
    for _ in range(n_sent):
        ts = start_date + (end_date - start_date) * rng.random()
        campaign_id = f"camp_{rng.integers(1, 21):02d}"
        events.append({
            "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "push_sent",
            "timestamp": pd.Timestamp(ts).isoformat(), "properties": {"campaign_id": campaign_id},
        })
        eid += 1
        if rng.random() < min(0.95, 0.7 * archetype.push_engagement_multiplier):
            open_ts = ts + pd.Timedelta(hours=float(rng.uniform(0.1, 48)))
            if open_ts < AS_OF:
                events.append({
                    "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "push_open",
                    "timestamp": pd.Timestamp(open_ts).isoformat(), "properties": {"campaign_id": campaign_id},
                })
                eid += 1

    # Campaign clicks (independent small count, not chained to push events —
    # matches the real sample's lack of a real send->open->click funnel).
    n_clicks = rng.poisson(0.8 * archetype.push_engagement_multiplier)
    for _ in range(n_clicks):
        ts = start_date + (end_date - start_date) * rng.random()
        events.append({
            "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "campaign_click",
            "timestamp": pd.Timestamp(ts).isoformat(), "properties": {"campaign_id": f"camp_{rng.integers(1, 21):02d}"},
        })
        eid += 1

    # Support tickets (low, roughly archetype-independent).
    n_tickets = rng.poisson(0.4)
    for _ in range(n_tickets):
        ts = start_date + (end_date - start_date) * rng.random()
        events.append({
            "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "support_ticket",
            "timestamp": pd.Timestamp(ts).isoformat(),
            "properties": {"category": str(rng.choice(SUPPORT_CATEGORIES))},
        })
        eid += 1

    # In-app events.
    n_iae = rng.poisson(2.5 * archetype.in_app_rate_multiplier)
    for _ in range(n_iae):
        ts = start_date + (end_date - start_date) * rng.random()
        events.append({
            "event_id": f"evt_syn_{eid:08d}", "customer_id": customer_id, "event_type": "in_app_event",
            "timestamp": pd.Timestamp(ts).isoformat(),
            "properties": {"event_name": str(rng.choice(IN_APP_EVENT_NAMES))},
        })
        eid += 1

    return events, eid


def generate_synthetic_dataset(
    n_customers: int = 1200, seed: int = 42, as_of: pd.Timestamp = AS_OF
) -> tuple[pd.DataFrame, pd.DataFrame]:
    rng = np.random.default_rng(seed)

    customer_ids = [f"syn_cust_{i:05d}" for i in range(n_customers)]
    archetypes = _assign_archetypes(rng, n_customers)
    churn_probs = np.array([_archetype_by_name(a).churn_probability for a in archetypes])
    churn_outcomes = (rng.random(n_customers) < churn_probs).astype(int)
    segments = _assign_segments(rng, archetypes)

    registry = pd.DataFrame({
        "customer_id": customer_ids,
        "archetype": archetypes,
        "churn_outcome": churn_outcomes,
    })
    registry = pd.concat([registry, segments], axis=1)

    all_events: list[dict] = []
    eid = 1
    for cid, arch_name, churn in zip(customer_ids, archetypes, churn_outcomes):
        events, eid = _generate_customer_events(rng, cid, _archetype_by_name(arch_name), int(churn), eid)
        all_events.extend(events)

    events_df = pd.DataFrame(all_events)
    return events_df, registry


def write_dataset(out_dir: Path, n_customers: int = 1200, seed: int = 42) -> None:
    events_df, registry = generate_synthetic_dataset(n_customers=n_customers, seed=seed)
    out_dir.mkdir(parents=True, exist_ok=True)

    events_records = json.loads(events_df.to_json(orient="records"))
    with open(out_dir / "synthetic_events.json", "w") as f:
        json.dump(events_records, f, indent=2)

    registry.to_json(out_dir / "customer_registry.json", orient="records", indent=2)
    print(f"Wrote {len(events_df)} events across {n_customers} customers to {out_dir}")
    print(f"Churn rate: {registry['churn_outcome'].mean():.1%}")


if __name__ == "__main__":
    write_dataset(Path(__file__).resolve().parents[2] / "data" / "synthetic")
