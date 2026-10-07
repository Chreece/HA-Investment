# Chronological investment validation

This directory contains executable research, a frozen comparison model, retained
source data, a plan specified before the outcome study, and versioned results.
Nothing in this directory is imported by the live Home Assistant integration.
Research variants are **not automatically promoted to the recommendation model**.

## Completed study

The final [verified readout](results/portfolio-robustness-20261006-v3/READOUT.md)
explains the mixed outcomes, cost sensitivity, concentration-related abstention,
inactive challengers and exact scope. The [complete tables](results/portfolio-robustness-20261006-v3/REPORT.md)
and [independent accounting audit](results/portfolio-robustness-20261006-v3/independent_accounting_audit.json)
cover **84 trials, 64,712 daily observations, 3,072 decisions and 11,141 fills**,
with zero final audit violations. The frozen code/data/plan and full ledger
passed integrity checks. Earlier failures are retained in a verified archive
with a [readable explanation](results/INVALIDATED_RUNS.md).

## What is compared

The reference implementation is copied byte-for-byte from commit
`f9c69933dbe79fdeeeb9fe78183095e184c2f30d`. Its allocation model, signal scorer and
identity module are checked against recorded SHA-256 hashes before use. Merely
importing the current model twice under different names would not create a valid
frozen comparison, so the reference source is retained separately.

The current implementation uses its actual `validated_model` and actual
`portfolio_plan.finalize_purchase_plan`. Both versions use their own frozen or
current `indication.analyze_prices` with the medium-horizon, daily, adaptive signal
scaffold. The simulation exercises model and purchase-plan code; it does not
pretend to reconstruct historical Home Assistant state, provider authentication,
historical issuer reviews or the live source-freshness gates.

Six other strategies provide comparisons:

| Strategy | Definition |
| --- | --- |
| Strategic allocation | Prespecified risk-policy sleeve targets, equal division within each available sleeve, no tactical score selection |
| Contribution-directed allocation | Purchases directed toward positive deficits against those same complete-portfolio targets; no sales |
| Fixed diagonal covariance shrinkage | Current suggestions reduced if a covariance estimate shrunk halfway toward its own diagonal requires it |
| Multiple risk windows | Current suggestions reduced by the most restrictive volatility estimate from 26, 52, 104 and all available aligned weeks |
| Smoothed volatility cap | A 26-week volatility cap smoothed with a fixed 0.25 update weight, bounded between zero and current purchase ceilings |
| Long-only trend filter | Purchases of non-cash-like instruments withheld below their 40-week average adjusted price; no selling or short positions |

Fixed diagonal shrinkage is **not** the Ledoit–Wolf optimal-intensity estimator.
That distinction is intentional. This experiment tests a simple, reproducible,
prespecified alternative, including intensities 0.25 and 0.75, without attaching a
stronger published method's name to it. All alternatives only reduce permissible
purchases and pass through the current full-portfolio guard. Consequently a
challenger can be inactive or produce exactly the same orders as the current
model; that is a valid result, not evidence of improved performance.

## Timing and accounting contract

At the first observed trading session of each month, the simulation adds the
declared external contribution and executes the recommendation formed using the
**previous completed session's** prices. An order specifies a maximum amount of
principal. The next observed close supplies the execution price, with the same
explicit charges for every comparison. A missing execution observation rejects
that order; it is never replaced by a later price or a forward-filled trade.

This is explicitly a **currency-budget execution experiment**. If the next
session's price falls, the same approved principal can purchase more units than
the preceding price-based unit estimate. The live planner preserves both the
incoming principal and unit ceilings at its current quote; this experiment does
not claim exact parity with a broker order entered for that earlier unit count.
If execution rises, fewer units fit; whole-unit experiments always round down.

The replay corresponds to explicit medium-horizon/adaptive scoring, 45% minimum
data reliability, overlap allowed, no minimum cash reserve, and an explicit
**100% additional per-instrument manual cap** (equivalently, no extra cap beyond
the risk/sleeve constraints). It does not claim to reproduce the UI's default
medium-diversification 45% candidate cap. Those choices are fixed throughout the
study; outcome comparisons do not silently change production defaults.

Existing holdings persist and are never sold. Unspent cash carries forward and
is explicitly offered as part of the next month's available purchase budget.
The full-portfolio adapter passes `carry cash + new contribution` as the purchase
budget and zero additional outside cash, avoiding double counting. The guard's
`baseline_with_contribution`/budget baseline includes that cash pool and all
holdings. The external cash-flow ledger separately records only the actual new
monthly contribution; the whole available purchase budget is never reported as
a fresh deposit.

The default cost scenario is an **illustrative** €1 fixed charge per purchase
plus 10 basis points of execution costs. It is not a quote from a broker.
Commission and FX charges are zero in this all-EUR-listing scenario. Fees,
rounding and cash debits are calculated independently from the production cost
planner with `Decimal`, and affordability is independently tested against an
exhaustive whole-lot oracle.

