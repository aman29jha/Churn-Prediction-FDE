"""
Fairness / bias check per docs/fairness.md: False Negative Rate (FNR)
parity as the primary metric (missing a real churner is the costlier
error, per docs/evaluation.md's cost asymmetry — not overall accuracy
parity), selection-rate parity as secondary context.

Slices on the synthetic segment fields (plan_tier, acquisition_channel,
region) — any finding here rests on an invented, documented correlation
(see docs/modeling.md), not observed Localytics customer behavior.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FINDING_RATIO_THRESHOLD = 1.25
FINDING_ABSOLUTE_GAP_THRESHOLD = 0.10


def _fnr(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    actual_positives = y_true == 1
    if actual_positives.sum() == 0:
        return float("nan")
    false_negatives = actual_positives & (y_pred == 0)
    return float(false_negatives.sum() / actual_positives.sum())


def subgroup_fairness_report(df: pd.DataFrame, y_true_col: str, y_pred_col: str, segment_col: str) -> pd.DataFrame:
    overall_fnr = _fnr(df[y_true_col].values, df[y_pred_col].values)
    overall_selection_rate = float(df[y_pred_col].mean())

    rows = []
    for segment_value, sub in df.groupby(segment_col):
        fnr = _fnr(sub[y_true_col].values, sub[y_pred_col].values)
        selection_rate = float(sub[y_pred_col].mean())
        ratio = fnr / overall_fnr if overall_fnr > 0 else float("nan")
        abs_gap = fnr - overall_fnr

        is_finding = (
            not np.isnan(ratio)
            and (ratio > FINDING_RATIO_THRESHOLD or abs_gap > FINDING_ABSOLUTE_GAP_THRESHOLD)
        )

        rows.append({
            "segment_field": segment_col,
            "segment_value": segment_value,
            "n": len(sub),
            "fnr": fnr,
            "overall_fnr": overall_fnr,
            "fnr_ratio_vs_overall": ratio,
            "selection_rate": selection_rate,
            "overall_selection_rate": overall_selection_rate,
            "is_finding": is_finding,
        })

    return pd.DataFrame(rows).sort_values("fnr_ratio_vs_overall", ascending=False)


def full_fairness_report(df: pd.DataFrame, y_true_col: str, y_pred_col: str, segment_cols: list[str]) -> pd.DataFrame:
    return pd.concat(
        [subgroup_fairness_report(df, y_true_col, y_pred_col, col) for col in segment_cols],
        ignore_index=True,
    )
