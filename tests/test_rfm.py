"""
Unit tests for src/features/rfm.py, written against hand-verified expected
values computed independently from the raw data/events.json event listings
(see git history for the derivation). Written before the implementation.

as_of = 2024-06-01T12:00:00Z (given), feature cutoff T = as_of - 60d,
frequency/monetary lookback windows are 30d/90d back from T. Label = 1 if
no `session` event in (T, as_of].
"""
import math
from pathlib import Path

import pandas as pd
import pytest

from src.features.rfm import compute_label, compute_rfm_features, load_events

DATA_PATH = Path(__file__).resolve().parents[1] / "data" / "events.json"
AS_OF = pd.Timestamp("2024-06-01T12:00:00Z")

# Hand-verified expected values for 8 customers, chosen to cover every
# feature column with at least one non-zero value, plus two edge cases:
# cust_00053 has no `session` event at all before T (recency_days -> NaN),
# and cust_00037 / cust_00073 have zero events after T (churn -> 1).
EXPECTED = {
    "cust_00048": dict(
        recency_days=24.946991, frequency_30d=1, frequency_90d=1,
        purchase_count_90d=0, purchase_revenue_90d=0.0, lifetime_revenue=0.0,
        has_ever_purchased=False, push_open_rate=0.0, campaign_click_count_90d=0,
        support_ticket_count_90d=0, avg_session_duration_90d=182.0,
        add_to_cart_count_90d=0, feature_use_count_90d=0, churn=0,
    ),
    "cust_00053": dict(
        recency_days=None, frequency_30d=0, frequency_90d=0,
        purchase_count_90d=0, purchase_revenue_90d=0.0, lifetime_revenue=0.0,
        has_ever_purchased=False, push_open_rate=0.0, campaign_click_count_90d=0,
        support_ticket_count_90d=1, avg_session_duration_90d=0.0,
        add_to_cart_count_90d=0, feature_use_count_90d=0, churn=0,
    ),
    "cust_00070": dict(
        recency_days=145.169259, frequency_30d=0, frequency_90d=0,
        purchase_count_90d=0, purchase_revenue_90d=0.0, lifetime_revenue=1.15,
        has_ever_purchased=True, push_open_rate=0.0, campaign_click_count_90d=0,
        support_ticket_count_90d=0, avg_session_duration_90d=0.0,
        add_to_cart_count_90d=0, feature_use_count_90d=0, churn=0,
    ),
    "cust_00023": dict(
        recency_days=2.053113, frequency_30d=2, frequency_90d=2,
        purchase_count_90d=0, purchase_revenue_90d=0.0, lifetime_revenue=0.0,
        has_ever_purchased=False, push_open_rate=0.0, campaign_click_count_90d=0,
        support_ticket_count_90d=0, avg_session_duration_90d=128.5,
        add_to_cart_count_90d=0, feature_use_count_90d=0, churn=0,
    ),
    "cust_00018": dict(
        recency_days=16.399144, frequency_30d=1, frequency_90d=1,
        purchase_count_90d=0, purchase_revenue_90d=0.0, lifetime_revenue=0.0,
        has_ever_purchased=False, push_open_rate=0.0, campaign_click_count_90d=0,
        support_ticket_count_90d=0, avg_session_duration_90d=186.0,
        add_to_cart_count_90d=0, feature_use_count_90d=0, churn=1,
    ),
    "cust_00037": dict(
        recency_days=198.594641, frequency_30d=0, frequency_90d=0,
        purchase_count_90d=0, purchase_revenue_90d=0.0, lifetime_revenue=0.0,
        has_ever_purchased=False, push_open_rate=1.0, campaign_click_count_90d=0,
        support_ticket_count_90d=0, avg_session_duration_90d=0.0,
        add_to_cart_count_90d=0, feature_use_count_90d=1, churn=1,
    ),
    "cust_00073": dict(
        recency_days=29.294873, frequency_30d=1, frequency_90d=1,
        purchase_count_90d=0, purchase_revenue_90d=0.0, lifetime_revenue=0.0,
        has_ever_purchased=False, push_open_rate=0.0, campaign_click_count_90d=1,
        support_ticket_count_90d=0, avg_session_duration_90d=255.0,
        add_to_cart_count_90d=0, feature_use_count_90d=0, churn=1,
    ),
    "cust_00049": dict(
        recency_days=12.696157, frequency_30d=1, frequency_90d=1,
        purchase_count_90d=1, purchase_revenue_90d=18.42, lifetime_revenue=18.42,
        has_ever_purchased=True, push_open_rate=0.0, campaign_click_count_90d=0,
        support_ticket_count_90d=1, avg_session_duration_90d=217.0,
        add_to_cart_count_90d=1, feature_use_count_90d=0, churn=1,
    ),
}


@pytest.fixture(scope="module")
def events():
    return load_events(DATA_PATH)


@pytest.fixture(scope="module")
def features(events):
    return compute_rfm_features(events, as_of=AS_OF, feature_window_days=60).set_index("customer_id")


@pytest.fixture(scope="module")
def labels(events):
    return compute_label(events, as_of=AS_OF, feature_window_days=60).set_index("customer_id")["churn"]