Performance uses a unitized portfolio NAV. External deposits issue units at the
pre-deposit NAV; trading costs reduce NAV. Adding cash therefore cannot hide an
investment loss. The report includes nominal wealth, net gain, costs, risk,
drawdown, time below a peak, trade counts, turnover, unspent cash, execution
rejections and abstentions. A real-wealth value is produced only for the separate
explicit 2% annual inflation-assumption scenario; no actual inflation series has
been inferred.

The 20% drawdown limit used in the primary experiment is a prespecified example
of the new user input. It is not a calibrated profile default or a future-loss
guarantee. A sensitivity run leaves this optional limit unset. Named risk policies
have the same constraints, but realized volatility is measured rather than
retrospectively forced to match.

## Observed data and its limitations

`data/yahoo-xetra-20261006.json` contains completed daily raw/adjusted closes for
five exact accumulating EUR Xetra fund listings: XEON, VAGF, SXR8, EUNL and VWCE.
The captured observations run from **2021-10-06 through 2026-10-02**, with
1,271–1,273 nonmissing observations per listing. Raw responses are compressed and
retained with checksums, source URL, fetch time, venue, currency, session timezone,
missing dates and reported corporate actions. In this snapshot 2026-10-05 is
missing for each listing; the capture deliberately excludes the current
2026-10-06 session. The simulation does not invent an observation for either date.

The capture fails if a split or cash-dividend event is reported. The bounded
experiment uses accumulating funds without those reported events; it does not
silently mishandle splits or distribute unmodelled dividends. It must be extended
and independently checked before using instruments with corporate-action cash
flows or changing nominal share units.

The chronology respects each listing's recorded inception. However, this is a
**current adjusted-history vintage and a universe selected from today's surviving
funds**. It cannot reconstruct delisted funds, historic universe membership,
historical metadata availability or revisions to past prices. It is therefore a
conditional retrospective study, not a bias-free estimate of a strategy's
prospective investment return.

The first year provides minimum history. Portfolio simulation starts in November
2022. The fixed development partition ends in December 2024; January 2025 onward
is a later chronological evaluation partition of already known history. No
parameter is fitted on either partition. This is not advertised as an untouched
prospective holdout.

Synthetic persistent decline, sharp reversal, simultaneous equity/bond decline,
flat returns and late-inception paths are separate adversarial scenarios. They
have no assigned probabilities and do not establish investment performance.
They test whether accounting and control behavior remain interpretable when
the recent-history assumptions become weak.

## Reproduce the study

Python's standard library is sufficient for the replay. Run from the repository
root, choosing a new identifier each time:

```bash
python research/chronological_validation.py --data research/data/yahoo-xetra-20261006.json --run-id unique-run-name
```

If a process is interrupted before its final result is written, its verified
ledger can be resumed without rerunning or overwriting completed trials:

```bash
python research/resume_validation_run.py --run-dir research/results/unique-run-name --data research/data/yahoo-xetra-20261006.json
```

Recovery requires unchanged engine/model, data and plan hashes, a complete
valid ledger prefix, and unchanged retained traces. Unledgered orphan traces
must match deterministic recomputation byte-for-byte. Version 3 used this
recovery after 79 valid records, with both existing orphan traces matching;
all 84 completed records subsequently passed the independent audit.

After completion, run the separate audit over the retained source and fills:

```bash
python research/audit_replay_results.py --run-dir research/results/unique-run-name --data research/data/yahoo-xetra-20261006.json
```

The plan includes **84 prespecified trials**: primary comparisons for low and
medium risk, zero-cost runs, €50 and €250 contributions, whole-unit purchases,
optional drawdown policy, parameter sensitivities, an explicit inflation
assumption, pre-existing holdings, and five adversarial regimes.

Each run writes a copy of the plan before computing outcomes. Every completed or
failed trial is appended immediately to a hash-chained ledger. Raw decision and
daily-accounting traces are compressed separately. Reusing an existing run ID is
refused. Data, model and research source fingerprints identify exactly what was
tested. If source files change during the run, the results remain retained but
are explicitly invalidated for final-source comparison, and the process exits
unsuccessfully.

To fetch a **new** source snapshot, explicitly opt into network access and choose
a new filename; existing evidence is never overwritten:

```bash
python research/fetch_validation_data.py --output research/data/new-source-snapshot.json
```

The tests include closed-form covariance checks, an independent quantile-grid ES
oracle, exhaustive whole-lot affordability, next-session price changes, missing
observations, exact inception, future-data perturbation, deposit-neutral
performance accounting, first-purchase fees, immutable reference hashes and
write-once ledger behavior:

```bash
python -m pytest -q tests/test_chronological_validation.py
```

## Interpretation and promotion policy

The outcome study is intended to reveal both improvements and deterioration.
Every predeclared result, including no-op challengers and failed runs, remains
available. It provides no automatic rule that chooses the highest historical
return. Promotion would require additional independent evidence, realistic
forward behavior and an explicit assessment of cost and risk tradeoffs.

No historical comparison makes investments future-safe. Costs, source quality,
cash accounting and exposure constraints can be tested directly. Future market
returns, correlations, liquidity and crisis behavior remain uncertain.
