# Explainability

Methodology below; real findings from `scripts/run_training_pipeline.py` against the 1,200-customer synthetic dataset follow at the end (raw output in `reports/global_shap_importance.png` and `reports/example_explanation.json`).

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

## Real findings (1,200-customer synthetic dataset)

**Global importance** (mean |SHAP value| across the test set):

| Feature | Mean \|SHAP\| |
|---|---|
| `recency_days` | 3.628 — dominant, by nearly an order of magnitude |
| `frequency_30d` | 0.456 |
| `avg_session_duration_90d` | 0.349 |
| `push_open_rate` | 0.277 |
| `frequency_90d` | 0.247 |
| `lifetime_revenue` | 0.127 |
| `purchase_revenue_90d` | 0.101 |
| `add_to_cart_count_90d` | 0.101 |
| `feature_use_count_90d` | 0.099 |
| `campaign_click_count_90d` | 0.055 |
| `support_ticket_count_90d` | 0.027 |
| `purchase_count_90d` | 0.013 |
| `has_ever_purchased` | 0.000 — completely unused |

**Resolving the two provisional features** (per [modeling.md](modeling.md), these were included on plausibility but their real value was undetermined from the noisy 80-row sample): both `add_to_cart_count_90d` and `feature_use_count_90d` land in the same importance tier as `lifetime_revenue`/`purchase_revenue_90d` — modest but real, not the least important features in the model. **Keep both.**

**A feature to actually drop**: `has_ever_purchased` has exactly zero SHAP importance — the model found it fully redundant with `purchase_count_90d`/`lifetime_revenue`, which already encode the same information more precisely. This is exactly the kind of evidence-based cleanup the design called for (see modeling.md) rather than guessing from a tiny sample.

**Example explanation** (highest-risk customer in the test set, `churn_probability = 0.999`):
> "This customer's churn risk is elevated mainly because of how long it's been since their last app session and their total spend history."

This is the literal auto-generated plain-language output — recency and lifetime revenue were the top two SHAP-ranked features for this customer, matching the global importance ranking.
