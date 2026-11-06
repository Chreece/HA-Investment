# Indication safety audit — 6 November 2026

## Scope and conclusion

Reviewed baseline `874d051ea75c4e4d4c971eba056da5dbb6101487` on
`indications`: provider evidence, market scoring, risk calculations, cash/lot
projection, final validation, contribution recovery, owned-instrument filtering,
and history privacy. The baseline passed all 2,126 existing tests. Independent
adversarial checks still reproduced the defects below.

The changes strengthen implementation correctness and remove redundant work.
They do not establish superior future investment returns. Market-score weights,
activation thresholds, risk targets, and the V13 signal formulas are unchanged.
Risk still applies to the new contribution. Existing holdings only support the
owned-instrument filter and the explicit current-portfolio candidate source.

## Reproduced defects and corrections

| Finding | Correction and evidence |
| --- | --- |
| History totals, period change, axes and statistics remained readable in incognito | History numeric text uses explicit placeholders, including accessible labels and dynamic hover values. Deliberate per-field reveal keeps the user's timeout/next-interaction setting. Behavioral DOM tests cover scopes, metrics, languages, refresh, zoom and preference changes. |
| Minimum-lot recovery could resurrect a purchase after the core rejected a changed currency or failed final account snapshot | Recovery requires a verified final contribution-only result. It cannot discard a global validation veto. Production-pipeline regression tests run the shipped contribution wrapper, provider parsers, finalizer and recovery, with a controlled initial projection fixture. |
| A raw unit price of 4.914 became a 4.91 principal ceiling, preventing an otherwise affordable whole unit | Preserve the raw one-unit principal. The execution-cost engine remains responsible for conservative cent cash rounding, fees and reserves. A one-cent fee/reserve boundary is tested. |
| A deterministic-AI review could describe the empty plan before minimum-lot recovery | Clear that review when recovery changes the purchases and record why. Full-AI purchase ceilings remain unchanged. |
| A bare/shared ticker alias or provider key could override contradictory instrument or listing evidence | Match with deterministic identifier precedence; reject conflicting global identifiers and conflicting venue/currency evidence in weaker fallbacks. Retain separate holdings under ambiguous aliases. Shared global instrument identity can still match across venue-specific FIGIs. |
| Contribution-only analysis enriched old holdings with market prices/FX, then discarded those values | Read ownership from the fresh local transaction ledger, bypassing cached display quantities. The ordinary dashboard still enriches market values. Tests fail immediately if contribution analysis calls the unrelated portfolio-enrichment path. |
| Each final risk snapshot aligned and validated the same return maps twice | Reuse the already dated, aligned series for the unchanged volatility, empirical expected shortfall and drawdown arithmetic. Independent rational-arithmetic tests cover results, common dates, calendar gaps and invalid inputs. |

Tests use synthetic financial fixtures. No private portfolio data or credentials
are required. Incognito is a presentation feature; this change redacts the new
history values rather than claiming to remove authenticated financial data from
browser/component memory. Existing non-history card blur retains its behavior.

## Scientific interpretation

