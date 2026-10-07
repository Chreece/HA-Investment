# Indication portfolio robustness — third improvement batch

Base: `f9c69933dbe79fdeeeb9fe78183095e184c2f30d`, the source-evidence and
instrument-identity batch in PR #12. This document supplements
[batch 1](SAFETY_GATES_V1.md) and [batch 2](SAFETY_GATES_V2.md).

The new final purchase contract is `complete-portfolio-confirmed-costs-v1`.
Risk diagnostics use `dated-risk-diagnostics-v1`; execution costs use
`confirmed-costs-downward-cash-debit-v1`. These are implementation contracts,
not certifications of investment performance or future loss limits.

## Exact tail loss and explicit drawdown policy

Weekly expected shortfall now integrates the empirical lower 5% quantile with
fractional boundary mass. For 52 weekly observations the tail contains two
complete observations and 0.6 of the third, divided by 2.6. Averaging the worst
three observations gave the wrong empirical statistic. In the retained
counterexample with returns −20%, −5%, −1% and 49 observations of +1%, the old
loss estimate is 8.6667%; exact empirical ES95 is 9.8462%.

The minimum history requirement remains 52 aligned weekly returns for the
portfolio gate. The ES helper's own minimum remains 30 observations, so a
26-week diagnostic deliberately shows unavailable ES. Evidence reports the
actual tail mass and observation count; 52 weeks do not become 52 independent
tail observations. The existing volatility/ES targets and their 1% numerical
calibration tolerance remain explicit policy inputs.

An optional `max_drawdown_pct` supplies a separate historical peak-to-trough
loss bound. No drawdown tolerance is invented from a named risk profile. An
unset value produces diagnostics without a drawdown gate; a configured value
must be greater than zero and at most 100%. The bound applies to the complete
portfolio after the proposed purchases. For example, constant −1% weekly
returns have zero volatility but about 40.70% compounded drawdown over 52 weeks;
the new optional drawdown gate detects that path risk.

Drawdown calculations use log wealth to remain defined under extreme gains,
underflow and total losses. Invalid or nonfinite observations cannot be
silently replaced by cash returns. Missing calendar weeks prevent the code
from claiming an uninterrupted full drawdown path. If a drawdown bound is
required but that path cannot be established, new purchases are blocked.

## Cash and execution costs

The purchase budget is cash designated for this plan. It can include unspent
earlier contributions. `existing_cash` means other cash already held,
**excluding the entire designated purchase budget**. It affects portfolio
risk, but cannot fund a purchase beyond that budget. The minimum cash-reserve
percentage applies to the designated purchase budget.

Every purchase requires explicit cost estimates:

| Input | Unit and treatment |
| --- | --- |
| `fixed_fee` | Portfolio-currency amount per executed purchase |
| `commission_pct` | Percentage of marked purchase principal |
| `spread_bps` | Additional execution spread in basis points of principal |
| `fx_bps` | Additional FX charge in basis points of principal |
| `confirmed` | Must be explicitly true before costs authorize purchases |

All four charges apply to every purchase in this version. Users should enter
only costs not already included in the price. Confirmed all-zero costs are
supported; missing or unconfirmed costs mean unknown, not free execution.
An unknown cost profile still permits analysis but produces no purchase plan.
Broker-specific schedules, instrument-specific exemptions, live spreads and
tax-lot optimization are not inferred.

The pure cost engine uses Decimal arithmetic, retains the raw quote rather
than rounding the price to cents, floors the available cash, rounds debits
up to cents, and only reduces incoming principal and quantity ceilings. Whole
units remain whole; fractional units use a bounded precision. Each purchase's
estimated cash debit includes both principal and estimated costs. The reported
`rounding_allowance` is a component of `estimated_transaction_cost`, not an
additional fee to add twice.

The final accounting identity is:

```text
marked holdings + marked new purchases + cash after purchases
  = marked holdings + other cash + designated purchase budget
    - estimated transaction costs (including cash-rounding allowance)
```

An initially rounded principal ceiling can be too small for a raw-price whole
lot. The finalizer removes that lot rather than increasing purchase authority
to fit it. No executor, broker order or transfer is triggered by these checks.

## Complete portfolio and authoritative owned quantities

