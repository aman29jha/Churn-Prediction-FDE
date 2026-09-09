"""
End-to-end training pipeline: load events -> RFM features + label ->
baseline vs. XGBoost -> evaluation -> explainability -> fairness.

Run: python -m scripts.run_training_pipeline
Produces reports/metrics.json, reports/fairness.json, reports/global_shap_importance.png

Data source (real production behavior, not a hypothetical): when
DATA_LAKE_BUCKET is set (the deployed environment), this reads the real,
live-accumulating Silver Iceberg table's plain-Parquet output — the same
data lineage gold_transform scores from — instead of regenerating a fixed
synthetic snapshot. That's the real 1,200-customer bootstrap population
AND the 80-customer real sample (both already flow through Silver), plus
whatever live_simulator has trickled in since. Local dev/tests (no
DATA_LAKE_BUCKET) still regenerate the fixed-seed synthetic dataset
exactly as before, keeping that workflow untouched.

as_of is chosen by select_as_of (src/modeling/train.py), not hardcoded:
it tries the current time first, and only adopts it if the resulting
label balance is healthy -- otherwise it falls back to the original
bootstrap's validated historical cutoff. See that function's docstring
for the full reasoning. This is what makes "the model improves over
time" actually true eventually, without a manual cutover: the day
live traffic is dense enough, this starts training on genuinely fresh
data automatically.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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
from src.modeling.train import FEATURE_COLUMNS, prepare_dataset, select_as_of, split_dataset, train_xgboost

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "synthetic"
REPORTS_DIR = REPO_ROOT / "reports"
MODELS_DIR = REPO_ROOT / "models"
MODEL_REGISTRY_BUCKET = os.environ.get("MODEL_REGISTRY_BUCKET")
DATA_LAKE_BUCKET = os.environ.get("DATA_LAKE_BUCKET")


def _load_real_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    events = pd.read_parquet(f"s3://{DATA_LAKE_BUCKET}/silver/events/")
    import boto3

    registry_body = boto3.client("s3").get_object(
        Bucket=DATA_LAKE_BUCKET, Key="sim-state/customer_registry.json"
    )["Body"].read()
    registry = pd.DataFrame(json.loads(registry_body))
    return events, registry


def _load_local_synthetic_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    events_path = DATA_DIR / "synthetic_events.json"
    registry_path = DATA_DIR / "customer_registry.json"
    if not events_path.exists():
        print("Generating synthetic dataset (not found on disk)...")
        write_dataset(DATA_DIR, n_customers=1200, seed=42)
    events = pd.DataFrame(json.loads(events_path.read_text()))
    registry = pd.DataFrame(json.loads(registry_path.read_text()))
    return events, registry


def main():
    REPORTS_DIR.mkdir(exist_ok=True)

    if DATA_LAKE_BUCKET:
        print(f"Reading real accumulated events from s3://{DATA_LAKE_BUCKET}/silver/events/ ...")
        events, registry = _load_real_data()
    else:
        events, registry = _load_local_synthetic_data()
    print(f"Loaded {len(events)} events across {events['customer_id'].nunique()} customers")

    chosen_as_of, as_of_diagnostics = select_as_of(
        events, candidate_as_of=pd.Timestamp(datetime.now(timezone.utc)), fallback_as_of=AS_OF,
    )
    print(f"\n=== as_of selection ===\n{json.dumps(as_of_diagnostics, indent=2)}")
    (REPORTS_DIR / "training_diagnostics.json").write_text(json.dumps(as_of_diagnostics, indent=2))

    dataset = prepare_dataset(events, registry, as_of=chosen_as_of, feature_window_days=60)
    print(f"Dataset: {len(dataset)} customers, churn rate = {dataset['churn'].mean():.1%}")

    # Real-sample customers (data/events.json's 80 real customers, already
    # flowing through Silver alongside the synthetic bootstrap population)
    # have no entry in the synthetic customer_registry.json, so they carry
    # NaN plan_tier/acquisition_channel/region after the left-merge in
    # prepare_dataset. They're still real, valid training examples (XGBoost
    # only uses FEATURE_COLUMNS) -- only fairness slicing, which groups by
    # those segment columns, needs them excluded.
    missing_registry = dataset["plan_tier"].isna().sum()
    if missing_registry:
        print(f"{missing_registry} customer(s) have no registry segment data (real-sample customers) "
              f"-- included in training, excluded from fairness slicing below.")

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
    example_explanation = explain_customer(
        explainer, example_row, top_k=3, churn_probability=float(model_scores[top_risk_idx])
    )
    example_explanation["customer_id"] = example_customer_id
    example_explanation["churn_probability"] = float(model_scores[top_risk_idx])
    print(f"\n=== Example explanation ({example_customer_id}, p={model_scores[top_risk_idx]:.3f}) ===")
    print(example_explanation["plain_language"])
    (REPORTS_DIR / "example_explanation.json").write_text(json.dumps(example_explanation, indent=2))

    # --- Fairness ---
    fairness_df = split.test.copy()
    fairness_df["model_score"] = model_scores
    fairness_df["predicted_churn"] = capacity_selection_mask(model_scores, capacity=0.15).astype(int)
    # Real-sample customers have no registry segment data (see above) --
    # can't be sliced by plan_tier/acquisition_channel/region, so they're
    # excluded here specifically, not from training.
    fairness_df = fairness_df.dropna(subset=["plan_tier", "acquisition_channel", "region"])

    fairness_report = full_fairness_report(
        fairness_df, y_true_col="churn", y_pred_col="predicted_churn",
        segment_cols=["plan_tier", "acquisition_channel", "region"],
    )
    print("\n=== Fairness report (FNR parity) ===")
    print(fairness_report.round(4).to_string())
    fairness_report.round(6).to_json(REPORTS_DIR / "fairness.json", orient="records", indent=2)

    findings = fairness_report[fairness_report["is_finding"]]
    print(f"\n{len(findings)} fairness finding(s) flagged (FNR ratio > 1.25x or absolute gap > 10pt).")

    # --- Persist artifacts for the service (models/ is gitignored — a
    # reproducible build output, regenerated by re-running this script,
    # same reasoning as data/synthetic/ not being committed). ---
    MODELS_DIR.mkdir(exist_ok=True)
    model.save_model(str(MODELS_DIR / "xgboost_model.json"))
    (MODELS_DIR / "feature_columns.json").write_text(json.dumps(FEATURE_COLUMNS, indent=2))

    import pickle
    with open(MODELS_DIR / "baseline.pkl", "wb") as f:
        pickle.dump(baseline, f)

    # Score the FULL population (not just test) for the service's fast-path
    # lookup cache — this is the local stand-in for the DynamoDB
    # customer_scores table described in docs/architecture/04-serving.md.
    all_scores = model.predict_proba(dataset[FEATURE_COLUMNS])[:, 1]
    all_baseline = baseline.score(dataset)
    score_table = dataset[["customer_id", "plan_tier", "acquisition_channel", "region"] + FEATURE_COLUMNS].copy()
    score_table["churn_probability"] = all_scores
    score_table["rfm_segment"] = all_baseline["segment"].values
    score_table.to_json(MODELS_DIR / "customer_scores.json", orient="records", indent=2)
    print(f"\nPersisted model + baseline + {len(score_table)}-row score cache to {MODELS_DIR}")

    # Real production retraining loop, not a local-only artifact: push to
    # the S3 model registry so the NEXT gold_transform run (its
    # sync-model-artifacts initContainer does `aws s3 sync
    # s3://<registry>/ /models/` — see airflow/dags/specs/gold_spark_application.yaml)
    # actually picks up what this run just trained. Without
    # MODEL_REGISTRY_BUCKET set (local dev), this is a no-op — artifacts
    # stay local-only, same as before.
    if MODEL_REGISTRY_BUCKET:
        import boto3

        s3 = boto3.client("s3")
        for filename in ["xgboost_model.json", "feature_columns.json", "baseline.pkl", "customer_scores.json"]:
            s3.upload_file(str(MODELS_DIR / filename), MODEL_REGISTRY_BUCKET, filename)
        # training_diagnostics.json (which as_of was used and why) travels
        # alongside the model artifacts so it's inspectable without needing
        # this specific pod's logs -- see select_as_of's docstring.
        s3.upload_file(str(REPORTS_DIR / "training_diagnostics.json"), MODEL_REGISTRY_BUCKET, "training_diagnostics.json")
        print(f"Pushed 5 model registry artifacts to s3://{MODEL_REGISTRY_BUCKET}/")

    return comparison, importance, fairness_report, findings


if __name__ == "__main__":
    main()
