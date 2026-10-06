# Scientific safety audit — 6 October 2026

## Provenance and scope

Production source audited: `indications` at `5d0a72c65a5d122ac9e090a5f9bdd615ff157c49`.

Experiments executed on isolated audit commit `32e665cf766b2f668e5d1bee2c14de993780403b`.

Completed run: https://github.com/Chreece/HA-Investment/actions/runs/37488972287

Job: `112356338367`. Original artifact ID: `11423589634`, requested retention 30 days. ZIP SHA256: `0438ee9166b928c58a6ae3b057abdfa98a2c8332a2dc660d29de65e274b99768`.

Runnable scripts: `audit/scientific_safety.py`, `audit/eligibility_prototype.py`, `audit/retrospective_market.py`. Workflow: `.github/workflows/scientific-audit.yml`.

The workflow verified that production components and all existing tests were unchanged. Only audit files were added to this separate branch. No algorithm changes were merged into indications or master. The eligibility prototype below was applied to an in-memory module only.

A successful audit workflow means the experiments executed, not that all safety properties passed. The results do not prove future investment safety or returns.

## Executed tests

- Existing suite: 190 passed; shipped JavaScript passed syntax validation.
- Generated portfolios: 300 runs, covering five risk profiles, varied budgets/prices, caps/reserves/confidence and fractional/whole/mixed modes.
- Targeted boundary checks: 13, including positive controls; nine investigated properties failed, several from the same root cause.
- Independent exhaustive whole-lot oracle: 40 finite two-instrument problems.
- Risk generalization: 750 simulations, 250 each for Gaussian, Student-t(4) and volatility-jump paths.
- Covariance estimation: 250 simulations, 15 assets and 80 training observations.
- Historical diagnostic: five EUR-traded UCITS ETFs, 816 common weekly observations, 555 evaluated weekly returns.
- Forecast diagnostic: 2,770 next-week forecasts over 554 calendar weeks, not 2,770 independent weeks.

## 1. Critical eligibility bypass

The whole-lot allocator uses membership in the approved weighted set as a sorting priority, not a hard admissibility condition. It can redistribute sleeve capacity to a candidate already rejected by the signal, confidence or context stage.

Synthetic counterexamples: score 40 below activation floor 48 received EUR 507; confidence 10% below a 45% floor received EUR 468; context scale zero received EUR 247. These are hypothetical instruments and arbitrary test inputs, not claims about named funds.

A candidate with no risk history received EUR 511 alongside a valid-history fractional holding. Its missing risk contribution was omitted and the combined calculation reported zero volatility and expected shortfall using observations from the other holding.

Generated test counts: budget violations 0, collective-sleeve violations 0, manual-cap violations 0, native historical-risk-check violations 0, tested quantity/price consistency violations 0, nonfinite outputs 0, eligibility reintroductions 6. Some are whole/mixed versions of the same generated inputs; this is not a real-world failure-rate estimate.

In-memory prototype: reject a lot-search record when `weighted_by_key.get(_identity(item), 0.0) <= EPS`. All four targeted eligibility probes passed afterward; the same 300 generated cases had zero observed violations in every measured category. This narrow correction was not deployed and does not fix the other problems below.

## 2. Data calendars, classification and risk semantics

The portfolio-return helper unions dates and fills absent asset returns with zero. A synthetic pair with zero overlapping weeks was accepted under the low-risk profile using 6.06% annualized volatility. The aligned same-shock version gave 12.11% and was rejected. Missing observations do not establish either dependence structure; treating missingness as harmless creates unsupported diversification.

Sixty price observations spaced 21 days apart produced 52 accepted 'weekly' returns after the trailing-window filter. These are not 52 comparable one-week returns. A raw stale risk map also received positive weight. These are model-boundary findings, not evidence of an actual provider serving those inputs.

