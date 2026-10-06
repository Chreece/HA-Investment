# Indication safety gates — first improvement batch

Base: `indications` at `5d0a72c65a5d122ac9e090a5f9bdd615ff157c49`.
Safety contract marker: `approved-candidates-aligned-history-v1`.

## Changes

Whole-lot redistribution may only select identities with positive approved
post-constraint weights. A cent-rounded zero is not confused with a rejected
candidate. Final production output independently asserts that no rejected
identity received money. AI cannot revive an explicitly ineligible candidate,
even if it carries a stale positive suggested amount.

Every positive position must have a complete, finite return map with at least
52 observations, the existing minimum. Invalid entries are not silently dropped
or changed to zero. Zero-weight rows may lack history; positive rows may not,
including tiny weights. A loss below -100%, invalid keys or boolean observations
are rejected. The aggregate uses common observations, not a union with zero
imputation. At least 52 aligned observations are needed for a usable signature.
Insufficient aligned history blocks risk scaling instead of letting exposure
shrink below an epsilon until the missing history disappears.

The weekly converter skips returns across missing ISO weeks. Adjacent weeks
across ISO year boundaries remain valid. A price change spanning three weeks
cannot count as a one-week return. Nonfinite, negative or boolean risk metrics,
or insufficient observation counts, cannot pass the final risk check.

## Validation performed before publication

Existing tests: **190 passed**, without modifying any existing test.
New behavioral tests: **615 passed**; **143 failed / 472 passed on the original
source**. Failed test instances are repeated checks of a few defects, not 143
distinct bugs.
Full patched suite: **805 passed**.

The generated section covers 500 deterministic markets and all three execution
modes: **1,500 allocation scenarios**, with repeated-call determinism and final
AI allocation-ceiling checks. Tests cover all five risk profiles, varied budgets,
prices, confidence/context, cash reserves, explicit caps and missing history.
No test requires arbitrary input to produce a purchase. Feasible and infeasible
controls are both retained.

Replaying the unmodified original audit's generated inputs: eligibility
reintroductions **6 -> 0** across 300 cases; no observed budget, sleeve, manual
cap, native historical-risk, quantity/price, or finite-output regressions.
The audit's four eligibility probes, disjoint-history probe and irregular-week
probe are corrected. Three other targeted probes remain outside this batch.

Independent comparison against the original model: **400 well-formed, aligned,
approved-set cases** retained identical amounts/units and risk calculations.
Python compilation and shipped JavaScript syntax checks passed locally.
CI must also complete before this change is considered ready to merge.

To repeat the regression test on an original source snapshot:

```sh
INVESTMENT_SAFETY_MODEL=/path/to/original/validated_model.py \
  python -m pytest -q tests/test_indication_safety_gates.py
python -m pytest -q tests
```

## Scope and remaining work

This patch does not tune market scoring, risk/sleeve targets or confidence
thresholds. It adds no forecast and makes no investment-performance claim.
Unknown risk causes abstention, not proof that no investable combination exists.

Still pending: as-of/freshness checks and provenance; authoritative ISIN/share
class/structure classification; fees-inclusive affordability; complete post-trade
portfolio analysis; horizon/drawdown/stress uncertainty; solver completeness;
and independently validated probabilistic forecasts. Common-history validation
alone does not prove the sample is representative of future risk. The original
audit's stale-history, structure-classification and drawdown-safety probes are
not fixed here.

No changes are needed to frontend assets in this batch. The runtime marker is
returned in `allocation.projection.safety_contract`. Installing new backend code
requires restarting Home Assistant; opening a PR does not update a running HA.