The final gate values every existing positive position, including holdings
excluded from the new candidate set. It uses independently verified current
prices, exact instrument identities, daily source histories and observed FX
from the existing source-evidence contract. A holding cannot disappear merely
because it has no current buy score or fails a candidate exclusion rule.

Owned quantities are reconstructed locally from immutable BUY/SELL records,
including transaction-local shared ownership. The portfolio display may retain
stored aggregates when a quote, historical FX request or enrichment deadline
fails. Those display fallbacks cannot establish quantity evidence for this
gate. Missing, invalid or ambiguous transaction evidence remains unknown.
Verified zero owner quantities, including fully shared purchases, require no
market history. Failed positions are identified in the result so the user can
locate the missing evidence.

The analysis also fingerprints immutable transaction and instrument inputs
before awaited market requests. A final storage read checks that membership,
transactions and the account currency have not changed while feeds or AI were
running. A mismatch blocks the purchase plan and displays a translated request
to run the analysis again. The previous AI review and its purchase reasons are
discarded. An unverifiable fingerprint cannot compare equal as valid evidence;
account-currency stability is required even when portfolio holdings are
explicitly ignored. Display-price and aggregate-quantity refreshes do not
invalidate an otherwise unchanged ledger.

Quantity evidence validates unique records, dates, actual finite quantities,
shared allocations and ownership order. Recorded full creation times resolve
same-date order; IDs do not invent temporal order. An ambiguous BUY/SELL group
is accepted only when opening owner units cover its sells, so every possible
within-group order gives the same valid quantity. This gate does not alter the
stored ledger or its FIFO cost-basis presentation.

The complete pre-purchase baseline includes all designated cash. The post-plan
calculation includes existing holdings, new holdings and cash remaining after
costs. It checks volatility, exact ES, the optional drawdown bound, economic
sleeve caps and the effective per-instrument cap. Independently verified ISINs
aggregate duplicate listings of the same fund; other positions retain exact
provider identities. This is not a complete constituent look-through model.

If an existing limit is already exceeded, a purchase may leave that breach in
place only when none of the enforced metrics or concentration limits worsens
relative to keeping the designated cash unspent. The result explicitly says
that an existing breach remains. Zero purchases cannot certify an unknown or
already breached portfolio as safe.

This is deliberately conservative. When a fixed existing holding is already
overweight, paying a purchase fee lowers the total-value denominator and can
slightly increase its weight, even if the new purchase is in another sleeve.
That plan can be withheld under the non-worsening rule. Cash retention,
abstentions and this limitation are reported rather than optimized away after
examining historical results.

The bounded search keeps only independently verified feasible reductions. It
does not claim to find the best discrete allocation. The original approved-set
and no-resurrection rules remain in force. AI sees the already affordable,
constrained ceilings. Its final amounts and units are bounded again, and the
complete portfolio is checked after any selective AI reductions that could
remove a hedge. Source freshness is rechecked after AI latency. The returned
`evidence_as_of` now includes this final check, superseding the earlier
pre-AI cutoff described in batch 2.

Explicitly ignoring portfolio context still evaluates only the purchase
budget, and the UI labels that restricted scope. It is not shown as a complete
portfolio assessment.

## Risk evidence and stress diagnostics

Historical portfolio statistics replay today's fixed weights over aligned
past returns, with residual cash earning zero nominal return. They are
counterfactual risk measurements, not the user's actual return history or an
executable weekly rebalancing backtest.

The result includes:

- Full history and recent 26-, 52- and 104-week statistics, with missing windows
  explicitly unavailable.
- Observed drawdown, current and longest time below a prior peak, and whether
  recovery is still incomplete at the end of the sample.
- A deterministic moving-block bootstrap with 128 repetitions, eight-week
  blocks and a fixed seed. Blocks preserve the joint observed return path and
  never bridge missing weeks. The displayed 5th–95th percentile ranges are
  conditional resampling diagnostics, not future coverage probabilities.
- Illustrative simultaneous asset declines, a sustained decline, a sharp
  decline followed by full recovery, higher rates, wider credit spreads and
  adverse unhedged currency moves.
- Observed contiguous outcomes for an explicitly requested integer horizon in
  weeks. Insufficient history remains unavailable; overlapping windows are
  disclosed and are not independent future trials.