Research supports investigating trend and momentum, but not automatically the
specific mixture, thresholds or instruments in this implementation. The
[Moskowitz–Ooi–Pedersen study](https://www.aqr.com/Insights/Research/Journal-Article/Time-Series-Momentum)
examines time-series momentum in futures/forwards. That evidence is not a direct
validation of this retail ETF/stock/crypto indication engine. Its normalized
market score and data-reliability score are not probabilities of profit.

Historical expected shortfall is the empirical average of the worst 5% return
mass. With the minimum 52 weekly observations, that mass is only 2.6 observations.
Neither that estimate nor observed maximum drawdown bounds a future loss. The
existing diagnostics retain the sample size, tail mass, calendar coverage,
conditional bootstrap interpretation and missing stress-factor evidence.

The retained [October study](../research/results/portfolio-robustness-20261006-v3/READOUT.md)
has useful chronology and accounting checks, but uses five surviving EUR funds,
one current adjusted-history vintage, and already known historical periods. Its
full-portfolio decision contract also predates the later contribution-only
production change. It therefore cannot certify the current live end-to-end
strategy's prospective performance. Its simple strategic baseline had greater
ending wealth and greater realized risk in the primary example; the result does
not demonstrate dominance by the current score. The additional shrinkage and
volatility-window variants were inactive in that example. No winning parameter
has been selected from those known results for this change.

The [backtest-overfitting research](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)
and [Deflated Sharpe Ratio](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)
explain why trying many alternatives and retaining only the best result inflates
confidence. These methods address particular statistical biases; they cannot
certify future safety. [Trading-cost research](https://www.aqr.com/insights/research/working-paper/trading-costs)
also supports evaluating actual implementation costs. Institutional estimates
are not substituted for a user's confirmed retail fee assumptions here.

## Rules for a future predictive-model change

1. Freeze the candidate definitions, data-vintage policy, current contribution-only
   baseline and all planned alternatives before observing comparison outcomes.
2. Use chronological decisions and next-available execution, with point-in-time
   membership/metadata where available. Explicitly identify survivorship and
   historical-revision limitations when such data are unavailable.
3. Compare against the unchanged algorithm and simple diversified/cash baselines
   using the same universe, cash flows, lot rules, costs and selected risk scope.
4. Report net returns together with drawdown, tail losses, concentration, costs,
   turnover, time in cash and abstentions. More deployed cash is not itself a
   scientific investment-performance objective.
5. Retain every attempted configuration and failed run, assess selection bias
   and neighboring-parameter stability, and reserve later forward observations
   before promoting new predictive parameters.

These are promotion requirements, not a claim that a new prospective study has
already been completed. No automatic trading, new model fitting or personal
financial-data collection is introduced by this audit.

## Second audit round — ownership and final validation

The follow-up starts from merged revision
`fbe003c6458a25ac774cdc1db9df4c7797a0e417`. It closes the two ownership-filter
limitations recorded by the first round and examines the final asynchronous
boundary in the shipped contribution manager.

| Finding | Correction and evidence |
| --- | --- |
| Display FIFO and its aggregate fallback could decide whether an instrument was owned | A bounded, network-free ownership snapshot uses the existing strict transaction-quantity validator. Missing, invalid, future, oversold or ambiguously ordered records remain unknown. Complete sales and fully shared units establish zero personal ownership; every verified positive personal remainder counts as owned. |
| A shared bare ticker with insufficient venue evidence could be treated as proof of nonownership | Exact identity now distinguishes matched, conflicting and ambiguous evidence. Unresolved shared aliases remain candidate-specific uncertainty. Explicit incompatible identifiers/listings remain nonmatches. |
| Ownership could change during provider, AI or preference awaits | Selected candidates are checked against the final local account snapshot. A newly owned excluded candidate or a sold-out portfolio-source candidate produces a clear rerun error. Unrelated holding edits, partial sales that retain ownership and allow-owned analysis remain independent of old-holdings risk. |
| Preference persistence occurred after the core's final currency and market-evidence checks | Persistence now runs before the final account read. The existing raw-source freshness and final purchase gates follow it without further asynchronous work. Regressions cover currency changes, evidence expiry and unavailable final reads across all three analysis modes. |
| Storage normalization could turn an unsupported transaction type into a Buy or reconstruct an explicitly empty ledger from an aggregate | Only absent legacy fields may be supplied. Explicit invalid types, empty/malformed transaction collections and invalid quantity types remain available to strict validation. Real-store regressions exercise normalization, repeat reads and legacy migration. |
| Full AI could lose affordable whole units by rounding their principal before a second discrete check | AI whole-unit ceilings retain raw price precision while the unchanged final cash-cost calculation still enforces fees, reserves and risk. Independent boundary checks cover a request just below one unit and production-path fractional-price examples. |

The ownership filter reports unknown evidence separately from known ownership.
Identifiable corruption affects the relevant candidate; it does not add an
unrelated old holding to the contribution's risk calculation. Current-portfolio
source candidates require verified positive personal units. No failed request
automatically repeats a paid AI review.

Legacy compatibility remains an input trust boundary: a valid numeric position
from an old format with no transaction collection can still be migrated to its
documented legacy Buy, and previously migrated records remain supported. Such
records are user-provided historical data, not independently verified broker
statements. The new checks do not reconstruct missing real-world transactions
or certify the original truth of a user's entries.

The final snapshot describes the account at analysis completion. A later edit
or market change requires a new analysis; these checks do not place or lock
trades. Predictive-model promotion requirements above remain open and unchanged.

## Verification boundary

The regression suite exercises Python and the real JavaScript methods in
LinkeDOM, including masked text and accessibility attributes. Syntax compilation
and whitespace checks accompany it. Full Chromium rendering was blocked by the
execution environment's socket/approval restrictions, so no visual-browser or
live Home Assistant deployment result is claimed.

The performance improvement is the removal of duplicate return-map work and
unnecessary quote/FX enrichment. Local timing was sensitive to concurrent work;
no end-to-end percentage speedup is asserted.
