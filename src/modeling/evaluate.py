"""
Evaluation metrics per docs/evaluation.md: PR-AUC (headline), recall @
fixed precision, top-decile capture/lift, F2 (used to pick the operating
threshold), Brier score (secondary, calibration). Threshold is
capacity-based (top ~15% of customers targeted), not an arbitrary 0.5 cut.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.metrics import average_precision_score, brier_score_loss, fbeta_score, precision_recall_curve


def capacity_threshold(scores: np.ndarray, capacity: float = 0.15) -> float:
    return float(np.quantile(scores, 1 - capacity))


def capacity_selection_mask(scores: np.ndarray, capacity: float = 0.15) -> np.ndarray:
    """Selects exactly the top `capacity` fraction by rank, breaking ties
    deterministically. A plain `scores >= quantile_threshold` comparison
    over-selects when the score distribution has heavy ties near the cutoff
    (e.g. a coarse baseline score with only ~13 distinct values) — this
    keeps the budget fixed regardless of score granularity, which is
    required for a fair baseline-vs-model comparison at the same capacity."""
    n_top = max(1, int(round(len(scores) * capacity)))
    top_idx = np.argsort(scores, kind="stable")[-n_top:]
    mask = np.zeros(len(scores), dtype=bool)
    mask[top_idx] = True
    return mask


def recall_at_precision(y_true: np.ndarray, scores: np.ndarray, target_precision: float = 0.40) -> float:
    precision, recall, _ = precision_recall_curve(y_true, scores)
    eligible = recall[precision >= target_precision]
    return float(eligible.max()) if len(eligible) else 0.0


def top_decile_capture(y_true: np.ndarray, scores: np.ndarray, decile: float = 0.10) -> float:
    n_top = max(1, int(len(scores) * decile))
    top_idx = np.argsort(scores)[-n_top:]
    total_positives = y_true.sum()
    if total_positives == 0:
        return 0.0
    return float(y_true[top_idx].sum() / total_positives)


def evaluate_scores(y_true: np.ndarray, scores: np.ndarray, capacity: float = 0.15) -> dict:
    threshold = capacity_threshold(scores, capacity)
    y_pred = capacity_selection_mask(scores, capacity).astype(int)

    return {
        "pr_auc": float(average_precision_score(y_true, scores)),
        "recall_at_precision_40": recall_at_precision(y_true, scores, 0.40),
        "top_decile_capture": top_decile_capture(y_true, scores, 0.10),
        "f2_at_capacity_threshold": float(fbeta_score(y_true, y_pred, beta=2, zero_division=0)),
        "precision_at_capacity_threshold": float((y_pred & (y_true == 1)).sum() / max(1, y_pred.sum())),
        "recall_at_capacity_threshold": float((y_pred & (y_true == 1)).sum() / max(1, y_true.sum())),
        "brier_score": float(brier_score_loss(y_true, scores)),
        "capacity_threshold_value": threshold,
        "n": int(len(y_true)),
        "positive_rate": float(y_true.mean()),
    }


def compare_baseline_vs_model(y_true: np.ndarray, baseline_scores: np.ndarray, model_scores: np.ndarray, capacity: float = 0.15) -> pd.DataFrame:
    baseline_metrics = evaluate_scores(y_true, baseline_scores, capacity)
    model_metrics = evaluate_scores(y_true, model_scores, capacity)
    return pd.DataFrame({"baseline": baseline_metrics, "xgboost": model_metrics}).T
