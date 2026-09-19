# HA Investment

💙 **Enjoying this hobby project? [Send a voluntary thank-you via Ko-fi](https://ko-fi.com/chreece).**


A **free, multi-user investment portfolio for Home Assistant**. Search and add stocks, ETFs, funds, crypto, indices, commodities/futures and FX instruments, then track current value, today's move, cost-basis profit/loss and historical trends directly inside Home Assistant.

> **No paid API subscription is required.** The default provider stack uses public/no-key endpoints and caches responses to reduce load. Market-data availability and delay depend on the source and exchange.

## Highlights

- **Private portfolio per Home Assistant user** — the authenticated WebSocket connection determines the owner; the browser cannot request another user's portfolio by ID.
- **Global asset search** by company/fund name, ticker/symbol, crypto pair or provider ID, strictly scoped to the user's selected portfolio/trading currency so EUR results do not mix in USD/GBP/AUD/BTC-quoted instruments. Crypto name searches are resolved to the selected quote currency instead of being discarded when a provider initially returns a USD discovery row.
- **Stocks, ETFs, funds, crypto, indices, commodities/futures and FX** through a modular provider layer.
- **Portfolio total + category totals** with today's absolute/percentage movement.
- **Profit/loss at every level**: per unit, per holding, per category and grand portfolio total.
- **Lot-based BUY/SELL ledger** with exact transaction timestamps, FIFO cost-basis consumption, and separate realized/unrealized P/L.
- Assign a **holding provider/location per BUY lot** (broker, exchange, wallet, bank or app). The same asset can be split across multiple providers without duplicating the holding.
- When adding a purchase, enter either **buy price per unit** or the **total invested amount**; quantity links the two and the missing value is calculated automatically.
- Record **platform/broker, bank/payment, exchange/network, tax and other transaction costs** separately from the money invested in the asset.
- Record **asset-denominated fees** when a provider withholds part of the purchased asset: gross quantity, net quantity received, withheld units and fee percentage are linked automatically.
- Enter either the **total extra costs** or the **total paid including costs**; HA Investment calculates the missing amount and reconciles it with the detailed fee breakdown.
- Enter a **manual total amount invested per category** when required. Manual category totals override calculated holding principal without double-counting transaction costs.
- **Portfolio analytics** with allocation views by category, holding provider and trading currency, plus monthly buy outflow, sell inflow and net cash flow.
- **Metric-aware history** for portfolio/category/holding value plus holding price, units, realized P/L and unrealized P/L. Compact card visuals use the appropriate chart style for each metric.
- **Hover or tap histories** for individual holdings, category totals and the full portfolio. A clicked/tapped history stays pinned and selected until closed.
- History periods: **1D, 1W, 1M, 3M, 1Y and 5Y** where supported by the selected metric.
- **Edit quantity directly on each card** with − / numeric input / +.
- Duplicate additions automatically increase the existing holding instead of creating duplicate cards.
- Per-user **base currency** with historical FX conversion where available.
- Responsive desktop/tablet/mobile UI using Home Assistant theme variables.
- Automatic visible-page refresh around once per minute plus manual refresh.
- **28 languages** included in setup translations; the investment panel includes localized UI strings for major languages including **Greek**.
- No globally visible portfolio sensor entities by default, avoiding accidental cross-user financial-data exposure.

## Free provider stack

| Source | Used for | Key / subscription | Notes |
|---|---|---:|---|
| Yahoo Finance endpoints | Broad global search, stocks, ETFs, funds, indices, futures/commodities, many crypto pairs, history | None | Unofficial endpoint; isolated behind a provider adapter so it can be replaced easily. |
| Kraken public API | Crypto search, latest crypto quotes and OHLC | None | Dedicated 24/7 crypto market source. |
| Frankfurter | FX search, latest/historical currency conversion | None | Open-source API aggregating central-bank/reference-rate sources. |
| Stooq | Conservative fallback for supported US equity/ETF history | None | Used only when a safe symbol mapping is known. |

The integration never requires a commercial market-data key. "Latest quote" is used in the UI rather than claiming exchange-grade real-time data when a source may be delayed.

## Installation

### HACS custom repository

1. Add this repository to HACS as an **Integration** custom repository.
2. Install **HA Investment**.
3. Restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration → HA Investment**.
5. Open **Investments** in the sidebar.

### Manual

Copy:

```text
custom_components/investment
```

into your Home Assistant configuration's `custom_components` directory, restart Home Assistant, then add **HA Investment** from Devices & services.

## How it works

1. The panel searches multiple no-key providers through Home Assistant's backend and returns only instruments quoted in the user's selected portfolio currency.
2. BUY and SELL transactions are stored in Home Assistant's private `.storage` area and reduced through a canonical ledger. BUY lots retain their own provider/location, quantities, quoted price, cash principal, fees and exact timestamp.
3. Quotes are fetched server-side, cached and converted into the user's selected portfolio currency.
4. The UI requests only the logged-in user's portfolio through authenticated Home Assistant WebSockets.
5. History requests are lazy: market history is downloaded only when a user opens or previews a metric that needs it; ledger-derived histories (for example units or realized P/L) use the stored transaction timeline directly.

## Portfolio cards

Each holding shows:

- symbol and full name
- asset category and exchange/source
- current unit quote
- current position value
- today's change and percentage
- current profit/loss when average buy price is configured
- direct quantity stepper and numerical editing
- buy price per unit editor, unit P/L and total holding P/L
- calculated investment principal, transaction costs and all-in amount spent
- transaction-cost breakdown when available
- remaining units grouped by holding provider/location when BUY lots are split across providers
- compact metric-aware historical visualizations, with detailed price/value/units/realized/unrealized views available from the holding
- market-data source and delay flag where known

## Cost basis and profit/loss

Each purchase can be entered in whichever form matches the broker receipt:

- **Gross quantity + price per unit** → HA Investment calculates the investment total.
- **Gross quantity + investment total** → HA Investment calculates the effective trade price per unit.
- If a receipt explicitly provides **both** a quoted unit price and an actual cash investment total, both are preserved even when they differ. This supports split-fee settlement models such as Bitcoin.de without falsifying either the quoted trade price or the cash cost basis.
- **Gross quantity + net quantity received** → HA Investment calculates the asset units withheld as commission and its percentage.
- **Gross quantity + withheld units or fee percentage** → HA Investment calculates the net quantity actually received.
- **Investment total + extra costs** → HA Investment calculates the total paid including costs.
- **Investment total + total paid including costs** → HA Investment calculates the extra costs.

Detailed fees can be split into platform/broker, bank/payment, exchange/network, taxes/duties and other costs. If the user supplies a larger total fee amount than the detailed breakdown, the remainder is preserved as an unallocated `other` cost so the transaction still reconciles exactly.

Investment principal and cash transaction costs remain separate in storage and in the UI. Asset-denominated fees are also tracked separately, but their purchase value is **informational and included in principal** rather than added again as a cash expense. Holdings increase by the **net quantity actually received**, while P/L uses the full investment principal plus true cash transaction costs. Currency displays are rounded to **2 decimal places**; asset quantities keep their meaningful fractional precision.

For split-fee receipts, the quoted unit price is descriptive market/trade information while `investment_total` is the actual cash principal used for cost basis. For example, a Bitcoin.de purchase can retain `2030.00 EUR/BCH` as the quoted price and `91.98 EUR` as cash paid for `0.0450846 BCH` net received. The effective cost used for per-unit P/L is derived from actual cash principal divided by net units, not by forcing the quoted price to match the settlement amount.

The Add Investment dialog keeps a live draft of every entered receipt field. Background portfolio polling is paused while the dialog is open, and any unavoidable frontend rerender reconstructs the dialog from that draft rather than from default values. This prevents a long-running entry from silently reverting to quantity `1` or losing its cost basis.

## Transactions, lots and holding providers

Every BUY is preserved as its own lot rather than being flattened into one anonymous quantity. A lot keeps its transaction time, gross/net units, quoted price, actual cash principal, costs, asset-denominated fees and optional **holding provider/location**. This allows one asset to be held simultaneously at multiple brokers, exchanges, wallets or other locations.

SELL transactions are applied through the canonical ledger using FIFO lot consumption. Realized P/L is produced from the consumed lots, while unrealized P/L and remaining provider balances are derived from the lots that are still open. Provider balances are therefore not a second editable source of truth: they come from the transaction ledger and remain consistent after partial sells.

The historical units and realized-P/L series use the exact stored transaction timestamps. Market-price/value series continue to use provider market history, so ledger events and market movements are kept conceptually separate.

Each category also has an optional manual **invested amount** input in the portfolio base currency. When set, that manual category principal becomes authoritative for category and grand-total P/L; transaction costs are still added separately. If principal information is incomplete, P/L is shown as unknown instead of presenting a misleading value.

## Focus and selection behavior

The frontend preserves the active input and text selection across its Shadow DOM rerenders. Search input, numeric editors and trend-period controls therefore retain focus while asynchronous data loads. Clicking/tapping a portfolio, category or holding pins its trend and applies a persistent selected state; hovering remains a temporary preview when no trend is pinned. Hover previews use a 1-second stationary hover-intent delay, disappear immediately when the pointer leaves the source card, and are suppressed whenever Search or another editable control is being approached or used.

## Trend aggregation

Historical holding values are multiplied by the current stored quantity and converted into the user's portfolio currency. Category and portfolio trends aggregate the constituent holdings with time-bucket forward filling so different market schedules can coexist in one trend.

FX conversion uses historical Frankfurter data where available. If a historical currency series cannot be obtained, the integration falls back to the latest available conversion rate rather than failing the entire chart.

## Portfolio analytics

The dashboard includes a visual analytics area for the current portfolio mix and recent cash activity:

- allocation by **asset category**
- allocation by **holding provider/location**
- allocation by **trading currency**
- monthly **BUY outflow**, **SELL inflow** and **net cash flow**
- compact 30-day previews on supported cards

Detailed history is metric-aware rather than forcing every value into the same chart. Price/value use continuous market series, units use a step timeline, and realized/unrealized P/L use signed profit/loss presentation so the visual encoding matches what changed.

## Privacy model

Portfolio state is stored under a Home Assistant user ID in `.storage/investment.portfolios`. WebSocket handlers derive that ID exclusively from the authenticated HA connection. There is intentionally no `user_id` parameter in any public panel command.

This repository does **not** create portfolio-value entities by default, because ordinary entity visibility could expose household financial values across users. An opt-in admin-controlled entity export can be added later without weakening the default model.

## Market-data limitations

Free market data is not identical to a licensed professional exchange feed. Depending on instrument and venue, data can be delayed, markets can be closed, symbols can change, and an upstream service can throttle or alter an unofficial endpoint. HA Investment therefore:

- labels prices as **latest quote**;
- caches aggressively enough for a home dashboard;
- isolates providers so alternatives can be added without rewriting the UI;
- lets one source fail without making search completely unusable;
- does not place trades and never stores brokerage credentials.

## Repository layout

```text
custom_components/investment/
├── __init__.py              # integration + custom panel registration
├── accounting.py            # cost-basis and P/L helpers
├── config_flow.py           # one-instance setup
├── const.py
├── currency.py              # quote/base-currency normalization
├── ledger.py                # canonical BUY/SELL lots, FIFO and provider balances
├── manager.py               # portfolio valuation, aggregation, cache/fallbacks
├── models.py
├── search.py                # provider search coordination
├── sensor.py                # core integration sensor platform
├── storage.py               # private per-user .storage data
├── websocket.py             # authenticated panel API
├── providers/
│   ├── base.py
│   ├── yahoo.py
│   ├── kraken.py
│   ├── frankfurter.py
│   └── stooq.py
├── translations/            # backend setup translations
└── www/
    └── investment-panel.js  # responsive localized Home Assistant panel
```

## Roadmap

- dividends/distributions and cash positions
- allocation targets and rebalancing views
- transaction CSV import/export
- optional broker read-only imports without making them a requirement
- additional free market-data adapters and automatic provider health scoring
- opt-in admin-controlled HA entities for automations
- alerts for user-defined price/portfolio thresholds

## Development checks

```bash
python -m compileall custom_components/investment
for file in custom_components/investment/www/*.js; do node --check "$file"; done
```

GitHub validation workflows for HACS and Hassfest are included.

## Disclaimer

HA Investment is a portfolio display and tracking tool, not a broker, trading system or source of investment advice. Verify important prices with the relevant exchange/broker before making financial decisions.

## License

MIT

## ❤️ Voluntary support

This is a private hobby project maintained in my free time and provided independently of contributions.

If you enjoy the project and would like to send me a voluntary personal thank-you, you can use **[Ko-fi](https://ko-fi.com/chreece)**.

Contributions are completely optional and do **not** buy or guarantee features, support, development work, early access, priority, or any other service. This is not a charitable donation and no donation receipt is issued.
