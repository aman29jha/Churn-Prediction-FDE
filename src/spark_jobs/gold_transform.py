"""
Silver -> Gold Spark job. Computes RFM features/labels, applies the
trained model, and computes RFM quintile segments, per
docs/architecture/01-data-platform.md.

Deliberate design choice: this job converts the (already deduped/
validated, Spark-scale) Silver DataFrame to pandas via toPandas(), then
reuses the exact same tested functions from src/features/rfm.py and
src/modeling/{train,baseline}.py rather than reimplementing the RFM
logic a second time in PySpark. Why this is the right call, not a
shortcut: Silver's dedup/validation genuinely needs Spark because it
operates at raw-event scale; but after that step, the *output* of RFM
aggregation is customer-grained — a few thousand rows even at real
production scale, not something that benefits from distributed compute.
Reimplementing the same window/aggregation logic twice (once in pandas,
once in PySpark) would risk the two versions silently diverging over
time. Reusing proven code here is the "right tool per stage" principle
applied consistently, not an excuse to skip Spark.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import xgboost as xgb
from pyspark.sql import DataFrame, SparkSession

from src.features.rfm import compute_label, compute_rfm_features
from src.modeling.baseline import RFMQuintileBaseline
from src.modeling.train import FEATURE_COLUMNS


def run_gold_transform(
    spark: SparkSession,
    silver_df: DataFrame,
    as_of: pd.Timestamp,
    model_path: Path,
    baseline: RFMQuintileBaseline,
    feature_window_days: int = 60,
) -> dict[str, DataFrame]:
    events_pd = silver_df.toPandas()
    events_pd["timestamp"] = pd.to_datetime(events_pd["timestamp"], utc=True)

    features = compute_rfm_features(events_pd, as_of=as_of, feature_window_days=feature_window_days)
    labels = compute_label(events_pd, as_of=as_of, feature_window_days=feature_window_days)
    features_labeled = features.merge(labels, on="customer_id", how="left")

    model = xgb.XGBClassifier()
    model.load_model(str(model_path))
    features_labeled["churn_probability"] = model.predict_proba(features_labeled[FEATURE_COLUMNS])[:, 1]

    segments = baseline.score(features)

    return {
        "rfm_features": spark.createDataFrame(features),
        "churn_scores": spark.createDataFrame(features_labeled[["customer_id", "churn_probability", "churn"]]),
        "rfm_segments": spark.createDataFrame(segments),
    }
