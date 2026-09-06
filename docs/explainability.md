# Explainability

*This document describes the methodology and output format. Actual findings (which features dominate, example customer explanations) get filled in once the model is trained against the synthetic dataset.*

## Why three layers, not just a SHAP plot

The assignment asks for "which signals drive predictions... and a concise interpretation for a non-technical stakeholder." A SHAP beeswarm plot alone satisfies the technical half but not the stakeholder half — so we produce three distinct outputs from the same underlying SHAP values.

## 1. Global explainability

A SHAP summary (beeswarm) plot computed once per training run across the full evaluation set — shows which of the 13 features (see [modeling.md](modeling.md)) matter most overall, and in which direction (e.g. does high `recency_days` push risk up, as expected). Lives in the reviewer console's model dashboard, alongside a simpler mean-|SHAP-value| bar chart for readers who find the beeswarm too dense.

## 2. Local (per-customer) explainability

A SHAP waterfall showing the top 5-6 features that pushed one specific customer's score up or down from the population baseline. Computed **on-demand**, not precomputed for every customer — XGBoost's `TreeExplainer` is fast enough per single instance (milliseconds) that there's no need to store an explanation for all ~1,200 customers ahead of time. Triggered by the reviewer console's live lookup box, and available as part of the `/score/{customer_id}` API response.

## 3. Plain-language interpretation

An auto-generated sentence built from the top 2-3 SHAP-ranked features for that customer, e.g.:

> "This customer's churn risk is elevated mainly because they haven't opened the app in 45 days, and their session frequency has dropped well below their usual pattern. Their purchase history is normal, which is a mildly positive signal."

Template logic: rank features by `|shap_value|`, phrase the top 1-2 as primary drivers with their direction, phrase the next 1 as a secondary or offsetting signal if its direction opposes the top driver. This is what a marketer reads — not a plot.

## Structured output, not just a plot

The `/score/{customer_id}` API response includes an `explanation` field:
```json
{
  "customer_id": "cust_00047",
  "churn_probability": 0.73,
  "rfm_segment": "At Risk",
  "explanation": [
    {"feature": "recency_days", "value": 45, "shap_value": 0.21, "direction": "increases_risk"},
    {"feature": "frequency_90d", "value": 2, "shap_value": 0.14, "direction": "increases_risk"},
    {"feature": "purchase_revenue_90d", "value": 12.50, "shap_value": -0.03, "direction": "decreases_risk"}
  ],
  "plain_language": "This customer's churn risk is elevated mainly because..."
}
```
This matters because a real campaign tool needs to consume "why" programmatically (e.g. to decide *which* win-back offer to send — a recency-driven risk might get a re-engagement push, a monetary-driven risk might get a discount) — not just render a picture for a human.
