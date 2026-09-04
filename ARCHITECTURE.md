# Architecture

## Security boundary

The custom panel communicates only through Home Assistant's authenticated WebSocket connection. Each command reads `connection.user.id`; portfolio APIs do not accept a user identifier from the browser.

## Data flow

```text
HA user browser
   │ authenticated HA WebSocket
   ▼
investment/websocket.py
   ▼
InvestmentManager ───────► TTL caches
   │                         │
   ├────► per-user Store     ├────► Yahoo adapter
   │                         ├────► Kraken adapter
   │                         ├────► Frankfurter adapter
   │                         └────► Stooq fallback
   ▼
portfolio/category/holding payloads
```

## Provider contract

Every provider implements three operations:

- `async_search(query, base_currency)`
- `async_quote(provider_id)`
- `async_history(provider_id, period)`

This keeps provider-specific symbol formats and response parsing out of portfolio logic. Search is currency-scoped end-to-end: providers receive the user's selected portfolio currency, the manager applies a final quote-currency gate, and the frontend applies a defensive filter before rendering results. Crypto discovery can resolve human names/aliases (for example `Bitcoin Cash`) and retarget a provider discovery symbol such as `BCH-USD` to a verified `BCH-EUR` instrument before the strict currency filter is applied.

## Cache policy

- latest quote: ~60 seconds
- search: ~5 minutes
- history: ~15 minutes
- FX conversion: ~5 minutes

The cache is instance-wide for public market data but portfolio summaries remain keyed by Home Assistant user ID.

## Cost-basis model

Each holding can store `average_buy_price` in the instrument quote currency. New purchases append transaction records containing gross quantity, net quantity received, optional asset-denominated fee quantity/percentage, investment principal, detailed cash transaction costs, total cash transaction costs, all-in cash total, currency and an optional note. The add API accepts either unit price or investment total, either transaction-cost total or all-in total, and linked gross/net/withheld-asset fields; the backend reconciles the missing values so callers cannot create contradictory accounting data.

Cash transaction costs are intentionally separate from principal. Holding P/L uses principal + cash transaction costs; category and portfolio summaries aggregate both separately and expose an all-in total. Per-unit P/L allocates cash transaction costs across the current net quantity so fees are reflected consistently.

An asset-denominated fee is modeled differently: it reduces the quantity actually received but does **not** create a second cash expense. For example, gross `0.04554 BCH` and net `0.0450846 BCH` imply `0.0004554 BCH` withheld (1%). The holding increases by the net quantity, while cost basis preserves the full purchase principal. The withheld units and their purchase-value equivalent are reported separately for transparency, but that equivalent value is marked as already included in principal to avoid double-counting.

Currency amounts are normalized/displayed to two decimal places. Asset quantities retain meaningful fractional precision and are never globally rounded to two decimals.

Each user can also store `category_expenses` in their selected portfolio base currency. A manual category expense is authoritative for the category's investment principal only; recorded transaction costs are then added separately. Otherwise category principal is calculated from holdings only when all constituent holdings have a known buy price. Grand P/L is emitted only when every category has a complete principal cost basis.

## Frontend focus model

The panel captures the active Shadow DOM control and text selection before a render and restores it after rebinding. Click/tap trend selection is separately pinned in component state, so market refreshes and UI rerenders do not silently move the user's active selection.
