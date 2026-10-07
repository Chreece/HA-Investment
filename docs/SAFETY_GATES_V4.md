# Whole-share purchase feasibility and stable final checks

This batch continues PR #13 at
`c9ca23ffc4a435b4058406ee57183938c0bb9af9`. It improves execution of already
approved purchases and the evidence needed to approve a complete portfolio.
The underlying V13 signal, risk thresholds, concentration caps and strict
non-worsening rule are retained.

The final portfolio contract remains `complete-portfolio-confirmed-costs-v1`.
The additional search contract is `bounded-whole-unit-reductions-v1`, and the
panel asset revision is `0.4.0-r62`.

## 1. Recover feasible whole-share combinations after fees

The previous cost projection reduced all approved quantities by a common
factor. With one whole share per candidate, any factor below one removes
every purchase. That can leave all cash even when an affordable subset meets
every existing risk requirement.

A reproduced example has a €100 purchase budget and three approved purchases:

| Candidate | Economic sleeve | Original maximum | Marked principal |
| --- | --- | ---: | ---: |
| A | Cash-like | 1 whole share | €40 |
| B | Government bond | 1 whole share | €30 |
| C | Aggregate bond | 1 whole share | €30 |

With a confirmed €1 cost per positive purchase, all three need €103. The old
proportional reducer returned zero shares. The new finalizer independently
checks the permitted integer combinations and selects A plus B: **€70 marked
principal, €2 costs, €72 total debit and €28 cash remaining**. The 45%
per-instrument and medium-risk sleeve limits still apply to wealth after fees.
This is a synthetic engineering counterexample, not a proposed fund portfolio.

### Authority and objective

The search starts from the original inputs to the final portfolio gate. A
candidate must have positive approved amount and quantity ceilings and pass
the existing evidence rules. For each eligible whole-share row, its integer
quantity cannot exceed either original ceiling at the raw quoted price.
An original zero, excluded candidate, failed identity or failed source check
cannot be revived. AI amounts and units supply their own additional ceilings.

The existing proportional result is kept as a verified incumbent. Any
fractional purchases remain at that incumbent's literal amounts and quantities.
The search can change the whole-share combination, then reprices it with the
confirmed Decimal cost engine and rechecks **all** existing holdings, new
purchases and residual cash. Removing a hedge cannot bypass that check.

Within this fixed fractional scope, choices are ordered by:

1. Largest exact marked purchase principal.
2. Smallest estimated transaction cost when principal is equal.
3. Smallest sum of relative reductions from the permitted whole-share maxima.
4. Stable provider/instrument identity and integer quantities for remaining ties.

This extends the existing allocator's deployment objective under its approved
limits. Greater deployment is not evidence of higher future returns, a better
risk-adjusted portfolio, or lower total fees. A newly feasible purchase may
incur a fee that the previous zero-purchase result did not incur.

### Bounded work and honest completeness

Small integer domains are enumerated completely. Candidate states are sorted
by the exact objective; unaffordable states are discarded, and states unable
to improve the verified incumbent need no risk calculation. The first
higher-ranked feasible state is sufficient to establish the best objective
within a completely generated domain. An accepted plan already at every
original integer maximum establishes the upper bound without enumeration.

Large domains use a deterministic bounded search. At most 1,024 final states
are retained, and the larger-domain beam has width 128. Additional portfolio
risk evaluations are also limited by a one-million-return-point work budget,
scaled to the supplied position/history size. These are operational work
limits, not fitted investment parameters.

The result records domain scope, reported combination count, states generated,
cash and objective pruning, risk evaluations and rejections, representation
failures, work limits, and selected-plan provenance. Its status distinguishes:

| Status | Meaning |
| --- | --- |
| `upper_bound_verified` | The accepted incumbent reaches every original integer maximum. |
| `exhaustive` | The complete small integer domain was generated and the search completed. |
| `bounded` | A state, work or representation limit prevents a completeness claim. |
| `blocked` | Required portfolio evidence or confirmed costs prevent the search. |
| `not_applicable` | There is no eligible positive whole-share domain to search. |

`optimality_proven` applies only to this finite downward integer domain at
fixed fractional quantities. `global_optimality_claimed` is always false.
A bounded zero result leaves `no_positive_feasible` unknown. It does not prove
that all permitted purchases are impossible. Affordable combinations rejected
by portfolio risk have a different explanation from unaffordable purchases.

Representative local worst-shape probes, with bootstrap diagnostics disabled,
took approximately 0.5–1.4 seconds for 20–100 candidates with 156–1,040 weekly
observations each. Capped searches retained the verified incumbent and reported
incompleteness. These measurements describe this test environment, not a Home
Assistant latency guarantee.

## 2. Repricing is stable across repeated checks

Nearest-float serialization could turn an exact marked principal into a
slightly smaller numeric ceiling. For a raw price of `23.252591845784377`, a
€100 amount ceiling, four approved whole shares and confirmed zero costs,
successive checks returned **4, then 3, then 2 shares**. No economic input had
changed.

The cost engine now serializes principal to the least float whose decimal
representation covers the exact marked amount. Quantities are first bounded
by a representable ceiling that cannot exceed the original caller's amount.
Higher-precision Decimal-only input ceilings are narrowed when necessary.
The cash debit, commission, spread, FX charges and cent allowance continue to
use Decimal calculations. No numerical risk tolerance or incoming purchase
ceiling is enlarged.

