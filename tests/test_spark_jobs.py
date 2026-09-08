"""
Local[*] PySpark validation of the Silver/Gold/Compaction logic, per the
staff-engineer execution plan's highest-leverage pre-AWS step: prove this
correct before it ever touches a real cluster.

The key check for Gold isn't "does it run" — it's that the Spark path
(toPandas -> reuse src/features/rfm.py) produces IDENTICAL output to
calling the pandas functions directly on the same events, since that
equivalence is the entire justification for not reimplementing the RFM
logic a second time in native PySpark (see gold_transform.py's docstring).
"""
import json
from pathlib import Path

import pandas as pd
import pytest

from src.data_gen.bootstrap import AS_OF
from src.features.rfm import compute_label, compute_rfm_features
from src.modeling.baseline import fit_rfm_quintile_baseline
from src.modeling.train import prepare_dataset
from src.spark_jobs.analytics_transform import run_kpi_daily_transform
from src.spark_jobs.compaction import compact_small_files, count_partitions
from src.spark_jobs.gold_transform import run_gold_transform
from src.spark_jobs.silver_transform import build_local_spark_session, run_silver_transform

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "synthetic"
MODELS_DIR = REPO_ROOT / "models"

pytestmark = pytest.mark.skipif(
    not (DATA_DIR / "synthetic_events.json").exists(),
    reason="synthetic dataset not found — run `python -m src.data_gen.bootstrap` first",
)


@pytest.fixture(scope="module")
def spark():
    session = build_local_spark_session("test-spark-jobs")
    yield session
    session.stop()


@pytest.fixture(scope="module")
def raw_events_pd():
    return pd.DataFrame(json.loads((DATA_DIR / "synthetic_events.json").read_text()))


@pytest.fixture(scope="module")
def bronze_df(spark, raw_events_pd):
    # Introduce a deliberate duplicate and an invalid event_type to prove
    # Silver's dedup/validation actually does something, not just pass
    # data through unchanged.
    sample = raw_events_pd.head(500).copy()
    duplicate_row = sample.iloc[[0]].copy()
    bad_row = sample.iloc[[1]].copy()
    bad_row["event_type"] = "not_a_real_event_type"
    bad_row["event_id"] = "evt_bad_type_test"

    with_issues = pd.concat([sample, duplicate_row, bad_row], ignore_index=True)
    with_issues_records = json.loads(with_issues.to_json(orient="records"))
    return spark.read.json(spark.sparkContext.parallelize([json.dumps(r) for r in with_issues_records]))


def test_silver_dedupes_and_drops_invalid_event_types(bronze_df):
    input_count = bronze_df.count()
    silver = run_silver_transform(bronze_df)
    output_count = silver.count()

    # input had +2 rows injected (1 duplicate, 1 invalid type) vs the
    # original 500-row sample; output should have exactly 500 (dup removed,
    # invalid type quarantined).
    assert input_count == 502
    assert output_count == 500
    assert "not_a_real_event_type" not in [r["event_type"] for r in silver.select("event_type").distinct().collect()]


def test_silver_flattens_properties_into_typed_columns(bronze_df):
    silver = run_silver_transform(bronze_df)
    assert "properties" not in silver.columns
    assert "duration_sec" in silver.columns
    assert "amount_usd" in silver.columns


def test_gold_matches_pandas_path_exactly(spark, raw_events_pd):
    """The core validation: Spark path (Silver -> toPandas -> reuse
    rfm.py) must produce identical features/labels to calling the pandas
    functions directly — this equivalence is what justifies not
    reimplementing RFM logic twice."""
    sample = raw_events_pd.head(300).copy()
    records = json.loads(sample.to_json(orient="records"))
    bronze = spark.read.json(spark.sparkContext.parallelize([json.dumps(r) for r in records]))
    silver = run_silver_transform(bronze)

    baseline_events = sample.copy()
    baseline_events["timestamp"] = pd.to_datetime(baseline_events["timestamp"], utc=True)
    expected_features = compute_rfm_features(baseline_events, as_of=AS_OF, feature_window_days=60)
    expected_labels = compute_label(baseline_events, as_of=AS_OF, feature_window_days=60)

    baseline_model = fit_rfm_quintile_baseline(expected_features)

    result = run_gold_transform(
        spark, silver, as_of=AS_OF, model_path=MODELS_DIR / "xgboost_model.json",
        baseline=baseline_model, feature_window_days=60,
    )

    gold_features = result["rfm_features"].toPandas().sort_values("customer_id").reset_index(drop=True)
    expected_sorted = expected_features.sort_values("customer_id").reset_index(drop=True)

    pd.testing.assert_frame_equal(
        gold_features[expected_sorted.columns], expected_sorted, check_dtype=False, atol=1e-6,
    )

    gold_scores = result["churn_scores"].toPandas().sort_values("customer_id").reset_index(drop=True)
    expected_labels_sorted = expected_labels.sort_values("customer_id").reset_index(drop=True)
    pd.testing.assert_series_equal(
        gold_scores["churn"].reset_index(drop=True), expected_labels_sorted["churn"].reset_index(drop=True),
        check_dtype=False,
    )


def test_kpi_daily_matches_manual_pandas_aggregation(bronze_df):
    silver = run_silver_transform(bronze_df)
    kpi = run_kpi_daily_transform(silver).toPandas()

    silver_pd = silver.toPandas()
    silver_pd["event_date"] = pd.to_datetime(silver_pd["timestamp"]).dt.date

    # Hand-verify one concrete day rather than trust the job's own logic:
    # DAU and purchase revenue for the first day with any purchase event.
    purchase_days = silver_pd[silver_pd["event_type"] == "purchase"]["event_date"]
    assert len(purchase_days) > 0, "fixture must contain at least one purchase to test revenue aggregation"
    sample_day = sorted(purchase_days.unique())[0]

    expected_dau = silver_pd[silver_pd["event_date"] == sample_day]["customer_id"].nunique()
    expected_revenue = silver_pd[
        (silver_pd["event_date"] == sample_day) & (silver_pd["event_type"] == "purchase")
    ]["amount_usd"].sum()

    row = kpi[kpi["event_date"] == sample_day].iloc[0]
    assert row["dau"] == expected_dau
    assert row["total_revenue"] == pytest.approx(expected_revenue)

    # push_open_rate/campaign_click_rate are cumulative (running-total)
    # ratios, not a same-day ratio — a real bug found live: a push sent
    # late one day is routinely opened the next, so a strict same-day
    # ratio spikes past 100% on low-volume days even though sends exceed
    # opens in aggregate. Hand-verify the cumulative definition directly
    # for the sample day, and confirm every day stays in [0,1] (only
    # guaranteed under the cumulative definition, not the same-day one).
    up_to_sample_day = silver_pd[silver_pd["event_date"] <= sample_day]
    expected_cum_sent = (up_to_sample_day["event_type"] == "push_sent").sum()
    expected_cum_open = (up_to_sample_day["event_type"] == "push_open").sum()
    expected_rate = expected_cum_open / expected_cum_sent if expected_cum_sent > 0 else 0.0
    assert row["push_open_rate"] == pytest.approx(expected_rate)
    assert kpi["push_open_rate"].between(0, 1).all()
    assert kpi["campaign_click_rate"].between(0, 1).all()


def test_compaction_reduces_partition_count(spark, raw_events_pd):
    sample = raw_events_pd.head(200)
    df = spark.createDataFrame(sample.astype(str)).repartition(20)
    assert count_partitions(df) == 20

    compacted = compact_small_files(df, target_partitions=4)
    assert count_partitions(compacted) == 4
    assert compacted.count() == df.count()  # no data lost
