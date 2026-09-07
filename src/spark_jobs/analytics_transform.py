"""
Silver -> Analytics (kpi_daily) Spark job. See docs/architecture/07-analytics.md.

Scope note, stated honestly rather than silently narrowed: of the three
tables documented there (kpi_daily, segment_migration, cohort_retention),
only kpi_daily is implemented here. The other two both need Gold's tables
to actually be append-only / partitioned by run_date first (a prerequisite
the doc calls out explicitly) so there is real history to diff a
transition matrix against or compare a cohort across — that's a separate,
larger change to gold_transform.py's write semantics, not done in this
pass. kpi_daily needs no such history: it's a same-run aggregation over
Silver events, grouped by day.
"""
from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F


def run_kpi_daily_transform(silver_df: DataFrame) -> DataFrame:
    daily = silver_df.withColumn("event_date", F.to_date("timestamp"))

    dau = daily.groupBy("event_date").agg(F.countDistinct("customer_id").alias("dau"))

    revenue = (
        daily.filter(F.col("event_type") == "purchase")
        .groupBy("event_date")
        .agg(
            F.sum("amount_usd").alias("total_revenue"),
            F.avg("amount_usd").alias("avg_revenue"),
            F.count("*").alias("purchase_count"),
        )
    )

    def _count_by_type(event_type: str, col_name: str) -> DataFrame:
        return (
            daily.filter(F.col("event_type") == event_type)
            .groupBy("event_date")
            .agg(F.count("*").alias(col_name))
        )

    push_sent = _count_by_type("push_sent", "push_sent_count")
    push_open = _count_by_type("push_open", "push_open_count")
    campaign_click = _count_by_type("campaign_click", "campaign_click_count")

    kpi = (
        dau.join(revenue, "event_date", "left")
        .join(push_sent, "event_date", "left")
        .join(push_open, "event_date", "left")
        .join(campaign_click, "event_date", "left")
        .na.fill(0)
        .withColumn(
            "push_open_rate",
            F.when(F.col("push_sent_count") > 0, F.col("push_open_count") / F.col("push_sent_count")).otherwise(F.lit(0.0)),
        )
        .withColumn(
            "campaign_click_rate",
            F.when(F.col("push_sent_count") > 0, F.col("campaign_click_count") / F.col("push_sent_count")).otherwise(F.lit(0.0)),
        )
        .orderBy("event_date")
    )
    return kpi
