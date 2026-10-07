# Verified outcome readout

**All 84 predeclared trials completed, and the independent accounting audit passed with zero violations across 64,712 daily points, 3,072 decisions and 11,141 filled purchases.** The 45 research tests separately cover mathematical oracles, chronology, inception, fees, cash and recovery-ledger behavior; these counts are not the integration's complete test-suite count.

The final evidence is in [REPORT.md](REPORT.md), [results.json](results.json), [experiment_ledger.jsonl](experiment_ledger.jsonl) and [independent_accounting_audit.json](independent_accounting_audit.json). Prior failed studies remain in the [invalidated-run archive](../invalidated-runs-20261006.tar.gz) with a [readable account of their failures](../INVALIDATED_RUNS.md).

## What the observed-data comparison shows

The primary medium-risk scenario supplied 48 contributions of €100, totalling €4,800, from November 2022 through the last available observation on 2026-10-02. It used five surviving EUR Xetra accumulating funds, illustrative €1 fixed purchase fees plus 10 bps of execution costs, fractional currency-budget purchases, and a 20% historical counterfactual drawdown policy. The source snapshot starts 2021-10-06; the earlier year supplies history. January 2025 onward is a later chronological partition of known history, not an untouched prospective holdout.

| Strategy | Ending nominal wealth (€) | Unitized maximum drawdown | Annualized observed volatility | Total costs (€) |
|---|---:|---:|---:|---:|
| frozen_v13 | 5261.01 | 12.55% | 5.29% | 230.83 |
| current | 5286.43 | 12.98% | 5.44% | 230.80 |
| strategic | 5581.94 | 14.19% | 7.94% | 229.85 |
| contribution_directed | 5496.34 | 14.19% | 7.46% | 226.38 |
| long_only_trend | 5322.67 | 9.26% | 4.90% | 201.62 |

The current model ended €25.42 above the frozen model in this scenario, while its observed drawdown was higher (12.98% versus 12.55%). This is mixed evidence, not dominance. The simple strategic and contribution-directed baselines had higher ending wealth and higher realized volatility. Shared policy limits do not make those exposures identical, and this comparison does not establish alpha or prospective superiority.

## Costs materially affect a small contribution plan

The current model made 225 purchases and incurred €230.80 of execution and rounding costs, about 4.81% of contributed capital. Under the separate zero-fee scenario it ended at €5552.95; that difference also includes the subsequent investment effect of retaining capital, so it is not simply the fee total subtracted once.

The whole-unit sensitivity made 52 purchases with €56.67 in costs and 7 no-purchase months, ending at €5298.49. Whole units also change instrument weights and timing; this result does not prove whole-unit execution universally improves investing. It does show why both fee assumptions and lot constraints need to be explicit.

## Existing concentration can cause sustained abstention

The scenario beginning with 50 EUNL units and €500 cash caused the current guard to abstain on 47/48 decision dates. It made 3 purchases in total. The trace reports 47 decisions as `existing_breach_not_worsened` and one as `within_limits`.

This is a real consequence of the conservative policy: paying a fee lowers total portfolio value and can slightly worsen an already-breached sleeve fraction even if the intended purchase is in another sleeve. Existing holdings are not sold by this planner. The algorithm consequently leaves cash until a permissible plan exists. The result is reported as a tradeoff; the policy was not loosened after observing the comparison.

## Experimental methods remain research-only

Fixed diagonal shrinkage, the multiple-window volatility cap and the smoothed volatility cap produced the same primary allocations and outcomes as the current model. Their additional constraints were inactive under these cases. The 0.25/0.50/0.75 shrinkage sensitivities were also ties. They therefore provide no measured improvement in this experiment and were not promoted to production.

The long-only trend filter changed outcomes, with fewer purchases and different drawdown/return tradeoffs. The 30-, 40- and 50-week variants remain reported, without selecting a winner. A favorable result on the known surviving universe is insufficient evidence for promotion.

Each final portfolio also has a 128-repetition, eight-week joint moving-block bootstrap diagnostic. Its quantiles are conditional resampling diagnostics from the observed sample, not future-loss probabilities or predictive guarantees. Missing duration, credit and currency-exposure evidence remains unknown in the corresponding factor scenarios.

## Exact execution and implementation scope

[execution_protocol.json](execution_protocol.json) records the choices used throughout the run. In particular:

- Decisions use the previous completed session; fills use the next observed close with explicit hypothetical charges.
- Orders are currency-capped. A lower next-session price may buy more units than the earlier quoted unit estimate. This is not exact parity with an order entered for that earlier unit count.
- The additional manual candidate cap is explicitly 100% (no extra cap beyond the active risk/sleeve constraints). This is not the UI's default 45% medium-diversification cap. Minimum data reliability is 45%, overlap is allowed and no extra minimum cash reserve is selected.
- The purchase budget includes prior unspent cash plus the new contribution. Only the actual new contribution is counted as an external cash flow, preventing cash reserves from being counted twice.
- The replay exercises the frozen/current signal scorer, allocation model and current full-portfolio/cost finalizer. It does not recreate historical Home Assistant state or the live provider, historical identity, source-freshness and UI paths.
- Risk limits describe current weights replayed over historical observations. They do not cap the investor's later realized drawdown, the cumulative effect of trading fees, intramonth losses or next-session price changes. Several deliberately severe synthetic baseline paths exceed 20% realized drawdown despite the 20% historical decision policy; those results are retained.
- The universe and adjusted prices are a current vintage. Delisted products, historical universe membership, historical broker schedules, taxes, cash interest and actual inflation were not reconstructed. Real wealth appears only in the separately labelled 2% inflation-assumption scenario.

## Checkpoint recovery and final integrity

The original version 3 process returned a failure after 79 durable ledger records without a Python traceback establishing its cause. The source, plan, data, all 79 completed traces and the complete ledger chain were verified before continuation. The resume runner recomputed only unledgered trials 080–084. Existing orphan traces 080 and 081 matched recomputation byte-for-byte before reuse. See [recovery-01.json](recovery-01.json) and [recovery-completed.json](recovery-completed.json).

The final ledger has 84 records with a valid hash chain, the source fingerprint is unchanged, and the independent auditor separately reconstructs all purchases, costs, cash, held quantities and unitized NAV. No audit tolerance, economic parameter or strategy was changed to obtain a pass. The earlier one-cent research accounting error was corrected with decimal cash-flow addition and explicit regression coverage before this final run.
