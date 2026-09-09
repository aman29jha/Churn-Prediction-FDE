"""
Tests for select_as_of — the guardrail that decides whether training can
safely use a fresh (near-now) as_of, or must fall back to the validated
historical anchor. Pure pandas, no Spark needed.
"""
import pandas as pd
import pytest

from src.modeling.train import select_as_of

FALLBACK = pd.Timestamp("2024-06-01T12:00:00Z")


def _session_event(customer_id: str, timestamp: pd.Timestamp, idx: int) -> dict:
    return {
        "event_id": f"evt_{customer_id}_{idx}",
        "customer_id": customer_id,
        "event_type": "session",
        "timestamp": timestamp,
    }


def test_candidate_chosen_when_recent_activity_is_broad():
    """73% of customers active in the last 60 days before the candidate
    as_of (27% churned) -- matches this system's ~27% designed base rate
    -- a healthy, in-band churn rate -> candidate should be adopted."""
    candidate_as_of = pd.Timestamp("2026-09-08T00:00:00Z")
    events = []
    for i in range(100):
        customer_id = f"cust_{i:03d}"
        # 73 of 100 customers have a session inside the last 60 days (active);
        # the remaining 27 don't (churned).
        recent = i < 73
        ts = candidate_as_of - pd.Timedelta(days=10 if recent else 400)
        events.append(_session_event(customer_id, ts, i))
    events_df = pd.DataFrame(events)

    chosen, diagnostics = select_as_of(events_df, candidate_as_of=candidate_as_of, fallback_as_of=FALLBACK)

    assert chosen == candidate_as_of
    assert diagnostics["chosen_as_of"] == candidate_as_of.isoformat()
    assert diagnostics["candidate_positive_rate"] == pytest.approx(0.27, abs=0.01)  # 27 of 100 churned


def test_fallback_chosen_when_recent_activity_is_sparse():
    """Only a handful of customers touched recently (matching
    live_simulator's actual trickle density right now) -> churn rate
    collapses toward 100% -> must fall back, not adopt the candidate."""
    candidate_as_of = pd.Timestamp("2026-09-08T00:00:00Z")
    events = []
    for i in range(100):
        customer_id = f"cust_{i:03d}"
        # Only 2 of 100 customers have any recent activity -- exactly the
        # kind of sparse trickle live_simulator produces today.
        recent = i < 2
        ts = candidate_as_of - pd.Timedelta(days=10 if recent else 800)
        events.append(_session_event(customer_id, ts, i))
    events_df = pd.DataFrame(events)

    chosen, diagnostics = select_as_of(events_df, candidate_as_of=candidate_as_of, fallback_as_of=FALLBACK)

    assert chosen == FALLBACK
    assert diagnostics["chosen_as_of"] == FALLBACK.isoformat()
    assert diagnostics["candidate_positive_rate"] > 0.90
    assert "isn't dense enough" in diagnostics["reason"]


def test_diagnostics_always_report_the_candidate_rate_regardless_of_outcome():
    """The diagnostics dict must always be inspectable -- this is what a
    demo/report reads to show *why* a given as_of was chosen."""
    candidate_as_of = pd.Timestamp("2026-09-08T00:00:00Z")
    events = pd.DataFrame([_session_event("cust_000", candidate_as_of - pd.Timedelta(days=5000), 0)])

    _, diagnostics = select_as_of(events, candidate_as_of=candidate_as_of, fallback_as_of=FALLBACK)

    assert "candidate_positive_rate" in diagnostics
    assert "acceptable_band" in diagnostics
    assert "reason" in diagnostics