@pytest.mark.parametrize("customer_id", list(EXPECTED.keys()))
def test_rfm_features_match_hand_verified_values(features, customer_id):
    row = features.loc[customer_id]
    expected = EXPECTED[customer_id]

    if expected["recency_days"] is None:
        assert math.isnan(row["recency_days"])
    else:
        assert row["recency_days"] == pytest.approx(expected["recency_days"], abs=1e-4)

    assert row["frequency_30d"] == expected["frequency_30d"]
    assert row["frequency_90d"] == expected["frequency_90d"]
    assert row["purchase_count_90d"] == expected["purchase_count_90d"]
    assert row["purchase_revenue_90d"] == pytest.approx(expected["purchase_revenue_90d"], abs=1e-6)
    assert row["lifetime_revenue"] == pytest.approx(expected["lifetime_revenue"], abs=1e-6)
    assert bool(row["has_ever_purchased"]) == expected["has_ever_purchased"]
    assert row["push_open_rate"] == pytest.approx(expected["push_open_rate"], abs=1e-6)
    assert row["campaign_click_count_90d"] == expected["campaign_click_count_90d"]
    assert row["support_ticket_count_90d"] == expected["support_ticket_count_90d"]
    assert row["avg_session_duration_90d"] == pytest.approx(expected["avg_session_duration_90d"], abs=1e-6)
    assert row["add_to_cart_count_90d"] == expected["add_to_cart_count_90d"]
    assert row["feature_use_count_90d"] == expected["feature_use_count_90d"]


@pytest.mark.parametrize("customer_id", list(EXPECTED.keys()))
def test_churn_label_matches_hand_verified_value(labels, customer_id):
    assert int(labels.loc[customer_id]) == EXPECTED[customer_id]["churn"]


def test_no_leakage_features_never_see_events_after_cutoff(events):
    """The feature computation must never use events after T, regardless of
    what happens in the label window — this is the core leakage guard."""
    T = AS_OF - pd.Timedelta(days=60)
    after_T = events[events["timestamp"] > T]
    assert len(after_T) > 0, "test fixture should have events after T to be meaningful"

    # Removing all post-T events should not change the computed features at all.
    pre_T_only = events[events["timestamp"] <= T]
    full = compute_rfm_features(events, as_of=AS_OF, feature_window_days=60).set_index("customer_id")
    truncated = compute_rfm_features(pre_T_only, as_of=AS_OF, feature_window_days=60).set_index("customer_id")

    common = full.index.intersection(truncated.index)
    pd.testing.assert_frame_equal(full.loc[common].sort_index(), truncated.loc[common].sort_index())


def test_exclude_recent_days_zero_uses_events_right_up_to_as_of(events):
    """Live scoring (gold_transform's --live-scoring) passes
    exclude_recent_days=0 so features reflect everything known right now,
    not the offline path's held-out 60-day gap. A synthetic session
    landing inside that gap (after T, at or before as_of) must change
    recency_days when exclude_recent_days=0, and must NOT when it's the
    default (the leakage guard from the test above)."""
    T = AS_OF - pd.Timedelta(days=60)
    fresh_session_time = T + pd.Timedelta(days=1)
    # events (the fixture) is already flattened (load_events calls
    # _ensure_flat) — the new row must match that shape (a flat
    # duration_sec column, no nested properties dict) or concatenating
    # produces both a "properties" and a "duration_sec" column at once.
    synthetic = pd.concat([
        events,
        pd.DataFrame([{
            "event_id": "e_fresh", "customer_id": "synthetic_fresh", "event_type": "session",
            "timestamp": fresh_session_time, "duration_sec": 100,
        }]),
    ], ignore_index=True)
    synthetic["timestamp"] = pd.to_datetime(synthetic["timestamp"], utc=True)

    default_gap = compute_rfm_features(synthetic, as_of=AS_OF, feature_window_days=60).set_index("customer_id")
    no_gap = compute_rfm_features(
        synthetic, as_of=AS_OF, feature_window_days=60, exclude_recent_days=0
    ).set_index("customer_id")

    # Default behavior: the fresh session is inside the excluded window,
    # so this customer has no pre-T session at all -> recency_days is NaN.
    assert math.isnan(default_gap.loc["synthetic_fresh", "recency_days"])
    # exclude_recent_days=0: the same session is now visible, giving a
    # real, finite recency measured from as_of (T + 1 day -> 59 days out).
    assert no_gap.loc["synthetic_fresh", "recency_days"] == pytest.approx(59.0)


def test_label_window_never_looks_before_cutoff(events):
    """The label must only reflect activity strictly after T — a customer whose
    only session is exactly at T should count as churned (window is (T, as_of])."""
    T = AS_OF - pd.Timedelta(days=60)
    synthetic = pd.DataFrame([
        {"event_id": "e1", "customer_id": "synthetic_edge", "event_type": "session",
         "timestamp": T, "properties": {"duration_sec": 100}},
    ])
    synthetic["timestamp"] = pd.to_datetime(synthetic["timestamp"], utc=True)
    label = compute_label(synthetic, as_of=AS_OF, feature_window_days=60).set_index("customer_id")
    assert int(label.loc["synthetic_edge", "churn"]) == 1


def test_row_per_customer_present_in_input(events):
    features = compute_rfm_features(events, as_of=AS_OF, feature_window_days=60)
    assert set(features["customer_id"]) == set(events["customer_id"].unique())