Hypothetical leveraged and inverse S&P 500 wrappers were classified as ordinary broad equity. A hypothetical ultra-short high-yield bond wrapper was classified as cash-like. Manager warning flags do not independently enforce a conservative admissibility gate. Classification confidence 90% for name taxonomy is a fixed value, not measured predictive/classification accuracy.

The final risk guard checks historical volatility and weekly expected shortfall, not maximum drawdown. A synthetic loss of 0.2% each week for 156 weeks passes the very-low risk guard despite 26.82% cumulative drawdown. This tests the guard, not the entire recommendation pipeline.

The production projection has no transaction-fee input. Current holdings affect context/overlap penalties but the final risk construction uses the candidate allocations rather than an explicit complete post-trade portfolio vector.

## 3. Solver completeness

Larger unit-count domains are sampled and the search uses a bounded beam. Against exhaustive enumeration, 27 of 40 outputs were below the exact maximum-deployment solution. Some gaps were only cents. None of these 40 cases produced a false zero.

Consequently, the previous claim that any zero result proves no feasible whole-lot portfolio exists was unsupported. Distinguish proved infeasibility, search exhaustion, missing data, and cost-inefficient/no-trade outcomes. This is not an instruction to force greater deployment.

## 4. Risk uncertainty simulations

Every simulated training portfolio satisfied the native historical risk guard on 156 training weeks. On 52 fresh weeks, empirical volatility/ES target breaches occurred in 129/250 Gaussian paths (51.6%), 103/250 Student-t(4) paths (41.2%), and 250/250 paths after a 2.5-times volatility jump.

These figures are not probabilities of losing money. Fitting to a historical risk boundary does not guarantee the same future estimate; sampling uncertainty and regime changes matter.

In 250 known-covariance Gaussian factor simulations, Ledoit-Wolf shrinkage reduced mean covariance Frobenius error from 0.0012802091 to 0.0012430023, about 2.91%. True annualized volatility of an unconstrained minimum-variance diagnostic moved from 3.0660% to 3.0233%. This is a modest simulation improvement, not validated retail investment performance.

## 5. Retrospective market comparison

Universe fixed for this audit: IEGE.AS, IBGS.AS, EUNL.DE, SXR8.DE, XEON.DE. Adjusted-close data were downloaded for exact symbols, verified EUR quote currency, archived with hashes, and aligned to 816 common weekly observations without interior calendar gaps.

Evaluation: May 2015 to December 2025, 260 initial training weeks, 555 subsequent weekly return periods. Very-long/adaptive model, four-week rebalancing with weight drift. Comparator: equal allocation within available economic sleeves under the same sleeve caps and historical-risk guard, without the current hand-built signal. It is not arbitrary equal weight over all funds.

| Risk profile | Current gross CAGR | No-signal gross CAGR | Current annual vol. | No-signal annual vol. | Current max drawdown | No-signal max drawdown |
|---|---:|---:|---:|---:|---:|---:|
| Very low | 1.37% | 2.59% | 2.53% | 2.83% | -6.46% | -5.82% |
| Medium | 2.33% | 4.91% | 5.21% | 5.69% | -13.42% | -11.52% |
| Very high | 3.00% | 6.85% | 7.19% | 8.15% | -18.06% | -16.30% |

With a hypothetical EUR 10,000 portfolio and EUR 3 per material trade plus 10 basis points turnover cost, native net CAGR was -0.70%, 0.19% and 0.87%, respectively. These costs/frequency are experimental assumptions, not a universal broker tariff or a claim that HA mandates those trades.

Limitations: selected surviving funds; contemporary revised adjusted history; 2026-written model evaluated retrospectively; not an untouched model-selection holdout; continuous rather than discrete/broker execution; zero nominal interest on uninvested cash; no tax, actual bid/ask or slippage model. Neither model is established universally superior by this small-universe diagnostic.

## 6. Prediction diagnostic

Rolling ridge regression: 156 training weeks, standardized predictors, alpha=10 fixed before inspecting results, trailing 4/13/52-week returns and 13-week volatility; next-week return withheld from its own training set. Total 2,770 forecasts, 554 calendar weeks.

