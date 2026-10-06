# Indication input evidence — second improvement batch

Base: `indications` at `6af536ac357f12bab07443ea85b08ce1bf4924dd`, including
the first safety batch in PR #11. This document supplements
[the first batch](SAFETY_GATES_V1.md).

Live input contract: `source-time-and-exact-identity-v1`.
Identity policy: `exact-provider-and-issuer-listing-v1`.
Freshness policy: `indication_source_time_v1`.

## Behavior

The live manager must establish the requested instrument's identity and the
dates of its actual source observations before it can allocate. It sanitizes
candidate requests, fetches independent provider metadata, checks the quote and
both signal and risk histories, and checks any required current and historical
FX. Source errors produce a visible rejected result with typed reasons and zero
allocation. A successful network request or a recent cache insertion does not
make an old observation fresh.

Evidence is checked after all of a candidate's source requests and again at a
common cutoff after the full candidate set finishes. The response records this
cutoff as `evidence_as_of`; `generated_at` is the response construction time and
may be later, particularly with an AI review. Scoring, weight reconstruction,
whole-unit redistribution and the final AI ceiling cannot restore a candidate
whose evidence failed. Missing evidence fields cannot be supplied by a scoring
scaffold, caller-provided `verified` flags or a cached classification result.

History responses must independently match the quote's provider listing,
venue, native currency unit and product type. An alternative adjusted source
requires the exact symbol and an explicitly equivalent venue; a suffix or
similar fund name is not a mapping. Pounds and pence remain different units in
both validation and cache keys. Indication caches use new namespaces so older
generic quote/history fallbacks cannot masquerade as validated evidence.

## Explicit source-time policy

These thresholds are operational policies, not empirically calibrated
probabilities of accuracy or future investment safety. Boundary ages are
inclusive; any timestamp later than the analysis cutoff is rejected, including
subsecond differences.

| Evidence | Maximum age / gap |
| --- | --- |
| Exchange-traded quote or daily history | 72 elapsed UTC weekday hours, with a seven-calendar-day hard cap |
| Current FX observation or daily FX history | Same weekday/calendar limits |
| Crypto quote | 24 calendar hours |
| Crypto daily history | 48 calendar hours |
| Weekly signal history | 14 calendar days |

Risk requests use a separate raw daily feed, subject to the daily limits. The
history gate examines every supplied observation for finite positive
values, valid timestamps, uniqueness and chronological order. It also checks
the last interval so a lone new observation cannot refresh a long-stale tail.
It rejects missing, future, boolean and nonfinite input. The unchanged minimum
of 52 aligned weekly risk returns is a separate downstream requirement.

Weekly risk returns are constructed from daily prices after any FX conversion.
Each daily point retains its provider-established trading-session date through
caches and conversion; weekly grouping uses this date, while freshness and
ordering retain the original timestamp. Missing or invalid session dates cannot
fall back to UTC grouping in the live risk path. This avoids pairing Xetra's
Monday-midnight weekly label (Sunday in UTC) with a different economic week
from a US venue. Long-horizon signal scoring retains its existing weekly feed.

