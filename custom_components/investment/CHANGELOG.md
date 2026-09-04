# Changelog

## 0.2.6

- Fixed transaction-form data loss during the 61-second background portfolio refresh. An open Add Investment dialog is no longer rebuilt by automatic refreshes.
- The complete in-progress purchase is now mirrored into persistent frontend draft state, including gross/net quantities, asset fee, quoted/derived monetary values, detailed costs and transaction note. Any unavoidable rerender restores the exact entered values instead of reverting to defaults.
- Added stale portfolio-request sequencing so an older background response cannot overwrite a newer forced refresh after a transaction is saved.
- Automatic portfolio polling is paused while an Add Investment dialog is open or being submitted, preventing a refresh from replacing controls between pointerdown and submit.
- Added a regression check using the Bitcoin.de BCH receipt values to verify that a reconstructed dialog retains `0.04554` gross, `0.0450846` net, `0.0004554` withheld and `91.98 EUR` investment total.
- Decoupled quoted buy price from actual cash principal when the user explicitly enters both. This correctly represents split-fee settlements such as Bitcoin.de: `2030.00 EUR/BCH` quoted price can coexist with `91.98 EUR` actually paid, while P/L uses the cash principal.
- Editing a holding's quoted buy price no longer rewrites explicit transaction cash principals; legacy transactions without a stored principal still derive one as before.

## 0.2.5

- Fixed the v0.2.4 empty-search regression for name searches under strict portfolio-currency filtering. Kraken now understands common crypto names (for example Bitcoin and Bitcoin Cash), while Yahoo can verify and retarget crypto discovery rows such as BCH-USD to BCH-EUR when EUR is the selected portfolio currency.
- Increased Yahoo discovery breadth and added a per-symbol currency cache so broader currency-aware search does not repeatedly resolve the same instrument on every adjacent keystroke.
- Added asset-denominated/withheld transaction fees: gross quantity bought, net quantity received, units retained by the provider and fee percentage.
- The add-investment form links gross/net/withheld/% fields automatically. The stored holding quantity is the net amount actually received.
- Cost basis now preserves the full transaction principal when units are withheld, so an asset fee increases the effective cost per unit without being double-counted as a separate cash expense.
- Added asset-fee purchase-value reporting per holding, category and portfolio, explicitly marked as included in principal.
- Currency values now render with exactly 2 decimal places. Asset/unit quantities retain fractional precision and are not rounded to 2 decimals.
- Added localized asset-fee labels and validation messages across the panel languages, including Greek and German.
- Added pure accounting/search contract tests, including the Bitcoin.de example 0.04554 BCH gross → 0.0450846 BCH net = 0.0004554 BCH (1%) withheld.

## 0.2.4

- Search results are now strictly scoped to the user-selected portfolio currency. An EUR portfolio will no longer mix in AUD, GBP, USD, BTC, ETH or other quote-currency variants.
- Kraken discovery filters pairs at source by quote currency, so searches such as BCH in an EUR portfolio return BCH/EUR rather than every Kraken BCH pair.
- Frankfurter FX/metal discovery now returns only pairs whose quote side is the selected portfolio currency.
- Yahoo discovery resolves missing trading currencies from chart metadata before accepting a result, avoiding exchange-name guesses and mixed-currency listings.
- Added a final backend currency gate plus a defensive frontend gate so malformed/stale provider results cannot leak a mismatched currency into Search.
- Adding an asset is also validated server-side against the selected portfolio currency, preventing stale/custom clients from bypassing the currency rule.
- Changing portfolio currency now clears the active query and cached result list immediately, preventing results from the previous currency remaining visible.
- Search rows now display an explicit quote-currency badge (for example EUR) so users can immediately verify what they are adding.
- GBP search accepts Yahoo's GBp/GBX quote labels as GBP-family listings for matching while preserving the existing pence-aware valuation behavior.

## 0.2.3

