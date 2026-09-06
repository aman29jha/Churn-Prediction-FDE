"""
End-to-end local pipeline: generate/load synthetic data -> RFM features +
label -> baseline vs. XGBoost -> evaluation -> explainability -> fairness.

Run: python -m scripts.run_training_pipeline
Produces reports/metrics.json, reports/fairness.json, reports/global_shap_importance.png
"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from src.data_gen.bootstrap import AS_OF, write_dataset
from src.modeling.baseline import fit_rfm_quintile_baseline
from src.modeling.evaluate import capacity_selection_mask, compare_baseline_vs_model, evaluate_scores
from src.modeling.explain import build_explainer, explain_customer, global_feature_importance
from src.modeling.fairness import full_fairness_report
from src.modeling.train import FEATURE_COLUMNS, prepare_dataset, split_dataset, train_xgboost

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "synthetic"
REPORTS_DIR = REPO_ROOT / "reports"


def main():
    REPORTS_DIR.mkdir(exist_ok=True)

    events_path = DATA_DIR / "synthetic_events.json"
    registry_path = DATA_DIR / "customer_registry.json"
    if not events_path.exists():
        print("Generating synthetic dataset (not found on disk)...")
        write_dataset(DATA_DIR, n_customers=1200, seed=42)

    events = pd.DataFrame(json.loads(events_path.read_text()))
    registry = pd.DataFrame(json.loads(registry_path.read_text()))

    dataset = prepare_dataset(events, registry, as_of=AS_OF, feature_window_days=60)
    print(f"Dataset: {len(dataset)} customers, churn rate = {dataset['churn'].mean():.1%}")

    split = split_dataset(dataset, seed=42)
    print(f"Train/val/test sizes: {len(split.train)}/{len(split.val)}/{len(split.test)}")

    # --- Baseline ---
    baseline = fit_rfm_quintile_baseline(split.train)
    test_baseline_scores = baseline.score(split.test)
    # Risk score for evaluation = inverse of combined RFM score (lower combined
    # = higher risk), min-max normalized to [0,1] so it's comparable to a
    # probability for ranking/threshold metrics. Brier score is reported for
    # completeness but isn't very meaningful here — the baseline is a rank
    # heuristic, not a calibrated probability estimator.
    raw_risk = 15 - test_baseline_scores["combined_score"].values
    baseline_risk_scores = (raw_risk - raw_risk.min()) / max(1e-9, raw_risk.max() - raw_risk.min())

    # --- XGBoost ---
    model = train_xgboost(split.train, seed=42)
    X_test = split.test[FEATURE_COLUMNS]
    model_scores = model.predict_proba(X_test)[:, 1]

    y_test = split.test["churn"].values

    comparison = compare_baseline_vs_model(y_test, baseline_risk_scores, model_scores, capacity=0.15)
    print("\n=== Baseline vs XGBoost (test set) ===")
    print(comparison.round(4).to_string())
    comparison.round(6).to_json(REPORTS_DIR / "metrics.json", orient="index", indent=2)

    # --- Explainability ---
    explainer = build_explainer(model)
    importance = global_feature_importance(explainer, X_test)
    print("\n=== Global feature importance (mean |SHAP|) ===")
    print(importance.round(4).to_string())

    fig, ax = plt.subplots(figsize=(8, 5))
    importance.sort_values().plot.barh(ax=ax, color="#2e86c1")
    ax.set_xlabel("mean |SHAP value|")
    ax.set_title("Global feature importance — churn model")
    fig.tight_layout()
    fig.savefig(REPORTS_DIR / "global_shap_importance.png", dpi=150)
    plt.close(fig)

    # Example per-customer explanation (highest-risk test customer).
    top_risk_idx = int(np.argmax(model_scores))
    example_row = X_test.iloc[[top_risk_idx]]
    example_customer_id = split.test.iloc[top_risk_idx]["customer_id"]
    example_explanation = explain_customer(explainer, example_row, top_k=3)
    example_explanation["customer_id"] = example_customer_id
    example_explanation["churn_probability"] = float(model_scores[top_risk_idx])
    print(f"\n=== Example explanation ({example_customer_id}, p={model_scores[top_risk_idx]:.3f}) ===")
    print(example_explanation["plain_language"])
    (REPORTS_DIR / "example_explanation.json").write_text(json.dumps(example_explanation, indent=2))

    # --- Fairness ---
    fairness_df = split.test.copy()
    fairness_df["model_score"] = model_scores
    fairness_df["predicted_churn"] = capacity_selection_mask(model_scores, capacity=0.15).astype(int)

    fairness_report = full_fairness_report(
        fairness_df, y_true_col="churn", y_pred_col="predicted_churn",
        segment_cols=["plan_tier", "acquisition_channel", "region"],
    )
    print("\n=== Fairness report (FNR parity) ===")
    print(fairness_report.round(4).to_string())
    fairness_report.round(6).to_json(REPORTS_DIR / "fairness.json", orient="records", indent=2)

    findings = fairness_report[fairness_report["is_finding"]]
    print(f"\n{len(findings)} fairness finding(s) flagged (FNR ratio > 1.25x or absolute gap > 10pt).")

    return comparison, importance, fairness_report, findings


if __name__ == "__main__":
    main()
