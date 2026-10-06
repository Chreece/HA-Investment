# Chronological investment experiment

Run: `portfolio-robustness-20261006-v3`. Baseline commit: `f9c69933dbe79fdeeeb9fe78183095e184c2f30d`.

Completed 84 prespecified trials; retained 0 failed trials.

This is an engineering and retrospective comparison on a current surviving universe. The later period is a chronological evaluation partition of known history, not an untouched prospective holdout. No alternative is automatically promoted to production.

## Primary observed-data comparison

All entries use the same explicit contribution, price timing, cost and lot scenario. Risk profiles share policy limits; realised risk is measured, not forced to match retrospectively.

| Risk | Strategy | Ending wealth (€) | Cost (€) | Unitized drawdown | Annualized volatility | Ending cash | No-purchase decisions |
|---|---|---:|---:|---:|---:|---:|---:|
| low | frozen_v13 | 5117.15 | 230.55 | 12.35% | 4.33% | 0.00% | 0/48 |
| low | current | 5130.90 | 230.68 | 12.67% | 4.44% | 0.00% | 0/48 |
| low | strategic | 5273.97 | 199.88 | 14.39% | 5.81% | 15.75% | 9/48 |
| low | contribution_directed | 5170.13 | 213.06 | 14.32% | 5.37% | 9.56% | 0/48 |
| low | fixed_diagonal_shrinkage | 5130.90 | 230.68 | 12.67% | 4.44% | 0.00% | 0/48 |
| low | robust_windows | 5130.90 | 230.68 | 12.67% | 4.44% | 0.00% | 0/48 |
| low | smoothed_volatility_cap | 5130.90 | 230.68 | 12.67% | 4.44% | 0.00% | 0/48 |
| low | long_only_trend | 5158.55 | 201.59 | 6.84% | 3.72% | 0.36% | 0/48 |
| medium | frozen_v13 | 5261.01 | 230.83 | 12.55% | 5.29% | 0.00% | 0/48 |
| medium | current | 5286.43 | 230.80 | 12.98% | 5.44% | 0.00% | 0/48 |
| medium | strategic | 5581.94 | 229.85 | 14.19% | 7.94% | 9.94% | 3/48 |
| medium | contribution_directed | 5496.34 | 226.38 | 14.19% | 7.46% | 4.82% | 0/48 |
| medium | fixed_diagonal_shrinkage | 5286.43 | 230.80 | 12.98% | 5.44% | 0.00% | 0/48 |
| medium | robust_windows | 5286.43 | 230.80 | 12.98% | 5.44% | 0.00% | 0/48 |
| medium | smoothed_volatility_cap | 5286.43 | 230.80 | 12.98% | 5.44% | 0.00% | 0/48 |
| medium | long_only_trend | 5322.67 | 201.62 | 9.26% | 4.90% | 0.32% | 0/48 |

## Later chronological evaluation partition

Performance below is measured from the boundary NAV, with each strategy's existing holdings carried forward. Ending wealth includes earlier contributions and is therefore omitted from this period-return table.

| Risk | Strategy | Time-weighted return after costs | Unitized drawdown | Annualized volatility |
|---|---|---:|---:|---:|
| low | frozen_v13 | 5.13% | 6.23% | 4.06% |
| low | current | 5.44% | 6.72% | 4.33% |
| low | strategic | 7.54% | 8.51% | 5.47% |
| low | contribution_directed | 6.03% | 7.34% | 4.81% |
| low | fixed_diagonal_shrinkage | 5.44% | 6.72% | 4.33% |
| low | robust_windows | 5.44% | 6.72% | 4.33% |
| low | smoothed_volatility_cap | 5.44% | 6.72% | 4.33% |
| low | long_only_trend | 5.94% | 6.81% | 4.34% |
| medium | frozen_v13 | 7.07% | 8.54% | 5.44% |
| medium | current | 7.47% | 9.04% | 5.74% |
| medium | strategic | 11.56% | 12.89% | 8.29% |
| medium | contribution_directed | 10.49% | 11.71% | 7.61% |
| medium | fixed_diagonal_shrinkage | 7.47% | 9.04% | 5.74% |
| medium | robust_windows | 7.47% | 9.04% | 5.74% |
| medium | smoothed_volatility_cap | 7.47% | 9.04% | 5.74% |
| medium | long_only_trend | 7.94% | 9.26% | 5.80% |

## Scope and interpretation

- Each month uses the previous completed session's observations; currency-capped orders execute at the following observed close. No future price is used to choose weights.
- Existing holdings are carried forward and never sold. Contributions and leftover cash are available for subsequent decisions.
- Costs are explicit illustrative scenarios, not a broker quote. Shares, cash, fees and subcent rounding are accounted for independently of the production planner.
- Deposits issue portfolio units at the pre-deposit NAV; performance and drawdown use unitized NAV so deposits cannot hide investment losses.
- The frozen model is the exact retained allocation and signal source from the baseline commit. The current model additionally uses the new complete-portfolio finalizer. This is a model/allocator replay, not an end-to-end Home Assistant/provider/authentication replay.
- The declared 20% cumulative-loss policy is an illustrative input, not an empirically calibrated default or a maximum possible future loss.
- The simple baselines obey the same named risk policy and current full-portfolio guard. They do not have identical realised volatility, which is reported separately.
- Fixed diagonal covariance shrinkage uses a prespecified lambda; it does not implement or claim Ledoit-Wolf optimal intensity. All challengers only reduce suggested purchases and undergo the same final guard.
- The zero-fee, contribution-size, whole-lot, drawdown-policy and parameter sensitivity trials are retained in JSON alongside synthetic persistent decline, reversal, joint equity/bond loss, flat returns and late-inception cases.
- Synthetic scenarios are deliberately constructed stress paths without assigned probabilities. They do not establish expected investment performance.
- Current adjusted data can contain historical revisions. The universe contains today's surviving verified funds, excludes delisted products and has no point-in-time membership reconstruction.
- Uninvested cash earns zero in this experiment. Taxes, historical spreads, actual broker fee schedules and cash interest are not reconstructed. Real wealth is only computed when an explicitly labelled inflation assumption is supplied.
- Observed dates cover the source snapshot only; the experiment cannot validate historical crises outside those dates or guarantee future returns.
- Every attempted trial is retained in a write-once hash-chained ledger with source, data, configuration and code fingerprints. Reusing a run ID is refused.

## Verified interpretation and exact execution scope

See [READOUT.md](READOUT.md) for the independently audited findings, costs, concentration-related abstention, research-only variants, checkpoint recovery and precise execution scope. [execution_protocol.json](execution_protocol.json) records the explicit 100% manual-cap and currency-order assumptions; this is not claimed to be the UI default configuration or fixed-unit execution parity.