- Added transaction-level cost tracking when a new investment is added: platform/broker, bank/payment, exchange/network, taxes/duties and other costs.
- Added linked **price per unit ↔ investment total** inputs. Enter either value and HA Investment calculates the other from quantity.
- Added linked **extra costs ↔ total including costs** inputs. Enter either value and HA Investment calculates the other automatically.
- Detailed transaction fees are reconciled against the total extra-cost amount; any valid remainder is preserved as an unallocated/other cost instead of being lost.
- Transaction costs are stored separately from investment principal, while holding/category/portfolio P/L uses the true all-in amount spent.
- Added portfolio, category and holding displays for invested principal, extra costs, all-in spent and fee breakdowns.
- Duplicate purchases retain individual transaction-cost records and update the holding's weighted average buy price.
- Transaction amounts use the asset transaction/quote currency and are converted to the user's portfolio currency for aggregate reporting.
- Added localized labels and validation messages for the new cost-entry workflow across all supported panel languages, including Greek.
- Synchronized the frontend cache-buster version with the integration manifest so Home Assistant browsers reliably load the new panel JavaScript after upgrades.

## 0.2.2

- Replaced simple trend hover timing with hover-intent detection: the pointer must remain nearly stationary on the same trend surface for 1 second before a preview opens.
- Moving across the portfolio/category/holding card resets the dwell timer, preventing accidental previews while travelling toward Search or another control.
- Trend previews now close immediately when leaving their source surface; the previous close grace period that could linger over Search was removed.
- Entering the Search region cancels both pending and visible trend previews before click/focus.
- Added a final `:hover` verification before a delayed trend callback may open, preventing stale timers from opening a preview after the pointer has already left.
- Portfolio trend previews are positioned inside the portfolio hero instead of below it, keeping the Search field unobstructed.

## 0.2.1

- Fixed accidental portfolio trend popups when moving the pointer toward the search field.
- Hover trends now require a short intentional dwell, are enabled only on devices with true hover/fine-pointer support, and are pointer-transparent so they cannot block Search or editing controls.
- Search, quantity, buy-price, category-expense, select, and button interactions suppress conflicting trend popups.
- Removed the full panel rerender on every Home Assistant `hass` state assignment; unrelated HA state changes no longer replace investment controls.
- Search typing now updates only the search-results slot, preserving the real input DOM node, caret, text selection, and focus.
- Trend loading/period changes now update only the trend overlay slot instead of rebuilding the whole dashboard.
- Added stale-search result protection so slower old searches cannot replace newer query results.
- Reduced portfolio refresh rendering from two full rebuilds to one when data is already visible.

## 0.2.0

- Added manual total amount-spent input per asset category.
- Added buy-price-per-unit editing directly on every holding card.
- Added P/L per unit, per holding, per category, and grand portfolio P/L with percentages.
- Manual category expenses override calculated holding cost basis to avoid double-counting.
- Added strict incomplete-cost handling: unknown cost basis produces unknown P/L instead of misleading totals.
- Fixed frontend focus loss caused by full Shadow DOM rerenders; active fields and cursor selection are restored.
- Added pinned click/tap selection for portfolio/category/holding trends with explicit close action.
- Added translations for the new accounting/focus labels across all panel languages, including Greek.

## 0.1.0 — 2026-09-02

Initial repository release.

- private per-Home-Assistant-user portfolios
- broad search for stocks, ETFs, funds, crypto, indices, commodities/futures and FX
- free/no-key provider architecture: Yahoo Finance endpoints, Kraken, Frankfurter and Stooq fallback
- latest value, today's move and optional cost-basis P/L
- category totals and allocation percentages
- hover/tap historical trends for holdings, categories and total portfolio
- 1D / 1W / 1M / 3M / 1Y / 5Y trends
- direct − / numeric / + quantity editing
- per-user portfolio currency
- responsive custom Home Assistant sidebar panel
- 28 setup translations and major-language panel localization including Greek
- HACS/Hassfest validation workflows
