"""
Three-layer explainability per docs/explainability.md: global SHAP summary,
per-customer SHAP waterfall (top features), and an auto-generated
plain-language interpretation built from the same SHAP values.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import shap
import xgboost as xgb


def build_explainer(model: xgb.XGBClassifier) -> shap.TreeExplainer:
    return shap.TreeExplainer(model)


def global_feature_importance(explainer: shap.TreeExplainer, X: pd.DataFrame) -> pd.Series:
    shap_values = explainer.shap_values(X)
    mean_abs = np.abs(shap_values).mean(axis=0)
    return pd.Series(mean_abs, index=X.columns).sort_values(ascending=False)


def explain_customer(
    explainer: shap.TreeExplainer,
    X_row: pd.DataFrame,
    top_k: int = 5,
    churn_probability: float | None = None,
) -> dict:
    shap_values = explainer.shap_values(X_row)[0]
    base_value = explainer.expected_value
    contributions = pd.Series(shap_values, index=X_row.columns).sort_values(key=np.abs, ascending=False)

    top = contributions.head(top_k)
    explanation = [
        {
            "feature": feat,
            "value": float(X_row.iloc[0][feat]) if pd.notna(X_row.iloc[0][feat]) else None,
            "shap_value": float(val),
            "direction": "increases_risk" if val > 0 else "decreases_risk",
        }
        for feat, val in top.items()
    ]
    plain_language = _plain_language(explanation, churn_probability)
    return {"base_value": float(base_value), "explanation": explanation, "plain_language": plain_language}


_FEATURE_PHRASES = {
    "recency_days": "how long it's been since their last app session",
    "frequency_30d": "how often they've used the app in the last month",
    "frequency_90d": "how often they've used the app in the last quarter",
    "purchase_count_90d": "how many purchases they've made recently",
    "purchase_revenue_90d": "how much they've spent recently",
    "lifetime_revenue": "their total spend history",
    "has_ever_purchased": "whether they've ever made a purchase",
    "push_open_rate": "how often they open push notifications",
    "campaign_click_count_90d": "how often they click on campaigns",
    "support_ticket_count_90d": "how many support tickets they've filed",
    "avg_session_duration_90d": "how long their sessions tend to last",
    "add_to_cart_count_90d": "how often they add items to cart",
    "feature_use_count_90d": "how much they use in-app features",
}


def _plain_language(explanation: list[dict], churn_probability: float | None = None) -> str:
    increasing = [e for e in explanation if e["direction"] == "increases_risk"]
    decreasing = [e for e in explanation if e["direction"] == "decreases_risk"]

    # Real bug found via the live console's Live Lookup tab: the old version
    # of this function called risk "elevated" any time the single largest-
    # magnitude SHAP feature among the top-k happened to point up, with no
    # regard for the actual predicted probability. That produced sentences
    # like "churn risk is elevated" for a customer whose churn_probability
    # was 2% (segment "Loyal") purely because push_open_rate outranked two
    # larger negative (risk-reducing) features in the top-5 list. Overall
    # framing must be driven by the actual predicted probability; the SHAP
    # directions only explain *why*, not *whether*, risk is elevated.
    if churn_probability is None:
        level = "elevated" if increasing else None
    elif churn_probability >= 0.5:
        level = "elevated"
    elif churn_probability >= 0.15:
        level = "moderate"
    else:
        level = "low"

    parts = []
    if level == "elevated" and increasing:
        drivers = " and ".join(_FEATURE_PHRASES.get(e["feature"], e["feature"]) for e in increasing[:2])
        parts.append(f"This customer's churn risk is elevated mainly because of {drivers}.")
    elif level == "moderate" and increasing:
        drivers = " and ".join(_FEATURE_PHRASES.get(e["feature"], e["feature"]) for e in increasing[:2])
        parts.append(f"This customer's churn risk is moderate, pushed up somewhat by {drivers}.")
    elif level == "low" and increasing:
        drivers = " and ".join(_FEATURE_PHRASES.get(e["feature"], e["feature"]) for e in increasing[:2])
        parts.append(
            f"This customer's overall churn risk is low, even though {drivers} nudge it up on their own."
        )

    if decreasing:
        offset = _FEATURE_PHRASES.get(decreasing[0]["feature"], decreasing[0]["feature"])
        if level == "low":
            parts.append(f"That's mainly held down by {offset}.")
        else:
            parts.append(f"On the positive side, {offset} is a mitigating signal.")
    if not parts:
        parts.append("This customer's churn risk is close to the population average, with no single dominant driver.")
    return " ".join(parts)
