"""
Classic 1-5 quintile RFM scoring + the required baseline heuristic.

Per docs/modeling.md: this is NOT where the churn label comes from (that
would be circular). It's the industry-standard segmentation the XGBoost
model has to beat, and it doubles as a stakeholder-facing dashboard
artifact (Champions/Loyal/At Risk/Hibernating/Lost).

Quintile thresholds are fit on a training population and frozen (stored
on the returned RFMQuintileBaseline object) so a customer's segment
doesn't drift just because the scoring population changed later.

Uses percentile thresholds + np.digitize rather than pd.cut with fixed
bin edges, because several features (lifetime_revenue especially, where
most customers are 0) have heavy ties that collapse pd.cut's quantile
edges into fewer than 5 usable bins.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

SEGMENT_ORDER = ["Lost", "Hibernating", "At Risk", "Loyal", "Champions"]
CHURN_PREDICTED_SEGMENTS = {"Lost", "Hibernating"}  # bottom 2 segments
_MISSING_RECENCY_FILL_MULTIPLIER = 1.5  # a customer with no session before T is treated as maximally inactive


def _fill_recency(recency_days: pd.Series) -> pd.Series:
    if recency_days.notna().any():
        fill_value = recency_days.max(skipna=True) * _MISSING_RECENCY_FILL_MULTIPLIER
    else:
        fill_value = 999.0
    return recency_days.fillna(fill_value)


def _percentile_thresholds(values: pd.Series) -> np.ndarray:
    return np.percentile(values.dropna(), [20, 40, 60, 80])


def _score_from_thresholds(values: pd.Series, thresholds: np.ndarray) -> np.ndarray:
    return np.digitize(values, thresholds) + 1  # -> integer scores 1-5


@dataclass
class RFMQuintileBaseline:
    recency_thresholds: np.ndarray
    frequency_thresholds: np.ndarray
    monetary_thresholds: np.ndarray
    combined_thresholds: np.ndarray

    def score(self, features: pd.DataFrame) -> pd.DataFrame:
        recency_filled = _fill_recency(features["recency_days"])
        # Recency is inverted: LOWER days-since-active = BETTER = higher score.
        r_score = 6 - _score_from_thresholds(recency_filled, self.recency_thresholds)
        f_score = _score_from_thresholds(features["frequency_90d"], self.frequency_thresholds)
        m_score = _score_from_thresholds(features["lifetime_revenue"], self.monetary_thresholds)
        combined = r_score + f_score + m_score

        segment_idx = _score_from_thresholds(pd.Series(combined), self.combined_thresholds) - 1
        segment = np.array(SEGMENT_ORDER)[np.clip(segment_idx, 0, 4)]
        predicted_churn = np.isin(segment, list(CHURN_PREDICTED_SEGMENTS)).astype(int)

        return pd.DataFrame({
            "customer_id": features["customer_id"].values,
            "r_score": r_score,
            "f_score": f_score,
            "m_score": m_score,
            "combined_score": combined,
            "segment": segment,
            "baseline_predicted_churn": predicted_churn,
        })


def fit_rfm_quintile_baseline(features: pd.DataFrame) -> RFMQuintileBaseline:
    recency_filled = _fill_recency(features["recency_days"])
    recency_thresholds = _percentile_thresholds(recency_filled)
    frequency_thresholds = _percentile_thresholds(features["frequency_90d"])
    monetary_thresholds = _percentile_thresholds(features["lifetime_revenue"])

    baseline = RFMQuintileBaseline(
        recency_thresholds=recency_thresholds, frequency_thresholds=frequency_thresholds,
        monetary_thresholds=monetary_thresholds, combined_thresholds=np.array([6, 8, 10, 12]),
    )
    # Combined score range is 3-15; fit real percentile thresholds on this
    # training population's actual combined-score distribution.
    scored = baseline.score(features)
    baseline.combined_thresholds = _percentile_thresholds(scored["combined_score"])
    return baseline