Five years is the requested risk-history horizon, not a fabricated coverage
claim. [Kraken's bounded daily OHLC feed](https://docs.kraken.com/api-reference/market-data/get-ohlc-data)
can return at most 720 observations,
so its actual coverage is shorter. The response reports retained daily
observations and usable weekly returns, and the unchanged 52 aligned-return
minimum still applies. No other venue's data extends that coverage.

Historical conversion requests ungrouped daily FX. Each asset timestamp uses
an observed FX rate at or before that timestamp, subject to the same age limits.
Only leading asset observations before the first available FX observation may
be cropped; their count is retained. Interior or trailing coverage failures
reject conversion. Empty FX cannot become a rate of one, and a later rate
cannot be backfilled into earlier dates. Current FX caches retain the provider's
actual observation date rather than a scalar rate alone.

Yahoo fallback closes retain the timestamp at the same array index. Alpha
Vantage quotes retain the reported trading day. Twelve Data no longer replaces
missing source times with fetch time. Kraken uses the price and timestamp from
the same recent trade for the exact pair; missing trade-time evidence prevents
an indication allocation.

## Exact fund identity

Fund eligibility requires both observed provider metadata and a reviewed issuer
record for the exact listing. The initial catalog covers seven existing EUR
discovery seeds; other ETFs and funds abstain until their share class is
verified. Provider-confirmed stocks and crypto pairs follow their direct
identity checks. Native Alpha Vantage `GLOBAL_QUOTE` responses do not establish
exchange, product type and currency independently of the request, so those
candidates currently abstain under this live contract.

| Yahoo listing | ISIN | Share class | Existing economic sleeve |
| --- | --- | --- | --- |
| XEON.DE | LU0290358497 | EUR 1C, synthetic swap | Cash-like |
| CEMK.DE | IE000WV38GP5 | EUR Accumulating | Cash-like |
| VAGF.DE | IE00BG47KH54 | EUR Hedged Accumulating | Aggregate bond |
| VGGF.DE | IE000B1A2798 | EUR Hedged Accumulating | Government bond |
| SXR8.DE | IE00B5BMR087 | USD Accumulating | Broad equity |
| EUNL.DE | IE00B4L5Y983 | USD Accumulating | Broad equity |
| VWCE.DE | IE00BK5BQT80 | USD Accumulating | Broad equity |

Every listed trading currency is EUR. Share-class currency and currency hedging
remain separate facts. The sleeve names preserve the existing model taxonomy;
in particular, "cash-like" does not mean a deposit, capital protection or zero
credit/counterparty risk.

Issuer records include primary source links, a review date of 2026-10-06, and a
180-day review policy. They abstain after 2027-04-04 until the issuer facts are
reviewed again. A review must verify the exact ISIN, share class, trading venue,
currency, structure and inception dates; changing only the review date is not
a review. No classification percentage is invented from these records.

Prices before the exact listing's inception, or the share-class inception when
no distinct listing date is recorded, are excluded before scoring and risk
return construction. This also excludes a first partial week labelled with the
Monday before a midweek launch. The 52-return minimum applies to retained
observations; history from an older distributing or otherwise different share
class cannot extend it.

Primary issuer references:

- [XEON / DWS factsheet](https://etf.dws.com/download/asset/226c6aa2-681e-4c7f-864d-0d717f8718d4)
- [CEMK / iShares](https://www.ishares.com/uk/individual/en/products/345319/?siteEntryPassthrough=true&switchLocale=y)
- [VAGF / Vanguard](https://www.vanguard.co.uk/uk-fund-directory/product/etf/bond/9443/global-aggregate-bond-ucits)
- [VGGF / Vanguard](https://www.vanguard.co.uk/professional/product/etf/bond/E070/global-government-bond-ucits-etf-eur-hedged-accumulating)
- [SXR8 / iShares](https://www.ishares.com/de/privatanleger/de/produkte/253743/ishares-core-s-p-500-ucits-etf)
- [EUNL / iShares](https://www.ishares.com/de/privatanleger/de/produkte/251882/ishares-msci-world-ucits-etf-acc)
- [VWCE / Vanguard](https://www.vanguard.co.uk/professional/product/etf/equity/9679/ftse-all-world-ucits-etf-usd-accumulating)

## Interface and verification

Verified fund cards display the ISIN, share class, venue and listing currency.
Discovery keeps zero allocations out of recommendation cards while exposing
evidence failures in its exclusions, automatically opened when all candidates
fail. Search and portfolio scopes retain their rejected cards with an
explanation. Missing-data and identity messages are localized in all 28
supported languages. The classification-confidence percentage is removed,
including values from older cached results; the separate historical-data
reliability measure retains its existing meaning.

New behavioral tests cover pure freshness/FX policies, exact issuer identity,
model evidence preservation, real provider/manager/allocation boundaries with
controlled external payloads, and both shipped JavaScript layers using a DOM.
Positive controls continue to allocate; rejection-only tests are insufficient
evidence that the integration is usable. The original 805-test suite remains
part of the full regression run.

Verification on 2026-10-06:

- Full suite: **1,287 passed** (805 retained cases and 482 new cases). Existing
  structural assertions were updated for the separate daily risk request and
  the revised identity/warning UI; behavioral guards were retained.
- New cases: 148 source-time/FX, 104 identity, 39 model-evidence, 66 production
  integration, 21 two-layer DOM UI, 64 session-calendar and 40 provider tests.
- Python compilation, every shipped JavaScript syntax check and
  `git diff --check` passed.
- The unchanged original audit's 300 generated portfolios report zero budget,
  sleeve, explicit-cap, historical-risk, quantity/price, eligibility-resurrection
  or nonfinite-output violations. The calendar change preserves all stable
  outputs of this offline replay.
- The original targeted audit remains **10/13**: cumulative-drawdown enforcement,
  freshness of arbitrary offline return-map keys, and blanket rejection of its
  synthetic high-yield example remain unmet in the legacy offline interface.
  Those probes do not run the live manager's evidence contract. The new live
  identity/freshness gates have separate behavioral coverage.
- An initial live check of seven catalog listings exposed the UTC weekly-label
  problem. The corrected daily risk path then passed identity, freshness,
  session-date and minimum-history checks for all seven listings plus MSFT,
  using 16 actual Yahoo requests from 19:16:49 to 19:17:52 UTC. CEMK retained
  273 daily observations / 56 weekly returns; VGGF retained 386 / 80 after
  excluding two pre-listing observations. The six older listing controls each
  produced 159 weekly returns. Every final map ended with 2026-W39, W40 and W41.
  This was a native-currency source smoke, not a live HA/AI allocation or a
  certification of current FX/market execution.

To run the complete suite:

```sh
python -m pip install pytest
npm install --prefix /tmp/investment-ui-test linkedom@0.18.12 --no-audit --no-fund --ignore-scripts
INVESTMENT_UI_NODE_MODULES=/tmp/investment-ui-test/node_modules python -m pytest -q tests
python -m compileall -q custom_components/investment
```

CI installs the same pinned test-only DOM package and syntax-checks all shipped
JavaScript. Panel asset revision `0.4.0-r60` ensures a new module and custom
element identity after the integration is installed and Home Assistant is
restarted. A branch or PR does not update a running Home Assistant instance.

## Scope and remaining work

Market-score formulas, activation thresholds, sleeve/risk targets, confidence
cutoffs and the aligned-history minimum are unchanged. The first batch's
approved-set and AI ceiling checks remain in force. This batch adds no forecast
or guaranteed-performance claim.

UTC weekday counting is not an exchange-specific holiday calendar. A date-only
FX observation does not establish its exact publication instant, and a daily
asset bar's provider timestamp need not be its exact closing instant. Daily
risk prices remove the weekly opening-label/closing-price mismatch. This batch
prevents use of later labelled FX observations; it does not certify intraday
synchronization of asset and FX closes or a point-in-time backtest.
Historical coverage and adjusted-price provenance also do not prove that a
sample represents future risks or includes all relevant stress periods.

Still pending: broader issuer-catalog coverage and maintenance; fees-inclusive
affordability; full post-trade portfolio risk; horizon/drawdown/stress
uncertainty; solver completeness; and independently validated probabilistic
forecasts. Offline mathematical fixtures without a live evidence contract
remain available for deterministic audit comparison; their passing allocations
do not certify live source freshness or product identity.
