# Modeling — Features, Label, Baseline, Synthetic Data, Model

## Data understanding that shaped every decision below

The raw sample (`data/events.json`, 800 events / 80 customers) has 7 event types, and every customer has at least one `session`. Two findings mattered most:

1. **No real behavioral funnel** — `push_sent -> push_open -> campaign_click -> purchase` do not chain together for the same customer/campaign in this sample (0.7%, 1.6%, ~0% linkage respectively). Event types were generated independently per customer, not as a causal sequence.
2. **The raw sample has ~no learnable churn signal on its own** — a logistic regression on core RFM features against our own leakage-safe label gets AUC 0.560 +/- 0.093 across 20 resamples; adding extra features barely moves it to 0.570 +/- 0.082. This matches the assignment README's own warning that the sample is "too small to train or evaluate on directly" — it's for event-shape reference only.

## Label

Cutoff **T = as_of - 60 days** (as_of = 2024-06-01T12:00:00Z, the given observation timestamp). `churn = 1` if the customer has **no `session` event in (T, as_of]** — the 60 days after the cutoff. Feature window (<=T) and label window (T, as_of] never overlap, so there is no leakage between what the model sees and what it's predicting.

The 60-day threshold was chosen on business reasoning ("gone quiet for two months"), deliberately **not** fit to what balance the raw 80-row sample happens to show — a 30-day window, for instance, would make churners the *majority* class in that tiny sample, which is small-sample noise, not a real signal to design around.

## Features (13, all computed on events with timestamp <= T)

**Core RFM:**
| Feature | Definition |
|---|---|
| `recency_days` | Days between T and the customer's last `session` before T |
| `frequency_30d` | Count of `session` events in (T-30d, T] |
| `frequency_90d` | Count of `session` events in (T-90d, T] |
| `purchase_count_90d` | Count of `purchase` events in (T-90d, T] |
| `purchase_revenue_90d` | Sum of `amount_usd` in (T-90d, T] |

**Monetary sparsity backups** (only 41/80 real customers ever purchase, ~1.3 purchases each — a 90-day window is zero for most customers even among buyers):
| Feature | Definition |
|---|---|
| `lifetime_revenue` | Sum of all `purchase` amounts before T |
| `has_ever_purchased` | Boolean, before T |

**Extras** (each independently justified, not just correlated):
| Feature | Definition | Why |
|---|---|---|
| `push_open_rate` | Lifetime `push_open` / `push_sent` before T (0 if never sent) | Tells us if marketing pushes even reach this customer — central to the campaign-targeting use case |
| `campaign_click_count_90d` | Count of `campaign_click` in (T-90d, T] | Active campaign engagement, not just passive app use |
| `support_ticket_count_90d` | Count of `support_ticket` in (T-90d, T] | Friction/dissatisfaction proxy |
| `avg_session_duration_90d` | Mean `duration_sec` of sessions in (T-90d, T] | Depth, not just frequency, of engagement |
| `add_to_cart_count_90d` * | Count of `in_app_event` where `event_name=add_to_cart` | "Considered a purchase, didn't complete" — provisional, see below |
| `feature_use_count_90d` * | Count of `in_app_event` where `event_name=feature_use` | The one in_app_event type with any measurable (if weak) correlation in the real sample — provisional |

\* **Provisional.** On the raw 80-row sample, correlation checks for these were noisy and inconclusive (p-values consistent with the multiple-testing problem — testing 10 features at alpha=0.05, 2-3 "significant" hits is what chance alone predicts). We include them because they're cheap and plausible, but the real decision to keep or drop them happens via **SHAP/ablation on the trained model against the ~1,200-customer synthetic dataset**, where the answer is actually trustworthy. `search`, `screen_view`, `share` in_app_event types were excluded outright — no signal and no clear business rationale distinct from what's already captured.

## Baseline: classic RFM quintile scoring

R, F, M each scored 1-5 by quintile (boundaries frozen from the training population, reused at serving time so a customer's segment doesn't drift just because the population changed) — the same style of scoring used in real marketing tools (e.g. the classic Champions/Loyal/At Risk/Hibernating/Lost segmentation). Baseline rule: **"bottom 2 segments = predicted churn."** This is the industry-standard heuristic the assignment asks us to beat, and it doubles as a stakeholder-facing dashboard artifact (queryable via Athena) — but it is **not** where the label comes from (that would be circular: using RFM buckets to define the label the model is trying to predict from RFM-derived features would just be re-labeling the past as itself).

## Model: XGBoost

Trained on the continuous features above (not the quintile scores — bucketing would throw away information a tree model can use natively). Chosen because:
- Handles nonlinear interactions between features automatically (e.g. "low recency AND low frequency is much worse than either alone").
- Native imbalance handling via `scale_pos_weight`.
- Fast/cheap to train and serve — no GPU, trivial inference cost, matters for the AWS budget.
- Pairs cleanly with **SHAP** for explainability: per-customer, per-feature attribution of exactly how much each signal pushed the score up or down — the basis for the console's "why this score" output and the plain-language stakeholder explanation deliverable.

Training itself is **plain Python, not Spark** — the training set is ~1,200 rows; distributing that over Spark would be cargo-culting, not judgment. It reads Gold Iceberg tables via a lightweight reader (pandas/pyiceberg), trains, computes SHAP values, and writes the model + explainer artifacts to the S3 model registry.

## Synthetic data generation

Per the assignment's explicit requirement: "too small to train on or evaluate directly... write a reproducible script that generates additional synthetic customers/events preserving the real sample's statistical structure."

- **Size**: ~1,200 synthetic customers — large enough for a real train/val/test split and for each fairness subgroup to have a few hundred customers (reliable subgroup metrics), small enough to stay fast and easy to reason about. Fixed random seed, fully reproducible.
- **Event-level statistics fit from the real 80 customers**: event-type mix, session inter-arrival gaps (gamma/mixture — captures the dormancy tail we observed, up to 300+ day gaps), session duration (~N(183s, 57s)), purchase amount (a **two-component mixture** — small ~$1-3 vs. larger ~$15-60 purchases; the real data is genuinely bimodal, a single lognormal would smear that away), push-open rate (~70% of sends).
- **Deliberately injected engagement archetypes** (high/medium/low), driving BOTH pre-cutoff behavior and post-cutoff outcome. This is an intentional, documented deviation from "faithfully replicate the raw sample's temporal structure": the raw sample doesn't clearly exhibit real recency-predicts-future-churn persistence (that's exactly what the AUC-0.56 finding above shows), and without injecting it, the synthetic dataset would inherit the same unlearnable-label problem. Real customer populations have heterogeneous, persistent engagement levels; the archetype mechanism is how we generate a realistic, *learnable* population from a shape fit to 80 customers, rather than 1,200 statistically-identical customers sampled independently.
- **Target ~25% churn base rate** — an explicit, documented generation parameter, not an emergent accident of the event-level distributions.
- **Synthetic segment fields** (`plan_tier`, `acquisition_channel`, `region`) for the fairness check — randomly assigned but with a plausible, modest, documented behavioral correlation (e.g. paid-acquisition customers trending toward lower engagement, a commonly observed real-world pattern), rather than pure noise. Purely random segments would likely show zero fairness gap, making that deliverable a non-finding. Explicitly documented in the fairness writeup as an invented assumption, not observed data.