Pooled mean squared error: ridge 0.0002062250; rolling mean 0.0001938305; zero return 0.0001945490. Out-of-sample R-squared versus rolling mean: -6.39% (ridge was worse on aggregate return MSE).

Mean calendar-week paired loss reduction: -0.00001239449. A 2,000-resample, 13-week block bootstrap gave a 95% interval [-0.00002409860, -0.00000387854], favoring the mean baseline on this metric under the experiment assumptions.

Probability-of-positive-return Brier score was somewhat better for ridge: 0.2038 versus 0.2123. The baseline's nominal 90% normal-return interval covered 88.05% of observations. This was not a conformal-interval test. Asset-level results varied, so post-hoc winners must not be promoted without fresh evaluation.

Do not deploy this prototype as a buy-selection forecast. A failed prototype does not imply forecasting is impossible. Research and further out-of-sample tests should focus on calibrated return/risk distributions, not unsupported precise price targets.

## Research-grounded recommendations

1. Preserve approved eligibility through lot conversion, AI and final output. Explicitly recheck original user constraints.
2. Verify ISIN/share class, exchange, Acc/Dist, tradability, leverage, duration, credit and currency exposure from trustworthy instrument metadata. Unknown material structure should lead to abstention for conservative profiles.
3. Include actual fees/spreads/FX costs in affordability and optimize complete post-trade holdings. Cash/no trade and accumulating contributions must remain options.
4. Compare shrinkage, conservative scenario losses and uncertainty margins on aligned data. Do not confuse in-sample weekly volatility with capital preservation over the user's horizon.
5. Report solver completeness honestly. Choosing maximum deployment is not a general definition of safest investment.
6. Require chronological point-in-time validation, simple baselines, realistic costs, multiple-testing controls, stress tests and fresh forward evaluation before promoting predictions. Prices, dividends, current fund identity and delistings must not introduce look-ahead or survivor bias.

Published research supports methods under assumptions, not the exact application's constants or guaranteed future outcomes. Core references:

- Ledoit & Wolf (2004), covariance shrinkage, Journal of Multivariate Analysis. https://doi.org/10.1016/S0047-259X(03)00096-4
- Rockafellar & Uryasev (2000), CVaR optimization, Journal of Risk. https://doi.org/10.21314/JOR.2000.038
- Lim, Shanthikumar & Vahn (2011), CVaR estimation fragility, Operations Research Letters. https://doi.org/10.1016/j.orl.2011.03.004
- Boyd et al. (2017), trading with risk and transaction costs; the paper does not supply forecasts. https://web.stanford.edu/~boyd/papers/cvx_portfolio.html
- DeMiguel, Garlappi & Uppal (2009), importance of simple out-of-sample benchmarks. https://doi.org/10.1093/rfs/hhm075
- Moskowitz, Ooi & Pedersen (2012), time-series momentum in futures; not direct validation of this ETF scorer. https://doi.org/10.1016/j.jfineco.2011.11.003
- Gneiting & Raftery (2007), proper probabilistic scoring. https://doi.org/10.1198/016214506000001437
- Gibbs & Candes (2021), adaptive conformal long-run coverage under distribution shift; not next-trade/profit guarantees. https://proceedings.neurips.cc/paper_files/paper/2021/hash/0d441de75945e5acbc865406fc9a2559-Abstract.html
- Campbell & Thompson (2008), evidence of restricted return predictability in a particular setting. https://doi.org/10.1093/rfs/hhm055
- Bailey et al. (2017), probability of backtest overfitting. https://doi.org/10.21314/JCF.2016.322

## Deployment status

Audit only. No production fixes, model tuning, prediction features or new recommendation thresholds were deployed. The smallest tested intervention is an in-memory eligibility guard; wider data, cost and risk improvements still require separate implementation and validation.
