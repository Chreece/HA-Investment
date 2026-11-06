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

## Remaining ownership-filter limitations

Normal ownership checks now use a fresh local transaction ledger, but malformed
FIFO records can still trigger the existing fallback to a persisted aggregate
quantity. This audit does not establish verified ownership for corrupted
records. A future change should represent that uncertainty explicitly rather
than silently treating a fallback quantity as verified.

Owned-instrument membership is captured when analysis starts. In contribution-only
scope, a concurrent Buy or Sell during a long analysis does not yet trigger a
final per-candidate ownership recheck, so the exclusion filter can become stale.
A targeted recheck before displaying final purchases would address this without
bringing old-holdings market values or full-portfolio risk back into the decision.
These are ownership-filter consistency limitations, not evidence that the new
contribution's risk limits protect against all future losses.

## Verification boundary

The regression suite exercises Python and the real JavaScript methods in
LinkeDOM, including masked text and accessibility attributes. Syntax compilation
and whitespace checks accompany it. Full Chromium rendering was blocked by the
execution environment's socket/approval restrictions, so no visual-browser or
live Home Assistant deployment result is claimed.

The performance improvement is the removal of duplicate return-map work and
unnecessary quote/FX enrichment. Local timing was sensitive to concurrent work;
no end-to-end percentage speedup is asserted.
