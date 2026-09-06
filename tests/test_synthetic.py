"""
Validation tests for the synthetic data generator (src/data_gen/bootstrap.py).

The key thing being validated is not "does it run" but "does it actually
fix the problem the real 80-row sample had" — i.e. does recency/frequency
now carry real, learnable signal about the churn outcome, per the
persistence-injection design documented in docs/modeling.md.
"""
import numpy as np
import pandas as pd
from sklearn.metrics import roc_auc_score

from src.data_gen.bootstrap import AS_OF, generate_synthetic_dataset
from src.features.rfm import compute_label, compute_rfm_features

N_CUSTOMERS = 300  # smaller than the real 1,200 target, for fast test runs
SEED = 7


def _generate():
    return generate_synthetic_dataset(n_customers=N_CUSTOMERS, seed=SEED)


def test_reproducible_with_fixed_seed():
    events_a, registry_a = _generate()
    events_b, registry_b = _generate()
    pd.testing.assert_frame_equal(registry_a, registry_b)
    assert events_a.equals(events_b)


def test_registry_has_one_row_per_customer():
    _, registry = _generate()
    assert len(registry) == N_CUSTOMERS
    assert registry["customer_id"].nunique() == N_CUSTOMERS


def test_churn_rate_near_target():
    _, registry = _generate()
    rate = registry["churn_outcome"].mean()
    # Target ~25% (see ARCHETYPES weights in bootstrap.py); allow generation
    # noise at this population size.
    assert 0.15 <= rate <= 0.35, f"churn rate {rate:.1%} drifted too far from the ~25% target"


def test_generated_events_produce_the_intended_label():
    """The events generated for a churn=1 customer must have no session
    after T, and a churn=0 customer must have at least one — verified by
    running the actual (already-tested) label function against the
    generated events, not just trusting the generation intent."""
    events, registry = _generate()
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)

    derived = compute_label(events, as_of=AS_OF, feature_window_days=60)
    merged = registry.merge(derived, on="customer_id", how="left")

    mismatches = merged[merged["churn_outcome"] != merged["churn"]]
    mismatch_rate = len(mismatches) / len(merged)
    # Allow a very small tolerance for edge-case timing (e.g. a forced
    # post-T session landing outside (T, as_of] due to a boundary draw),
    # but this should be near-exact since generation directly controls it.
    assert mismatch_rate < 0.02, (
        f"{mismatch_rate:.1%} of customers' generated events don't match their "
        f"intended churn_outcome — persistence injection isn't working correctly"
    )


def test_recency_carries_real_signal_unlike_the_real_sample():
    """This is the core validation: on the real 80-row sample, recency_days
    was NOT predictive of churn (AUC ~0.56, see docs/modeling.md). The whole
    point of the archetype/persistence injection is to fix that. If this
    test fails, the synthetic dataset has the same unlearnable-label
    problem as the raw sample and the modeling exercise is pointless."""
    events, registry = _generate()
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)

    features = compute_rfm_features(events, as_of=AS_OF, feature_window_days=60)
    merged = registry.merge(features, on="customer_id", how="left")
    merged["recency_days"] = merged["recency_days"].fillna(merged["recency_days"].max() * 1.5)

    auc = roc_auc_score(merged["churn_outcome"], merged["recency_days"])
    assert auc > 0.75, f"recency_days AUC {auc:.3f} is too weak — persistence injection isn't producing learnable signal"


def test_purchase_amounts_are_bimodal_not_single_lognormal():
    """Sanity check that the two-component mixture is actually producing
    both a small-purchase cluster and a large-purchase cluster, matching
    the real sample's bimodality rather than a smooth single distribution."""
    events, _ = _generate()
    purchases = events[events["event_type"] == "purchase"]
    amounts = purchases["properties"].apply(lambda p: p["amount_usd"])
    assert (amounts < 5).sum() > 10, "expected a real cluster of small purchases"
    assert (amounts > 8).sum() > 10, "expected a real cluster of larger purchases"


def test_acquisition_channel_correlates_with_archetype_as_documented():
    """Confirms the documented (invented) fairness-check correlation is
    actually present in the generated data, not just aspirational in a
    comment — paid acquisition should skew toward low_engagement."""
    _, registry = _generate()
    paid = registry[registry["acquisition_channel"] == "paid"]
    organic = registry[registry["acquisition_channel"] == "organic"]
    paid_low_rate = (paid["archetype"] == "low_engagement").mean()
    organic_low_rate = (organic["archetype"] == "low_engagement").mean()
    assert paid_low_rate > organic_low_rate
