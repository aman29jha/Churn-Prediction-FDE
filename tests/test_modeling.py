import numpy as np
import pandas as pd

from src.modeling.baseline import CHURN_PREDICTED_SEGMENTS, SEGMENT_ORDER, fit_rfm_quintile_baseline
from src.modeling.evaluate import capacity_selection_mask
from src.modeling.fairness import subgroup_fairness_report


def _toy_features(n=200, seed=0):
    rng = np.random.default_rng(seed)
    return pd.DataFrame({
        "customer_id": [f"c{i}" for i in range(n)],
        "recency_days": rng.exponential(30, n),
        "frequency_90d": rng.poisson(3, n),
        "lifetime_revenue": np.where(rng.random(n) < 0.5, 0.0, rng.uniform(1, 100, n)),
    })


def test_rfm_quintile_scores_are_in_valid_range():
    features = _toy_features()
    baseline = fit_rfm_quintile_baseline(features)
    scored = baseline.score(features)
    assert scored["r_score"].between(1, 5).all()
    assert scored["f_score"].between(1, 5).all()
    assert scored["m_score"].between(1, 5).all()
    assert scored["segment"].isin(SEGMENT_ORDER).all()


def test_lower_recency_gives_higher_r_score():
    """A customer active yesterday should score better than one active 300 days ago."""
    features = _toy_features()
    baseline = fit_rfm_quintile_baseline(features)

    recent = features.copy()
    recent["recency_days"] = 1.0
    dormant = features.copy()
    dormant["recency_days"] = 300.0

    assert baseline.score(recent)["r_score"].mean() > baseline.score(dormant)["r_score"].mean()


def test_bottom_two_segments_predict_churn():
    features = _toy_features()
    baseline = fit_rfm_quintile_baseline(features)
    scored = baseline.score(features)
    predicted = scored[scored["baseline_predicted_churn"] == 1]
    assert set(predicted["segment"].unique()) <= CHURN_PREDICTED_SEGMENTS


def test_capacity_selection_respects_fixed_budget_even_with_heavy_ties():
    """This is the bug found and fixed during the training pipeline run:
    a discrete/tied score distribution must not cause over-selection past
    the intended capacity."""
    scores = np.array([5] * 60 + [3] * 30 + [1] * 10)  # heavy ties at the top value
    mask = capacity_selection_mask(scores, capacity=0.15)
    assert mask.sum() == round(len(scores) * 0.15)


def test_fairness_report_flags_a_real_gap():
    df = pd.DataFrame({
        "churn": [1] * 20 + [1] * 20 + [0] * 60,
        "predicted_churn": [1] * 5 + [0] * 15 + [1] * 18 + [0] * 2 + [0] * 60,
        "segment": ["A"] * 20 + ["B"] * 20 + ["A"] * 30 + ["B"] * 30,
    })
    report = subgroup_fairness_report(df, "churn", "predicted_churn", "segment")
    a_row = report[report["segment_value"] == "A"].iloc[0]
    b_row = report[report["segment_value"] == "B"].iloc[0]
    assert a_row["fnr"] > b_row["fnr"]
    assert bool(a_row["is_finding"])
