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
