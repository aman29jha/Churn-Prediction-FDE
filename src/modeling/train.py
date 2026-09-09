"""
Label construction (via src/features/rfm.py) + stratified splitting +
XGBoost training. Training is deliberately plain Python, not Spark — the
dataset is ~1,200 rows; distributing that would be cargo-culting (see
docs/modeling.md).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
import xgboost as xgb
from sklearn.model_selection import train_test_split

from src.features.rfm import compute_label, compute_rfm_features

FEATURE_COLUMNS = [
    "recency_days", "frequency_30d", "frequency_90d",
    "purchase_count_90d", "purchase_revenue_90d", "lifetime_revenue", "has_ever_purchased",
    "push_open_rate", "campaign_click_count_90d", "support_ticket_count_90d",
    "avg_session_duration_90d", "add_to_cart_count_90d", "feature_use_count_90d",
]


@dataclass
class DatasetSplit:
    train: pd.DataFrame
    val: pd.DataFrame
    test: pd.DataFrame


def select_as_of(
    events: pd.DataFrame,
    candidate_as_of: pd.Timestamp,
    fallback_as_of: pd.Timestamp,
    feature_window_days: int = 60,
    min_positive_rate: float = 0.22,
    max_positive_rate: float = 0.32,
) -> tuple[pd.Timestamp, dict]:
    """Real design problem, not hypothetical: as live traffic accumulates
    in Silver, training should eventually use a fresher as_of (real
    recent history) instead of pinning forever to the original bootstrap
    dataset's fixed cutoff — otherwise "the model retrains daily" is true
    but "the model improves over time" never becomes true, since nothing
    new ever enters the window.

    The acceptable band is deliberately tight (±5pt around this system's
    validated ~27% base rate — docs/evaluation.md), not the wide
    [0.10, 0.50] first tried here: live-tested against real accumulated
    Silver data after ~a day of live_simulator's trickle, a loose band
    let through a candidate as_of whose label balance looked fine (36%)
    but was actually signal-diluted — PR-AUC dropped from 0.94 to 0.48,
    the RFM baseline's from 0.78 to 0.39. Root cause: live_simulator
    samples customers uniformly (weighted only by archetype population
    share, not by engagement propensity), so "touched at least once
    recently" trends toward meaning "existed long enough to be randomly
    picked," not "genuinely still engaged" — a materially weaker signal
    than the original archetype-driven historical label, even though the
    aggregate rate alone didn't obviously reveal that. A tight band
    anchored to the known-good rate catches this; a loose one doesn't.

    Naively using now() is unsafe *today* for a second, more obvious
    reason too: early on, live_simulator's trickle touches only a
    handful of the ~1,280 customers per run, so a recent as_of's label
    window (T, as_of] sees almost nobody active, collapsing the churn
    rate toward 100% instead of diluting it toward 0% — the opposite
    failure mode from the one above, caught by the same band.

    Net effect: only adopt the candidate as_of when live traffic density
    is both broad enough (this band) and not SO broad that "recently
    touched" stops discriminating between archetypes; otherwise fall
    back to the validated historical anchor. This makes the transition
    to genuinely-live training automatic the day real traffic quality
    supports it — not a manual cutover someone has to remember to make,
    and not a silent quality regression either.
    """
    events = events.copy()
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)
    candidate_labels = compute_label(events, as_of=candidate_as_of, feature_window_days=feature_window_days)
    candidate_rate = float(candidate_labels["churn"].mean()) if len(candidate_labels) else 1.0

    diagnostics = {
        "candidate_as_of": candidate_as_of.isoformat(),
        "candidate_positive_rate": round(candidate_rate, 4),
        "fallback_as_of": fallback_as_of.isoformat(),
        "acceptable_band": [min_positive_rate, max_positive_rate],
    }

    if min_positive_rate <= candidate_rate <= max_positive_rate:
        diagnostics["chosen_as_of"] = candidate_as_of.isoformat()
        diagnostics["reason"] = "live traffic density now supports a healthy label balance at the current date"
        return candidate_as_of, diagnostics

    diagnostics["chosen_as_of"] = fallback_as_of.isoformat()
    diagnostics["reason"] = (
        f"candidate as_of's churn rate ({candidate_rate:.1%}) falls outside the "
        f"[{min_positive_rate:.0%}, {max_positive_rate:.0%}] sane band -- live traffic isn't "
        "dense enough yet to retrain on real-time data without the label collapsing"
    )
    return fallback_as_of, diagnostics


def prepare_dataset(events: pd.DataFrame, registry: pd.DataFrame, as_of: pd.Timestamp, feature_window_days: int = 60) -> pd.DataFrame:
    events = events.copy()
    events["timestamp"] = pd.to_datetime(events["timestamp"], utc=True)

    features = compute_rfm_features(events, as_of=as_of, feature_window_days=feature_window_days)
    labels = compute_label(events, as_of=as_of, feature_window_days=feature_window_days)

    dataset = features.merge(labels, on="customer_id", how="left")
    dataset = dataset.merge(
        registry[["customer_id", "archetype", "plan_tier", "acquisition_channel", "region"]],
        on="customer_id", how="left",
    )
    dataset["has_ever_purchased"] = dataset["has_ever_purchased"].astype(int)
    return dataset


def split_dataset(dataset: pd.DataFrame, seed: int = 42) -> DatasetSplit:
    train, temp = train_test_split(dataset, test_size=0.30, stratify=dataset["churn"], random_state=seed)
    val, test = train_test_split(temp, test_size=0.50, stratify=temp["churn"], random_state=seed)
    return DatasetSplit(train=train, val=val, test=test)


def train_xgboost(train_df: pd.DataFrame, seed: int = 42) -> xgb.XGBClassifier:
    X = train_df[FEATURE_COLUMNS]
    y = train_df["churn"]

    # Native imbalance handling: upweight the minority (churn) class rather
    # than resampling.
    n_pos = (y == 1).sum()
    n_neg = (y == 0).sum()
    scale_pos_weight = n_neg / n_pos if n_pos > 0 else 1.0

    model = xgb.XGBClassifier(
        n_estimators=200, max_depth=4, learning_rate=0.05,
        scale_pos_weight=scale_pos_weight, eval_metric="aucpr",
        random_state=seed, missing=np.nan,  # XGBoost handles NaN (e.g. recency_days for cold-start) natively
    )
    model.fit(X, y)
    return model