Rate, credit and currency scenarios require explicitly verified, dated
exposure inputs. The current live provider path does not supply duration,
spread duration or unhedged currency fractions. Those scenarios therefore
remain unknown where their exposures are missing. Trading in EUR is not proof
of zero foreign-currency exposure, and a fund name is not a duration estimate.
The illustrative scenarios have no assigned probabilities and do not
automatically become calibrated allocation limits.

The UI carries these settings and diagnostics through both shipped panel
layers, including all 28 supported languages. Purchase costs, before/after
portfolio risk, existing breaches and unavailable evidence are visible. The
asset revision is `0.4.0-r61`.

## Chronological comparison and research challengers

The reproducible study is described in [research/README.md](../research/README.md).
It retains a byte-for-byte frozen V13 model/scorer/identity baseline, current
pure construction and purchase gates, strategic and contribution-directed
baselines, and four research challengers: fixed diagonal covariance shrinkage,
multiple risk windows, a smoothed volatility cap and a long-only trend filter.
These research variants are not selected automatically for live recommendations.

The plan contains 84 predeclared trials on an observed five-fund EUR Xetra
snapshot and separate synthetic regimes. Contributions, charges, cash carry,
whole lots, complete holdings, chronology, inception and unitized performance
are recorded. Decisions use the preceding completed session; fills use the
next observed close. The replay's orders are currency-capped, not exact
UI-unit orders, and use an explicit 100% optional candidate cap rather than
the UI's default 45% cap. See the retained execution protocol for this scope.

All tried configurations and failed or invalidated runs remain recorded. The
first run was invalidated when defensive risk-source fixes changed its source
fingerprint. The second run's independent audit found a one-cent float/cash
carry error and a missing experiment-ledger record. Those results remain
invalid; the missing record was not reconstructed into the original ledger.
Cash additions now use Decimal, and durable append/readback checks verify the
entire ledger chain and count. The entire unchanged economic plan was rerun
against frozen sources and subjected to the same strict independent audit. The
study uses today's adjusted-data vintage and today's surviving fund universe.
Its later chronological partition is already known history, not an untouched
prospective holdout. It cannot establish future outperformance or remove
survivorship and revision bias.

## Research basis and limits

The implementation follows the useful distinctions in the earlier research
review without claiming that these papers prove this retail strategy:

