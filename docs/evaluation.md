# Evaluation Metrics

## Why not accuracy

With a ~25% churn base rate (our synthetic dataset's target — see [modeling.md](modeling.md)), a model that predicts "nobody churns" scores 75% accuracy while being completely useless. Every metric below is chosen for imbalanced classification, not generic accuracy.

## The cost asymmetry driving the design

Missing a real churner (false negative) loses that customer with no chance to intervene — genuinely costly. Flagging a non-churner (false positive) costs one wasted campaign send/discount — annoying and wasteful, but far cheaper than losing the customer. **FN >> FP in cost**, so metrics lean toward recall, not precision/accuracy — but not without a floor, or marketing ends up contacting the entire customer base.

## Metrics reported

| Metric | Purpose |
|---|---|
| **PR-AUC** (average precision) | Headline ranking-quality metric; robust under imbalance, unlike ROC-AUC which gets optimistic/misleading when the negative class dominates |
| **Recall @ fixed precision** | "At an acceptable waste rate, how many real churners do we still catch?" — the question marketing would actually ask |
| **Top-decile capture / lift** | Of the riskiest 10% targeted, what fraction of actual churners is caught vs. random targeting — matches the real usage pattern: audience selection is a ranked list, not a uniform yes/no classifier |
| **F2 score** | Recall weighted 2x precision, reflecting the FN-costlier-than-FP asymmetry; used to pick the operating threshold |
| **Calibration (Brier score)** | Secondary — checks that a "73% risk" score is trustworthy, not just a good rank |

## Threshold selection

**Capacity-based**, not an arbitrary 0.5 cutoff: the threshold is set to target the **top ~15%** of customers, matching a realistic campaign budget — a concrete "who gets contacted" answer for marketing, not just a statistically-optimal-but-abstract number. F2/recall/precision are reported at that operating point.

## Protocol

Stratified **70/15/15 train/val/test** split on the label (churn rate preserved in each split). Validation set tunes the threshold; test set reports final numbers and is reused (same set, sliced by the synthetic segment fields) for the fairness check — no separate sampling for that.

## Reporting

Baseline (RFM quintile rule, see [modeling.md](modeling.md)) vs. XGBoost model, side-by-side on every metric above, plus a cumulative gains/lift chart — visually answers "random targeting vs. our model" for a non-technical audience, surfaced in the [reviewer console](architecture/06-reviewer-console.md).
