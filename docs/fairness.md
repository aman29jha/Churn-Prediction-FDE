# Fairness / Bias Check

Methodology below; real findings from `scripts/run_training_pipeline.py` against the 1,280-customer population (1,200-customer synthetic bootstrap + the 80-customer real sample) follow at the end (raw output in `reports/fairness.json`).

## What we slice on

The real raw sample has no profile/demographic fields — only event-level data. Per [modeling.md](modeling.md), the synthetic generator adds three segment fields purely for this check: `plan_tier`, `acquisition_channel`, `region`. These carry a plausible, modest, **documented** behavioral correlation with engagement (e.g. paid-acquisition customers trending toward lower engagement, a commonly observed real-world pattern) — not pure random noise, and not observed data. Any finding below rests on this invented assumption, and the write-up says so explicitly rather than presenting it as discovered fact about real Localytics customers.

## Why False Negative Rate parity, not accuracy parity

[evaluation.md](evaluation.md) already establishes that a false negative (missing a real churner) is the costlier error for this business — that customer leaves with no chance to intervene. So the fairness question that actually matters is: **are some subgroups' churners being systematically missed at a higher rate than others?** That's a concrete harm (less retention effort spent on that group), not an abstract statistical difference. Overall accuracy parity is a weaker, less business-relevant check here and is reported only as secondary context.

## Metrics per subgroup, same held-out test set

No separate sampling — the same stratified test set from [evaluation.md](evaluation.md), sliced by each segment field:
- **False Negative Rate (FNR)** — primary
- **Selection rate** — what fraction of each subgroup lands in the top-15% targeted group (catches under- or over-targeting of a subgroup, a demographic-parity-style secondary lens)
- PR-AUC and top-decile capture, for full context alongside the primary metrics

## What counts as a finding

Any subgroup with FNR **greater than 1.25x the overall FNR**, or an absolute gap of more than 10 percentage points (whichever is more informative given that subgroup's sample size — with ~1,200 customers split across 3-4 categories per field, some subgroups will have only a couple hundred customers, so a fixed ratio threshold alone can be noisy at the edges).

## Recommendations if a gap is found

Not "retrain a separate model per segment" — that adds real maintenance burden for a modest gain and doesn't address the underlying question of *why* the gap exists. Instead:
1. **Monitor it going forward** on the observability dashboard (see [architecture/05-observability.md](architecture/05-observability.md)) rather than treating this as a one-time check that's "done."
2. **Consider a segment-specific decision threshold** (a post-hoc fairness adjustment) to equalize catch rate across groups, rather than changing the model's training or features.
3. **Flag to product/data teams that this segment may need real data collection** — since the segment itself is synthetic, a real gap here is a hypothesis worth validating against actual Localytics customer data, not a conclusion to act on directly.

## What we're explicitly not claiming

This check validates our *methodology* for finding and reasoning about fairness gaps. It does not, and cannot, tell us anything about real disparities in Localytics' actual customer base, because the subgroup labels themselves are invented. The value of doing this well is demonstrating the check is built into the design from the start (per the assignment's own "not bolted on at the end" evaluation criterion), not that the specific numbers generalize.

## Real findings (1,280-customer population (1,200-customer synthetic bootstrap + the 80-customer real sample), test set n=192, overall FNR = 0.4375)

Sliced on `plan_tier` (3 values), `acquisition_channel` (4 values), `region` (4 values) — 11 subgroups checked in total.

**1 finding flagged**: `region = north_america` — FNR 0.545 vs. overall 0.4375 (absolute gap 10.8 points, just over the 10-point threshold; ratio 1.25x, right at the ratio threshold too). n=51 for this subgroup — on the smaller side, so this should be treated as a signal worth monitoring, not a confident conclusion; at this sample size a single-digit swing in false negatives would move the gap noticeably. `region = latam` shows the opposite pattern (FNR 0.308, better than overall) — consistent with the north_america gap being a real if modest effect rather than pure noise, but not strong enough evidence to be certain given the segment sizes involved.

**No findings** on `plan_tier` or `acquisition_channel` — all FNR ratios stayed within roughly 0.75x-1.15x of the overall rate, well inside the threshold. Notably, `acquisition_channel` was the field we deliberately gave a documented behavioral correlation with engagement archetype (see [modeling.md](modeling.md)) — the model doesn't discriminate against paid-acquisition customers on FNR specifically, even though they're less engaged on average, likely because the model conditions on the actual engagement features (recency, frequency) rather than the channel itself.

**Recommendation given this specific finding**: monitor the `region = north_america` FNR gap on the observability dashboard for a few more scoring cycles before acting — one measurement at n=51 isn't enough to justify a segment-specific threshold adjustment yet. If it persists or widens, the threshold-adjustment approach described above is the next step, not retraining.
