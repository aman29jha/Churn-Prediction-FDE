"""
Raw events -> RFM feature table.

Leakage guard: `compute_rfm_features` only ever reads events with
timestamp <= T (the feature cutoff). `compute_label` only ever reads
events with T < timestamp <= as_of. The two never share data — see
docs/modeling.md for the full feature definitions and rationale.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

FEATURE_WINDOW_DAYS_DEFAULT = 60
SHORT_LOOKBACK_DAYS = 30
LONG_LOOKBACK_DAYS = 90


def load_events(path: str | Path) -> pd.DataFrame:
    with open(path) as f:
        raw = json.load(f)
    df = pd.DataFrame(raw)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    return _ensure_flat(df)


def _ensure_flat(events: pd.DataFrame) -> pd.DataFrame:
    """Idempotent: flattens a `properties` dict column into top-level
    columns (amount_usd, duration_sec, event_name, campaign_id, category)
    if present, otherwise assumes the caller already flattened it. Both
    load_events (real data) and the synthetic generator (src/data_gen)
    produce the same nested schema, so this lets compute_rfm_features /
    compute_label accept either without callers needing to remember to
    flatten first."""
    if "properties" not in events.columns:
        return events
    props = pd.json_normalize(events["properties"])
    return pd.concat([events.drop(columns=["properties"]).reset_index(drop=True), props.reset_index(drop=True)], axis=1)


def _cutoff(as_of: pd.Timestamp, feature_window_days: int) -> pd.Timestamp:
    return as_of - pd.Timedelta(days=feature_window_days)


def _count_in_window(df: pd.DataFrame, since: pd.Timestamp, index: pd.Index) -> pd.Series:
    windowed = df[df["timestamp"] > since]
    counts = windowed.groupby("customer_id").size()
    return counts.reindex(index, fill_value=0).astype(int)


def compute_rfm_features(
    events: pd.DataFrame,
    as_of: pd.Timestamp,
    feature_window_days: int = FEATURE_WINDOW_DAYS_DEFAULT,
    exclude_recent_days: int | None = None,
) -> pd.DataFrame:
    """exclude_recent_days: how many days immediately before `as_of` to
    hold out of feature computation. Defaults to `feature_window_days`,
    reserving a gap exactly as wide as compute_label's label window so
    training/eval features never overlap the label period they're
    predicting (offline use — see docs/modeling.md). Live scoring
    (gold_transform's --live-scoring path) passes 0: there's no label to
    protect against leakage from at serving time, so features should use
    every event known up to `as_of` itself, not stop 60 days short of it.
    """
    events = _ensure_flat(events)
    gap_days = feature_window_days if exclude_recent_days is None else exclude_recent_days
    T = _cutoff(as_of, gap_days)
    T30 = T - pd.Timedelta(days=SHORT_LOOKBACK_DAYS)
    T90 = T - pd.Timedelta(days=LONG_LOOKBACK_DAYS)

    customer_ids = events["customer_id"].unique()
    pre_T = events[events["timestamp"] <= T]

    out = pd.DataFrame(index=pd.Index(customer_ids, name="customer_id"))

    sessions = pre_T[pre_T["event_type"] == "session"]
    last_session = sessions.groupby("customer_id")["timestamp"].max()
    out["recency_days"] = ((T - last_session).dt.total_seconds() / 86400).reindex(out.index)

    out["frequency_30d"] = _count_in_window(sessions, T30, out.index)
    out["frequency_90d"] = _count_in_window(sessions, T90, out.index)

    purchases = pre_T[pre_T["event_type"] == "purchase"]
    purchases_90d = purchases[purchases["timestamp"] > T90]
    out["purchase_count_90d"] = _count_in_window(purchases, T90, out.index)
    out["purchase_revenue_90d"] = (
        purchases_90d.groupby("customer_id")["amount_usd"].sum().reindex(out.index, fill_value=0.0)
    )
    out["lifetime_revenue"] = (
        purchases.groupby("customer_id")["amount_usd"].sum().reindex(out.index, fill_value=0.0)
    )
    out["has_ever_purchased"] = out.index.isin(purchases["customer_id"].unique())

    push_sent = pre_T[pre_T["event_type"] == "push_sent"].groupby("customer_id").size().reindex(out.index, fill_value=0)
    push_open = pre_T[pre_T["event_type"] == "push_open"].groupby("customer_id").size().reindex(out.index, fill_value=0)
    out["push_open_rate"] = (push_open / push_sent.where(push_sent > 0)).fillna(0.0)

    out["campaign_click_count_90d"] = _count_in_window(pre_T[pre_T["event_type"] == "campaign_click"], T90, out.index)
    out["support_ticket_count_90d"] = _count_in_window(pre_T[pre_T["event_type"] == "support_ticket"], T90, out.index)

    sessions_90d = sessions[sessions["timestamp"] > T90]
    out["avg_session_duration_90d"] = (
        sessions_90d.groupby("customer_id")["duration_sec"].mean().reindex(out.index, fill_value=0.0)
    )

    in_app = pre_T[pre_T["event_type"] == "in_app_event"]
    has_event_name = "event_name" in in_app.columns
    add_to_cart = in_app[in_app["event_name"] == "add_to_cart"] if has_event_name else in_app.iloc[0:0]
    feature_use = in_app[in_app["event_name"] == "feature_use"] if has_event_name else in_app.iloc[0:0]
    out["add_to_cart_count_90d"] = _count_in_window(add_to_cart, T90, out.index)
    out["feature_use_count_90d"] = _count_in_window(feature_use, T90, out.index)

    return out.reset_index()


def compute_label(
    events: pd.DataFrame,
    as_of: pd.Timestamp,
    feature_window_days: int = FEATURE_WINDOW_DAYS_DEFAULT,
) -> pd.DataFrame:
    T = _cutoff(as_of, feature_window_days)
    customer_ids = events["customer_id"].unique()

    label_window = events[(events["timestamp"] > T) & (events["timestamp"] <= as_of)]
    active_customers = set(label_window[label_window["event_type"] == "session"]["customer_id"].unique())

    churn = pd.Series(
        [0 if c in active_customers else 1 for c in customer_ids],
        index=pd.Index(customer_ids, name="customer_id"),
        name="churn",
    )
    return churn.reset_index()