Previously rounded-down external ceilings remain authoritative. Explicit
later reductions still reduce the plan, and invented metadata cannot authorize
extra principal. The engine does not reinterpret an upstream cent-rounded
approval as permission to spend more at the raw price.

## 3. Each held record supplies its own identity assertions

Sharing a provider ID previously allowed an existing holding to inherit a
candidate's complete identity verdict. A holding with conflicting stored
ISIN, symbol, exchange, native currency or share-class data could therefore
be treated as verified. Deduplicated requests for multiple held records had
the same problem.

Each positive held record is now resolved against the actual observed provider
metadata using that record's own original assertions. Validated market data
can still be reused without unnecessary provider requests, but one record's
identity approval cannot authorize another. Prior source failures remain
blocking. Compatible duplicates retain both quantities; verified zero-owner
holdings retain their existing exemption from positive-exposure history checks.

## 4. Preserve evidence through the live finalization sequence

The initial combination search runs through Home Assistant's executor. It
operates on the per-analysis pure inputs; state changes are applied after the
awaited result returns. The existing final ledger, currency and source
freshness checks still run after that work. The final verification executes
synchronously after the last storage/source read, preserving that snapshot
boundary. Tests mutate the ledger, currency and clock during executor work
and require the stale purchases to be removed.

Rechecking a selected plan necessarily sees narrower quantity ceilings. Its
newly empty domain cannot replace an earlier bounded search with a false
completeness claim. The manager preserves original search provenance and
abstention explanations only when the selected identities, amounts, quantities
and source evidence remain unchanged. It records the latest verification
separately. AI that retains exactly the deterministic selection can retain
the same evidence; changed quantities or invalidated evidence cannot.

The UI explains completed and limited whole-share searches in all 28 supported
languages. Mixed plans disclose their fixed fractional purchases. Unknown,
blocked or invalidated assessments do not display a prior completeness claim.

## Independent evidence and reproduction

The standard-library [audit](../research/audit_purchase_search.py) extracts the
actual prior implementation from commit `c9ca23f`. Its oracle independently
enumerates integer vectors and reconstructs exact costs, cash, position/sleeve
concentration, population volatility, empirical tail loss and compounded
drawdown. It does not call the production pricing or risk helpers.

The fixed plan includes 14 named engineering scenarios and 48 generated cases
with seed `20261007`. Generated domains have 2–4 candidates and 1–3 original
whole units per candidate; every draw is retained. This is an engineering
feasibility study, not another historical strategy-performance comparison.

| Final audit measure | Result |
| --- | ---: |
| Completed cases | 62 / 62 |
| Exhaustive oracle vectors | 1,465 |
| Current rule violations | 0 |
| Prior implementation rule violations | 0 |
| Missed optimum in audited small domains | 0 |
| Cases with greater permissible principal | 34 |
| Cases with reduced incumbent principal | 0 |

The [v2 summary](../research/results/purchase-search-20261007-v2/summary.json),
[complete case ledger](../research/results/purchase-search-20261007-v2/cases.jsonl)
and [comparison with v1](../research/results/purchase-search-20261007-v2/comparison-to-v1.json)
retain inputs, outcomes, source fingerprints and verification. V1 is retained
at its original source and superseded by the final explanation correction;
all 62 economic outcomes and the predeclared plan are identical. The earlier
84-trial historical study is unchanged and still describes its original commit.
It has not been represented as a performance test of this new implementation.

Run the focused tests and create a new write-once audit directory:

```bash
python -m pytest -q tests/test_purchase_search.py tests/test_purchase_search_oracle.py tests/test_execution_costs_idempotence.py tests/test_held_identity_reuse.py tests/test_purchase_search_integration.py
python research/audit_purchase_search.py --output-dir research/results/new-purchase-search-run
```

The existing chronological study now fingerprints both the live identity
module and the new search helper, so a future run cannot omit these participating
dependencies from its source identity.

Local validation on 2026-10-07 passed **2,070 tests**: the 1,880-test baseline
and 190 additional cases. The full run includes actual provider/parser/manager
integration, both shipped DOM layers, all 28 language dictionaries, independent
cash and risk oracles, repeated JSON serialization, exact AI ceilings, and
portfolio/source changes during executor work. Python compilation, shipped
JavaScript syntax checks and whitespace validation also passed. The final
case ledger, its plan hash and all participating source hashes were read back
and verified independently after the audit completed.

## Methodological basis

- [Lobo, Fazel and Boyd (2007), *Portfolio optimization with linear and fixed
  transaction costs*](https://stanford.edu/~boyd/papers/portfolio.html): fixed
  charges introduce discrete choices and make a continuous proportional
  adjustment insufficient for general feasibility or optimality claims.
- [MOSEK Portfolio Optimization Cookbook, transaction costs](https://docs.mosek.com/portfolio-cookbook/transaction.html):
  distinguishes zero trades from positive trades with fixed charges and
  formulates their cash constraints explicitly.
- [Home Assistant, working with async](https://developers.home-assistant.io/docs/asyncio_working_with_async/):
  provides the executor interface used for the initial synchronous calculation.

These sources motivate implementation choices. They do not establish that
this signal or maximum-deployment objective predicts future market returns.