- [Goldberg and Mahmoud, *Drawdown: From Practice to Theory and Back Again*](https://arxiv.org/abs/1404.7493)
  motivates measuring loss paths and recovery separately from volatility.
- [Politis and Romano, *The Stationary Bootstrap*](https://users.ssc.wisc.edu/~behansen/718/Politis%20Romano.pdf)
  supports dependence-aware resampling research. This implementation uses a
  fixed-length moving-block diagnostic, not that paper's random-length method.
- [Ledoit and Wolf, *Honey, I Shrunk the Sample Covariance Matrix*](https://www.ledoit.net/Honey_2004.pdf)
  motivates studying estimation error. The challenger here has a fixed diagonal
  shrinkage intensity and is not the paper's estimated optimal-intensity method.
- [DeMiguel, Garlappi and Uppal, *Optimal Versus Naive Diversification*](https://doi.org/10.1093/rfs/hhm075)
  motivates explicit simple baselines rather than assuming a more elaborate
  allocation must perform better.
- [Gârleanu and Pedersen, *Dynamic Trading with Predictable Returns and Transaction Costs*](https://w4.stern.nyu.edu/facdir/lpederse/papers/DynamicTrading.pdf)
  motivates making trading costs part of decisions. This implementation is a
  bounded contributions-only purchase check, not their dynamic optimizer.
- [Moreira and Muir, *Volatility-Managed Portfolios*](https://amoreira2.github.io/alan-moreira.github.io/VolPortfolios_published.pdf)
  and [Cederburg et al., *On the Performance of Volatility-Managed Portfolios*](https://www.lehigh.edu/~xuy219/research/COWY.pdf)
  motivate testing volatility management and its implementation limits, not
  assuming universal improvement.
- [Bailey et al., *The Probability of Backtest Overfitting*](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)
  and [Bailey and López de Prado, *The Deflated Sharpe Ratio*](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
  motivate retaining the full experiment history. This study does not fabricate
  a probability of overfitting or a deflated Sharpe statistic from an inadequate
  set of independent trials.
- [Basel Committee, *Stress Testing Principles*](https://www.bis.org/publications/201810-guidelines-stress-testing-principles.pdf)
  motivates explicit adverse scenarios and limitations. These scenarios are
  diagnostic examples, not regulatory stress certification.

## Verification

Verification on 2026-10-06:

- **1,880 tests passed** with the pinned LinkeDOM test environment, including
  the 1,287 previously retained cases and 593 additional cases.
- Python compilation, both shipped JavaScript syntax checks and
  `git diff --check` passed.
- Final study `portfolio-robustness-20261006-v3`: **84/84 trials completed**
  with an unchanged source/data/plan fingerprint and a complete hash-chained
  experiment ledger.
- The independent auditor recomputed **64,712 daily accounting points, 3,072
  decisions and 11,141 simulated fills** with **zero violations** of its
  chronology, source/trace identity, inception, minimum-history, observed-fill,
  principal-ceiling, cost, whole-unit, cash, holdings and unitized-NAV checks.
  The parent review reran this audit independently before publication.

An execution interruption left a verified 79-trial prefix in the final run.
Checkpoint recovery verified all previous plan/source/data/ledger identities,
recomputed two orphan traces byte-for-byte, and completed the five unledgered
trials. It did not replace the verified prefix or select outcomes. Recovery
records, the complete final ledger and original invalidated runs are retained.

The test suite covers
exact ES with an independent quantile oracle, strict calendars, path arithmetic,
cost and lot affordability, invalid held values, unknown evidence, existing
breaches, complete-portfolio AI checks, real provider/parser/runtime boundaries,
ledger quantity recovery and both UI layers. Passing these tests establishes
the documented software behavior; it does not establish future investment
returns.

Reproduction and results:

- [Final study report](../research/results/portfolio-robustness-20261006-v3/REPORT.md)
- [Independent accounting audit](../research/results/portfolio-robustness-20261006-v3/independent_accounting_audit.json)
- [Exact execution protocol](../research/EXECUTION_PROTOCOL.json)
- [Invalidated-run evidence and archive](../research/results/INVALIDATED_RUNS.md)

## What the comparison actually found

The primary medium-risk example contributes €100 per month for 48 decisions,
€4,800 in total, with an illustrative €1 charge plus 10 basis points per
purchase. It uses the observed five-fund EUR Xetra universe, zero interest on
uninvested cash, fractional units and the explicitly configured historical
drawdown input. These are research inputs, not the user's actual broker costs.

| Model / policy | Ending wealth | Maximum unitized drawdown | Execution costs |
| --- | ---: | ---: | ---: |
| Frozen V13 | €5,261.01 | 12.55% | €230.83 |
| Current guarded construction | €5,286.43 | 12.98% | €230.80 |
| Simple strategic allocation | €5,581.94 | 14.19% | €229.85 |
| Contribution-directed allocation | €5,496.34 | 14.19% | €226.38 |
| Experimental long-only trend filter | €5,322.67 | 9.26% | €201.62 |

The current construction has slightly higher ending wealth and slightly larger
observed drawdown than its frozen comparator in this example. The simple
baselines have higher ending wealth and higher measured volatility. None of
these findings establishes universal superiority. Fixed diagonal shrinkage,
multiple risk windows and smoothed volatility produced the same primary orders
as the current construction; they supplied no additional primary-sample benefit.
The trend variant's different outcome is a reason for further evaluation, not
automatic promotion after seeing this sample.

Costs are material at small contribution sizes: €230.80 is about 4.81% of the
€4,800 supplied in this illustrative scenario. The separate zero-fee current
case ended at €5,552.95; that difference also includes the resulting changes in
purchase quantities and later compounding. Confirming realistic costs matters
more than treating the example fee schedule as the user's own.

The pre-existing concentrated-equity sensitivity is especially restrictive:
the current guard made no purchases in **47 of 48** decisions. It ended with
about 30.87% cash, versus no cash for the frozen comparator, and lower ending
wealth as well as a smaller observed drawdown. This demonstrates the practical
cost of the strict non-worsening policy described above. That behavior is
explicit and tested; it is not hidden behind a claim of guaranteed safer
investment performance.
