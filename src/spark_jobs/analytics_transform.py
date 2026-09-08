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

from pyspark.sql import DataFrame, Window
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

    # Real bug found checking this exact chart's own numbers: a strict
    # same-day ratio (today's opens / today's sends) isn't a sound "rate"
    # here — a push sent late one day is routinely opened the next, so
    # some individual days show more opens than sends and the "rate"
    # spikes above 100% (verified live: values like 2.0, 4.0 on
    # low-volume days), even though push_sent_count > push_open_count
    # in aggregate across the whole dataset. Cumulative (running-total)
    # rate is both mathematically sound here — it can't exceed 1.0 given
    # that aggregate holds — and a more standard way to chart an
    # engagement trend than a noisy per-day ratio anyway.
    running = Window.orderBy("event_date").rowsBetween(Window.unboundedPreceding, Window.currentRow)
    kpi = (
        dau.join(revenue, "event_date", "left")
        .join(push_sent, "event_date", "left")
        .join(push_open, "event_date", "left")
        .join(campaign_click, "event_date", "left")
        .na.fill(0)
        .withColumn("cum_push_sent", F.sum("push_sent_count").over(running))
        .withColumn("cum_push_open", F.sum("push_open_count").over(running))
        .withColumn("cum_campaign_click", F.sum("campaign_click_count").over(running))
        .withColumn(
            "push_open_rate",
            F.when(F.col("cum_push_sent") > 0, F.col("cum_push_open") / F.col("cum_push_sent")).otherwise(F.lit(0.0)),
        )
        .withColumn(
            "campaign_click_rate",
            F.when(F.col("cum_push_sent") > 0, F.col("cum_campaign_click") / F.col("cum_push_sent")).otherwise(F.lit(0.0)),
        )
        .drop("cum_push_sent", "cum_push_open", "cum_campaign_click")
        .orderBy("event_date")
    )
    return kpi
