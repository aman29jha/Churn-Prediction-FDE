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
from pyspark.sql import functions as F

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
    live_scoring: bool = False,
) -> dict[str, DataFrame]:
    """live_scoring=True (gold_transform's real, scheduled production
    path — see scripts/spark_job_entrypoint.py's --live-scoring flag)
    computes features from every event known up to `as_of` (normally
    "now"), not the offline train/eval path's held-out 60-day gap — see
    compute_rfm_features's exclude_recent_days docstring. False (the
    default, used for reproducible backtests) preserves the original
    leakage-safe windowing."""
    events_pd = silver_df.toPandas()
    events_pd["timestamp"] = pd.to_datetime(events_pd["timestamp"], utc=True)

    exclude_recent_days = 0 if live_scoring else None
    features = compute_rfm_features(
        events_pd, as_of=as_of, feature_window_days=feature_window_days, exclude_recent_days=exclude_recent_days
    )
    labels = compute_label(events_pd, as_of=as_of, feature_window_days=feature_window_days)
    features_labeled = features.merge(labels, on="customer_id", how="left")

    model = xgb.XGBClassifier()
    model.load_model(str(model_path))
    features_labeled["churn_probability"] = model.predict_proba(features_labeled[FEATURE_COLUMNS])[:, 1]

    segments = baseline.score(features)

    # run_date makes each table's Iceberg write a genuine upsert keyed on
    # (customer_id, run_date) rather than a blind overwrite — see
    # scripts/spark_job_entrypoint.py's MERGE INTO logic. Same-day re-runs
    # update in place (no duplicate rows); different days accumulate real
    # history, satisfying docs/architecture/07-analytics.md's stated
    # prerequisite for segment_migration/cohort_retention ("Gold tables
    # must retain history, append-only, partitioned by run_date").
    run_date = as_of.date().isoformat()

    return {
        "rfm_features": spark.createDataFrame(features).withColumn("run_date", F.lit(run_date)),
        "churn_scores": spark.createDataFrame(
            features_labeled[["customer_id", "churn_probability", "churn"]]
        ).withColumn("run_date", F.lit(run_date)),
        "rfm_segments": spark.createDataFrame(segments).withColumn("run_date", F.lit(run_date)),
    }
