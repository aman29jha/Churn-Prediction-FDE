# Fairness / Bias Check

*This document describes the methodology. Actual findings (which subgroups, what gaps, if any) get filled in once the model is trained and evaluated against the synthetic dataset.*

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
