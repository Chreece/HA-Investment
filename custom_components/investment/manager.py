"""Portfolio and market-data orchestration."""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import time
from collections import defaultdict
from copy import deepcopy
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt as dt_util

from .accounting import derive_asset_quantities, derive_purchase_principal, derive_purchase_settlement
from .currency import (
    canonical_currency,
    default_settlement_currency,
    fixed_conversion_rate,
    frozen_transaction_rate,
    normalize_currency,
)
from .const import (
    CONF_ALPHA_VANTAGE_API_KEY,
    CONF_ALPHA_VANTAGE_ENTITLEMENT,
    CONF_TWELVE_DATA_API_KEY,
    DEFAULT_ALPHA_VANTAGE_ENTITLEMENT,
    DEFAULT_HISTORY_CACHE_SECONDS,
    DEFAULT_INCOGNITO_REVEAL_SECONDS,
    DEFAULT_QUOTE_CACHE_SECONDS,
    DEFAULT_SEARCH_CACHE_SECONDS,
    DEFAULT_UI_LANGUAGE,
    EXPOSABLE_ENTITY_METRICS,
    MAX_INCOGNITO_REVEAL_SECONDS,
    SIGNAL_ENTITY_EXPOSURE_CHANGED,
    SUPPORTED_UI_LANGUAGES,
    TRANSACTION_COST_TYPES,
)
from .ledger import (
    fifo_summary,
    normalize_shared_allocations,
    normalize_shared_ownership,
    personal_quantity,
    personal_ratio,
    quantity_at,
    shared_quantity,
    transaction_timestamp,
    validate_transaction_date,
)
from .models import HistoryPoint, Quote
from .providers import (
    AlphaVantageProvider,
    FrankfurterProvider,
    KrakenProvider,
    ProviderError,
    StooqProvider,
    TwelveDataProvider,
    YahooProvider,
)
from .providers.base import quote_currency_matches
from .storage import InvestmentStore

_LOGGER = logging.getLogger(__name__)


class TTLCache:
    def __init__(self) -> None:
        self._data: dict[Any, tuple[float, Any]] = {}

    def get(self, key: Any, ttl: int):
        item = self._data.get(key)
        if not item:
            return None
        ts, value = item
        if time.monotonic() - ts > ttl:
            self._data.pop(key, None)
            return None
        return deepcopy(value)

    def set(self, key: Any, value: Any) -> None:
        self._data[key] = (time.monotonic(), deepcopy(value))

    def clear_prefix(self, prefix: tuple) -> None:
        for key in list(self._data):
            if isinstance(key, tuple) and key[: len(prefix)] == prefix:
                self._data.pop(key, None)


class InvestmentManager:
    """Per-instance manager. User privacy is enforced at every public method."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry | None = None) -> None:
        self.hass = hass
        self.entry = entry
        self.store = InvestmentStore(hass)
        self.yahoo = YahooProvider(hass)
        self.kraken = KrakenProvider(hass)
        self.frankfurter = FrankfurterProvider(hass)
        self.stooq = StooqProvider(hass)

        options = dict(entry.options) if entry is not None else {}
        self.paid_providers: list[Any] = []
        twelve_key = str(options.get(CONF_TWELVE_DATA_API_KEY) or "").strip()
        if twelve_key:
            self.paid_providers.append(TwelveDataProvider(hass, twelve_key))
        alpha_key = str(options.get(CONF_ALPHA_VANTAGE_API_KEY) or "").strip()
        if alpha_key:
            self.paid_providers.append(
                AlphaVantageProvider(
                    hass,
                    alpha_key,
                    str(
                        options.get(
                            CONF_ALPHA_VANTAGE_ENTITLEMENT,
                            DEFAULT_ALPHA_VANTAGE_ENTITLEMENT,
                        )
                    ),
                )
            )

        self.providers = {
            self.yahoo.provider_id: self.yahoo,
            self.kraken.provider_id: self.kraken,
            self.frankfurter.provider_id: self.frankfurter,
            **{provider.provider_id: provider for provider in self.paid_providers},
        }
        # Commercial/API-key sources are intentionally first. The existing no-key
        # providers remain available so a subscription outage never makes Search
        # unusable and users are never forced to buy market data.
        self.search_providers = [
            *self.paid_providers,
            self.kraken,
            self.frankfurter,
            self.yahoo,
        ]
        self._search_rank = {
            provider.provider_id: index for index, provider in enumerate(self.search_providers)
        }
        self._cache = TTLCache()
        self._network_sem = asyncio.Semaphore(6)

    async def async_initialize(self) -> None:
        await self.store.async_load()

    @staticmethod
    def _validated_transaction_date(raw: str | None) -> str | None:
        """Validate an optional historical transaction date in HA local time."""
        return validate_transaction_date(raw, today=dt_util.now().date())

    async def async_search(self, user_id: str, query: str, currency: str | None = None) -> list[dict[str, Any]]:
        query = query.strip()
        if len(query) < 1:
            return []
        user = await self.store.async_user(user_id)
        base = str(user.get("base_currency", "EUR")).upper()
        search_currency = str(currency or base).upper()
        if len(search_currency) != 3 or not search_currency.isalpha():
            raise ValueError("Trading currency must be a 3-letter code")
        cache_key = ("search", search_currency, query.casefold())
        cached = self._cache.get(cache_key, DEFAULT_SEARCH_CACHE_SECONDS)
        if cached is not None:
            return cached

        async def run(provider):
            try:
                async with self._network_sem:
                    return await provider.async_search(query, search_currency)
            except Exception as err:  # one source failing must not break discovery
                _LOGGER.debug("Search provider %s failed: %s", provider.provider_id, err)
                return []

        chunks = await asyncio.gather(*(run(provider) for provider in self.search_providers))
        raw_results: list[dict[str, Any]] = []
        for chunk in chunks:
            raw_results.extend(item.as_dict() for item in chunk)
        # API-key providers are preferred when configured; free providers remain
        # deterministic fallbacks and still participate in discovery.
        raw_results.sort(
            key=lambda x: (
                self._search_rank.get(str(x.get("provider") or ""), 99),
                x["symbol"],
            )
        )
        seen: set[tuple[str, str]] = set()
        results: list[dict[str, Any]] = []
        for item in raw_results:
            # Provider filtering is intentional but this is the final privacy-
            # neutral correctness gate: search results must be quoted in the
            # explicitly selected trading/search currency. Unknown or mismatched
            # currencies are excluded rather than silently mixed in Search.
            if not quote_currency_matches(item.get("currency"), search_currency):
                continue
            symbol = str(item["symbol"]).upper()
            category = item.get("category") or "other"
            if category == "crypto":
                symbol = symbol.replace("-", "/")
            elif category == "fx" and symbol.endswith("=X") and len(symbol) >= 8:
                compact = symbol[:-2]
                if len(compact) == 6:
                    symbol = f"{compact[:3]}/{compact[3:]}"
            canonical = (category, symbol)
            if canonical in seen:
                continue
            seen.add(canonical)
            results.append(item)
            if len(results) >= 30:
                break
        self._cache.set(cache_key, results)
        return results

    async def _fx_rate_on_date(
        self, from_currency: str, to_currency: str, on_date: str | None
    ) -> tuple[float, str | None]:
        """Return current/historical FX, preserving GBp/GBX scaling."""
        source, source_scale = self._normalize_currency(from_currency)
        target, target_scale = self._normalize_currency(to_currency)
        if source == target:
            return source_scale / target_scale, on_date
        key = ("fx_date", source, target, on_date or "latest")
        cached = self._cache.get(key, 86400 if on_date else 300)
        if cached is not None:
            rate, rate_date = cached
            return float(rate) * source_scale / target_scale, rate_date
        rate, rate_date = await self.frankfurter.async_rate(source, target, on_date=on_date)
        self._cache.set(key, (rate, rate_date))
        return float(rate) * source_scale / target_scale, rate_date

    async def _resolve_fx_leg(
        self, source_currency: str, target_currency: str, on_date: str,
        explicit_rate: float | None, *, label: str,
    ) -> tuple[float, str | None, str]:
        """Resolve one transaction FX leg with fixed conversions taking precedence."""
        fixed = fixed_conversion_rate(source_currency, target_currency)
        if fixed is not None:
            if explicit_rate is not None:
                supplied = float(explicit_rate)
                if not math.isfinite(supplied) or supplied <= 0:
                    raise ValueError(f"{label} must be greater than zero")
                if not math.isclose(supplied, fixed, rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError(f"{label} is fixed at {fixed:g} for {source_currency} to {target_currency}")
            return fixed, on_date, "identity" if math.isclose(fixed, 1.0) else "fixed"
        if explicit_rate is not None:
            supplied = float(explicit_rate)
            if not math.isfinite(supplied) or supplied <= 0:
                raise ValueError(f"{label} must be greater than zero")
            return supplied, on_date, "manual"
        rate, rate_date = await self._fx_rate_on_date(source_currency, target_currency, on_date)
        return rate, rate_date, "Frankfurter"

    async def async_fx_rate(
        self, user_id: str, from_currency: str, *, to_currency: str | None = None, on_date: str | None = None
    ) -> dict[str, Any]:
        """Expose free current/historical FX for the transaction editor."""
        user = await self.store.async_user(user_id)
        target = self._canonical_currency(to_currency or user.get("base_currency") or "EUR")
        source = self._canonical_currency(from_currency)
        if on_date:
            self._validated_transaction_date(on_date)
        fixed = fixed_conversion_rate(source, target)
        if fixed is not None:
            return {
                "from_currency": source, "to_currency": target, "rate": fixed,
                "date": on_date, "source": "identity" if math.isclose(fixed, 1.0) else "fixed",
            }
        rate, rate_date = await self._fx_rate_on_date(source, target, on_date)
        return {"from_currency": source, "to_currency": target, "rate": rate, "date": rate_date, "source": "Frankfurter"}

    async def _transaction_fx_rate(
        self, tx: dict[str, Any], source_currency: str, target_currency: str
    ) -> float:
        """Convert a transaction amount using frozen legs before historical FX."""
        source = self._canonical_currency(source_currency)
        target = self._canonical_currency(target_currency)
        trade_currency = self._canonical_currency(tx.get("transaction_currency") or source)
        settlement_currency = self._canonical_currency(
            tx.get("settlement_currency") or tx.get("fee_currency") or trade_currency
        )
        on_date = str(tx.get("fx_date") or tx.get("date") or "") or None

        frozen = frozen_transaction_rate(tx, source, target)
        if frozen is not None:
            return frozen

        # If a broker-specific trade->settlement rate was stored, keep that
        # first leg even when the user later changes the portfolio/reporting
        # currency. Only the settlement->new-target leg comes from market FX.
        if source == trade_currency and tx.get("trade_fx_rate") is not None:
            settlement_to_target, _ = await self._fx_rate_on_date(
                settlement_currency, target, on_date
            )
            return float(tx["trade_fx_rate"]) * settlement_to_target

        rate, _ = await self._fx_rate_on_date(source, target, on_date)
        return rate

    async def async_add(
        self,
        user_id: str,
        asset: dict[str, Any],
        quantity: float | None,
        *,
        gross_quantity: float | None = None,
        net_quantity: float | None = None,
        asset_fee_quantity: float | None = None,
        asset_fee_percent: float | None = None,
        principal_mode: str = "unit",
        average_buy_price: float | None = None,
        gross_trade_total: float | None = None,
        investment_total: float | None = None,
        transaction_costs: dict[str, Any] | None = None,
        transaction_cost_total: float | None = None,
        all_in_total: float | None = None,
        transaction_note: str | None = None,
        transaction_date: str | None = None,
        settlement_currency: str | None = None,
        fx_rate: float | None = None,
        trade_fx_rate: float | None = None,
        shared_allocations: list[dict[str, Any]] | None = None,
        shared_ownership: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Append a BUY while preserving native trade and settlement currencies."""
        if asset.get("provider") not in self.providers:
            raise ValueError("Unsupported provider")
        if not asset.get("provider_id") or not asset.get("symbol") or not asset.get("name"):
            raise ValueError("Incomplete asset")
        user = await self.store.async_user(user_id)
        base_currency = self._canonical_currency(user.get("base_currency") or "EUR")
        trade_currency = self._canonical_currency(asset.get("currency") or base_currency)
        default_settlement = default_settlement_currency(trade_currency)
        settlement_currency = self._canonical_currency(settlement_currency or default_settlement)
        if len(settlement_currency) != 3 or not settlement_currency.isalpha():
            raise ValueError("Settlement currency must be a 3-letter code")
        transaction_date = self._validated_transaction_date(transaction_date) or dt_util.now().date().isoformat()

        trade_to_settlement, trade_rate_date, trade_fx_source = await self._resolve_fx_leg(
            trade_currency, settlement_currency, transaction_date, trade_fx_rate, label="Trade FX rate"
        )
        settlement_to_base, settlement_rate_date, fx_source = await self._resolve_fx_leg(
            settlement_currency, base_currency, transaction_date, fx_rate, label="FX rate"
        )
        fx_date = settlement_rate_date or trade_rate_date or transaction_date

        asset_quantities = derive_asset_quantities(
            quantity=quantity, gross_quantity=gross_quantity, net_quantity=net_quantity,
            asset_fee_quantity=asset_fee_quantity, asset_fee_percent=asset_fee_percent,
        )
        quantity = asset_quantities.net
        gross_quantity = asset_quantities.gross
        shared_allocations = normalize_shared_allocations(shared_allocations, quantity)

        # Native unit/gross values remain in the instrument's trading currency.
        if gross_trade_total is not None:
            gross_trade_total = round(max(0.0, float(gross_trade_total)), 2)
        if average_buy_price is None and gross_trade_total is not None:
            average_buy_price = round(gross_trade_total / gross_quantity, 2)
        elif average_buy_price is not None:
            average_buy_price = round(max(0.0, float(average_buy_price)), 12)
        if gross_trade_total is None and average_buy_price is not None:
            gross_trade_total = round(average_buy_price * gross_quantity, 2)

        # Cash principal and cash fees are settlement-currency amounts.
        if investment_total is not None:
            investment_total = round(max(0.0, float(investment_total)), 2)
        if investment_total is None and gross_trade_total is not None:
            investment_total = round(gross_trade_total * trade_to_settlement, 2)
        if average_buy_price is None and investment_total is not None:
            native_principal = investment_total / trade_to_settlement
            average_buy_price = round(native_principal / gross_quantity, 2)
            gross_trade_total = round(average_buy_price * gross_quantity, 2)

        if principal_mode not in {"unit", "total", "independent"}:
            principal_mode = "unit"
        if average_buy_price is None and investment_total is None:
            raise ValueError("A buy price or investment total is required")

        clean_costs: dict[str, float] = {}
        for key in TRANSACTION_COST_TYPES:
            raw = (transaction_costs or {}).get(key)
            if raw in (None, ""):
                continue
            value = round(float(raw), 2)
            if value < 0:
                raise ValueError("Transaction costs cannot be negative")
            if value:
                clean_costs[key] = value
        detailed_cost_total = round(sum(clean_costs.values()), 2)
        if transaction_cost_total is not None:
            transaction_cost_total = round(float(transaction_cost_total), 2)
            if transaction_cost_total < 0:
                raise ValueError("Transaction cost total cannot be negative")
        if all_in_total is not None:
            all_in_total = round(float(all_in_total), 2)
            if all_in_total < 0:
                raise ValueError("All-in total cannot be negative")
            if investment_total is None:
                raise ValueError("An investment total is required to derive costs")
            inferred = round(all_in_total - investment_total, 2)
            if inferred < -0.000001:
                raise ValueError("All-in total cannot be below the investment total")
            inferred = max(0.0, inferred)
            if transaction_cost_total is not None and not math.isclose(transaction_cost_total, inferred, rel_tol=1e-7, abs_tol=0.01):
                raise ValueError("Transaction costs and all-in total do not match")
            transaction_cost_total = inferred
        if transaction_cost_total is None:
            transaction_cost_total = detailed_cost_total
        if transaction_cost_total + 0.000001 < detailed_cost_total:
            raise ValueError("Transaction cost total cannot be below the detailed costs")
        residual = round(max(0.0, transaction_cost_total - detailed_cost_total), 2)
        if residual:
            clean_costs["other"] = round(clean_costs.get("other", 0.0) + residual, 2)
        if all_in_total is None and investment_total is not None:
            all_in_total = round(investment_total + transaction_cost_total, 2)

        holding = await self.store.async_add_holding(
            user_id, asset, quantity, average_buy_price=average_buy_price,
            gross_trade_total=gross_trade_total, investment_total=investment_total,
            transaction_costs=clean_costs, transaction_cost_total=transaction_cost_total,
            all_in_total=all_in_total, transaction_note=transaction_note, transaction_date=transaction_date,
            fee_currency=settlement_currency, gross_quantity=gross_quantity,
            asset_fee_quantity=asset_quantities.fee, asset_fee_percent=asset_quantities.fee_percent,
            transaction_currency=trade_currency, settlement_currency=settlement_currency,
            portfolio_currency_at_transaction=base_currency, fx_rate=settlement_to_base,
            quote_fx_rate=(trade_to_settlement * settlement_to_base), trade_fx_rate=trade_to_settlement,
            fx_date=fx_date or transaction_date, fx_source=fx_source, trade_fx_source=trade_fx_source,
            shared_allocations=shared_allocations, shared_ownership=shared_ownership,
        )
        self._cache.clear_prefix(("portfolio", user_id))
        self._cache.clear_prefix(("scope_history", user_id))
        return holding


    async def async_sell(
        self, user_id: str, holding_id: str, quantity: float, *,
        sell_price: float | None = None, gross_sale_total: float | None = None,
        proceeds_total: float | None = None, transaction_costs: dict[str, Any] | None = None,
        transaction_cost_total: float | None = None, transaction_note: str | None = None,
        transaction_date: str | None = None, settlement_currency: str | None = None,
        fx_rate: float | None = None,
        trade_fx_rate: float | None = None,
    ) -> dict[str, Any]:
        """Append a SELL with native trade values and settlement-currency proceeds."""
        user = await self.store.async_user(user_id)
        holding = next((item for item in user.get("holdings", []) if item.get("id") == holding_id), None)
        if holding is None:
            raise ValueError("Holding not found")
        quantity = float(quantity)
        if not math.isfinite(quantity) or quantity <= 0:
            raise ValueError("Sell quantity must be greater than zero")
        tx_date = self._validated_transaction_date(transaction_date) or dt_util.now().date().isoformat()

        raw_records: list[dict[str, Any]] = []
        for tx in holding.get("transactions") or []:
            if tx.get("type", "buy") == "sell":
                raw_records.append({"id": tx.get("id"), "type": "sell", "sort_ts": transaction_timestamp(tx), "quantity": float(tx.get("quantity") or 0)})
            else:
                raw_records.append({"id": tx.get("id"), "type": "buy", "sort_ts": transaction_timestamp(tx), "quantity": personal_quantity(tx)})
        tmp_tx = {"id": "__new_sell__", "type": "sell", "date": tx_date, "created_at": int(time.time()), "quantity": quantity}
        raw_records.append({"id": "__new_sell__", "type": "sell", "sort_ts": transaction_timestamp(tmp_tx), "quantity": quantity})
        fifo_summary(raw_records)

        base = self._canonical_currency(user.get("base_currency") or "EUR")
        trade_currency = self._canonical_currency(holding.get("currency") or base)
        default_settlement = default_settlement_currency(trade_currency)
        settlement_currency = self._canonical_currency(settlement_currency or default_settlement)
        trade_to_settlement, trade_rate_date, trade_fx_source = await self._resolve_fx_leg(
            trade_currency, settlement_currency, tx_date, trade_fx_rate, label="Trade FX rate"
        )
        settlement_to_base, settlement_rate_date, fx_source = await self._resolve_fx_leg(
            settlement_currency, base, tx_date, fx_rate, label="FX rate"
        )
        fx_date = settlement_rate_date or trade_rate_date or tx_date

        unit = None if sell_price is None else round(max(0.0, float(sell_price)), 12)
        gross = None if gross_sale_total is None else round(max(0.0, float(gross_sale_total)), 2)
        if gross is None and unit is not None:
            gross = round(unit * quantity, 2)
        elif unit is None and gross is not None:
            unit = round(gross / quantity, 2)
        if gross is None:
            raise ValueError("A sell price or total sale value is required")
        gross_settlement = round(gross * trade_to_settlement, 2)

        clean_costs: dict[str, float] = {}
        for key in TRANSACTION_COST_TYPES:
            raw = (transaction_costs or {}).get(key)
            if raw in (None, ""):
                continue
            value = round(float(raw), 2)
            if value < 0:
                raise ValueError("Transaction costs cannot be negative")
            if value:
                clean_costs[key] = value
        detailed = round(sum(clean_costs.values()), 2)
        costs = None if transaction_cost_total is None else round(float(transaction_cost_total), 2)
        if costs is not None and costs < 0:
            raise ValueError("Transaction cost total cannot be negative")
        proceeds = None if proceeds_total is None else round(float(proceeds_total), 2)
        if proceeds is not None:
            if proceeds < 0 or proceeds > gross_settlement + 0.000001:
                raise ValueError("Net sale proceeds must be between zero and gross sale value")
            inferred = round(max(0.0, gross_settlement - proceeds), 2)
            if costs is not None and not math.isclose(costs, inferred, rel_tol=1e-7, abs_tol=0.01):
                raise ValueError("Sale costs and net proceeds do not match")
            costs = inferred
        if costs is None:
            costs = detailed
        if costs + 0.000001 < detailed:
            raise ValueError("Transaction cost total cannot be below the detailed costs")
        if costs > gross_settlement + 0.000001:
            raise ValueError("Transaction costs cannot exceed gross sale value")
        if proceeds is None:
            proceeds = round(max(0.0, gross_settlement - costs), 2)
        residual = round(max(0.0, costs - detailed), 2)
        if residual:
            clean_costs["other"] = round(clean_costs.get("other", 0.0) + residual, 2)

        updated = await self.store.async_add_sell_transaction(
            user_id, holding_id, quantity, sell_price=unit, gross_sale_total=gross,
            proceeds_total=proceeds, transaction_costs=clean_costs, transaction_cost_total=costs,
            transaction_note=transaction_note, transaction_date=tx_date, fee_currency=settlement_currency,
            transaction_currency=trade_currency, settlement_currency=settlement_currency,
            portfolio_currency_at_transaction=base, fx_rate=settlement_to_base, quote_fx_rate=(trade_to_settlement * settlement_to_base),
            trade_fx_rate=trade_to_settlement, fx_date=fx_date or tx_date, fx_source=fx_source, trade_fx_source=trade_fx_source,
        )
        if updated is None:
            raise ValueError("Holding not found")
        self._cache.clear_prefix(("portfolio", user_id)); self._cache.clear_prefix(("scope_history", user_id))
        return updated


    async def async_edit_transaction(
        self, user_id: str, holding_id: str, transaction_id: str, *,
        quantity: float | None = None, gross_quantity: float | None = None,
        net_quantity: float | None = None, asset_fee_quantity: float | None = None,
        asset_fee_percent: float | None = None, average_buy_price: float | None = None,
        gross_trade_total: float | None = None, investment_total: float | None = None,
        all_in_total: float | None = None, sell_price: float | None = None,
        gross_sale_total: float | None = None, proceeds_total: float | None = None,
        transaction_costs: dict[str, Any] | None = None, transaction_cost_total: float | None = None,
        transaction_note: str | None = None, transaction_date: str | None = None,
        settlement_currency: str | None = None, fx_rate: float | None = None,
        trade_fx_rate: float | None = None,
        shared_allocations: list[dict[str, Any]] | None = None,
        shared_ownership: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Edit one transaction, preserving native currency truth and rerunning FIFO."""
        user = await self.store.async_user(user_id)
        holding = next((item for item in user.get("holdings", []) if item.get("id") == holding_id), None)
        if holding is None:
            raise ValueError("Holding not found")
        original = next((tx for tx in holding.get("transactions") or [] if str(tx.get("id")) == str(transaction_id)), None)
        if original is None:
            raise ValueError("Transaction not found")
        tx_type = str(original.get("type") or "buy")
        tx_date = self._validated_transaction_date(transaction_date) or str(original.get("date") or dt_util.now().date().isoformat())
        base = self._canonical_currency(user.get("base_currency") or "EUR")
        trade_currency = self._canonical_currency(holding.get("currency") or original.get("transaction_currency") or base)
        default_settlement = default_settlement_currency(trade_currency)
        settlement_currency = self._canonical_currency(settlement_currency or original.get("settlement_currency") or original.get("fee_currency") or default_settlement)
        manual_trade_fx = trade_fx_rate is not None
        manual_settlement_fx = fx_rate is not None
        original_trade_currency = self._canonical_currency(original.get("transaction_currency") or holding.get("currency") or base)
        original_settlement_currency = self._canonical_currency(original.get("settlement_currency") or original.get("fee_currency") or default_settlement)
        same_fx_context = (
            tx_date == str(original.get("date") or "")
            and trade_currency == original_trade_currency
            and settlement_currency == original_settlement_currency
            and base == self._canonical_currency(original.get("portfolio_currency_at_transaction") or base)
        )

        fixed_trade = fixed_conversion_rate(trade_currency, settlement_currency)
        if fixed_trade is not None:
            if manual_trade_fx:
                supplied = float(trade_fx_rate)
                if not math.isfinite(supplied) or supplied <= 0:
                    raise ValueError("Trade FX rate must be greater than zero")
                if not math.isclose(supplied, fixed_trade, rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError(f"Trade FX rate is fixed at {fixed_trade:g} for {trade_currency} to {settlement_currency}")
            trade_to_settlement = fixed_trade
            trade_fx_source = "identity" if math.isclose(fixed_trade, 1.0) else "fixed"
            fx_date = tx_date
        elif manual_trade_fx:
            trade_to_settlement = float(trade_fx_rate)
            if not math.isfinite(trade_to_settlement) or trade_to_settlement <= 0:
                raise ValueError("Trade FX rate must be greater than zero")
            trade_fx_source = "manual"
            fx_date = tx_date
        elif same_fx_context and original.get("trade_fx_rate") is not None:
            trade_to_settlement = float(original["trade_fx_rate"])
            trade_fx_source = str(original.get("trade_fx_source") or "historical")
            fx_date = str(original.get("fx_date") or tx_date)
        else:
            trade_to_settlement, fx_date = await self._fx_rate_on_date(
                trade_currency, settlement_currency, tx_date
            )
            trade_fx_source = "Frankfurter"

        fixed_settlement = fixed_conversion_rate(settlement_currency, base)
        if fixed_settlement is not None:
            if manual_settlement_fx:
                supplied = float(fx_rate)
                if not math.isfinite(supplied) or supplied <= 0:
                    raise ValueError("FX rate must be greater than zero")
                if not math.isclose(supplied, fixed_settlement, rel_tol=1e-10, abs_tol=1e-12):
                    raise ValueError(f"FX rate is fixed at {fixed_settlement:g} for {settlement_currency} to {base}")
            settlement_to_base = fixed_settlement
            fx_source = "identity" if math.isclose(fixed_settlement, 1.0) else "fixed"
        elif manual_settlement_fx:
            settlement_to_base = float(fx_rate)
            if not math.isfinite(settlement_to_base) or settlement_to_base <= 0:
                raise ValueError("FX rate must be greater than zero")
            fx_source = "manual"
        elif same_fx_context and original.get("fx_rate") is not None:
            settlement_to_base = float(original["fx_rate"])
            fx_source = str(original.get("fx_source") or "historical")
        else:
            settlement_to_base, _ = await self._fx_rate_on_date(settlement_currency, base, tx_date)
            fx_source = "Frankfurter"

        clean_costs: dict[str, float] = {}
        for key in TRANSACTION_COST_TYPES:
            raw = (transaction_costs or {}).get(key)
            if raw in (None, ""):
                continue
            value = round(float(raw), 2)
            if not math.isfinite(value) or value < 0:
                raise ValueError("Transaction costs cannot be negative")
            if value:
                clean_costs[key] = value
        detailed = round(sum(clean_costs.values()), 2)
        costs = None if transaction_cost_total is None else round(float(transaction_cost_total), 2)
        if costs is not None and (not math.isfinite(costs) or costs < 0):
            raise ValueError("Transaction cost total cannot be negative")
        replacement = deepcopy(original)
        replacement.update({
            "date": tx_date, "note": (transaction_note or "").strip()[:500] or None,
            "transaction_currency": trade_currency, "settlement_currency": settlement_currency,
            "fee_currency": settlement_currency, "portfolio_currency_at_transaction": base,
            "fx_rate": settlement_to_base, "quote_fx_rate": trade_to_settlement * settlement_to_base,
            "trade_fx_rate": trade_to_settlement, "fx_date": fx_date or tx_date,
            "fx_source": fx_source, "trade_fx_source": trade_fx_source,
        })

        if tx_type == "sell":
            sell_quantity = float(quantity or 0)
            if not math.isfinite(sell_quantity) or sell_quantity <= 0:
                raise ValueError("Sell quantity must be greater than zero")
            unit = None if sell_price is None else round(max(0.0, float(sell_price)), 12)
            gross = None if gross_sale_total is None else round(max(0.0, float(gross_sale_total)), 2)
            if gross is None and unit is not None: gross = round(unit * sell_quantity, 2)
            elif unit is None and gross is not None: unit = round(gross / sell_quantity, 2)
            if gross is None: raise ValueError("A sell price or total sale value is required")
            gross_settlement = round(gross * trade_to_settlement, 2)
            proceeds = None if proceeds_total is None else round(float(proceeds_total), 2)
            if proceeds is not None:
                if not math.isfinite(proceeds) or proceeds < 0 or proceeds > gross_settlement + 0.000001:
                    raise ValueError("Net sale proceeds must be between zero and gross sale value")
                inferred = round(max(0.0, gross_settlement - proceeds), 2)
                if costs is not None and not math.isclose(costs, inferred, rel_tol=1e-7, abs_tol=0.01):
                    raise ValueError("Sale costs and net proceeds do not match")
                costs = inferred
            if costs is None: costs = detailed
            if costs + 0.000001 < detailed: raise ValueError("Transaction cost total cannot be below the detailed costs")
            if costs > gross_settlement + 0.000001: raise ValueError("Transaction costs cannot exceed gross sale value")
            if proceeds is None: proceeds = round(max(0.0, gross_settlement - costs), 2)
            residual = round(max(0.0, costs - detailed), 2)
            if residual: clean_costs["other"] = round(clean_costs.get("other", 0.0) + residual, 2)
            replacement.update({"quantity": sell_quantity, "sell_price": unit, "gross_sale_total": gross, "proceeds_total": proceeds, "costs": clean_costs, "cost_total": costs})
        else:
            quantities = derive_asset_quantities(quantity=quantity, gross_quantity=gross_quantity, net_quantity=net_quantity, asset_fee_quantity=asset_fee_quantity, asset_fee_percent=asset_fee_percent)
            gross, net = quantities.gross, quantities.net
            trade_total = None if gross_trade_total is None else round(max(0.0, float(gross_trade_total)), 2)
            unit = None if average_buy_price is None else round(max(0.0, float(average_buy_price)), 12)
            if unit is None and trade_total is not None: unit = round(trade_total / gross, 2)
            elif trade_total is None and unit is not None: trade_total = round(unit * gross, 2)
            cash = None if investment_total is None else round(max(0.0, float(investment_total)), 2)
            if cash is None and trade_total is not None: cash = round(trade_total * trade_to_settlement, 2)
            if unit is None and cash is not None:
                unit = round((cash / trade_to_settlement) / gross, 2)
                trade_total = round(unit * gross, 2)
            if unit is None and cash is None: raise ValueError("A buy price or investment total is required")
            all_in = None if all_in_total is None else round(float(all_in_total), 2)
            if all_in is not None:
                if not math.isfinite(all_in) or all_in < 0: raise ValueError("All-in total cannot be negative")
                inferred = round(all_in - cash, 2)
                if inferred < -0.000001: raise ValueError("All-in total cannot be below the investment total")
                inferred = max(0.0, inferred)
                if costs is not None and not math.isclose(costs, inferred, rel_tol=1e-7, abs_tol=0.01): raise ValueError("Transaction costs and all-in total do not match")
                costs = inferred
            if costs is None: costs = detailed
            if costs + 0.000001 < detailed: raise ValueError("Transaction cost total cannot be below the detailed costs")
            residual = round(max(0.0, costs - detailed), 2)
            if residual: clean_costs["other"] = round(clean_costs.get("other", 0.0) + residual, 2)
            if all_in is None and cash is not None: all_in = round(cash + costs, 2)
            share_rows = normalize_shared_allocations(
                shared_allocations if shared_allocations is not None else original.get("shared_allocations"), net
            )
            share_meta = normalize_shared_ownership(
                shared_ownership if shared_ownership is not None else original.get("shared_ownership")
            )
            replacement.update({
                "quantity": net, "net_quantity": net, "gross_quantity": gross,
                "shared_allocations": share_rows,
                "shared_ownership": share_meta,
                "shared_quantity": round(sum(float(a["quantity"]) for a in share_rows), 12),
                "personal_quantity": round(max(0.0, net - sum(float(a["quantity"]) for a in share_rows)), 12),
                "asset_fee_quantity": quantities.fee, "asset_fee_percent": quantities.fee_percent,
                "buy_price": unit, "gross_trade_total": trade_total, "investment_total": cash,
                "costs": clean_costs, "cost_total": costs, "all_in_total": all_in,
            })

        updated = await self.store.async_replace_transaction(user_id, holding_id, transaction_id, replacement)
        if updated is None: raise ValueError("Transaction not found")
        self._cache.clear_prefix(("portfolio", user_id)); self._cache.clear_prefix(("scope_history", user_id))
        return updated

    async def _ledger_in_currency(
        self, holding: dict[str, Any], target_currency: str, *, current_price: float | None = None
    ):
        """Build FIFO using the FX rate frozen/historical on every transaction date."""
        records: list[dict[str, Any]] = []
        transactions = holding.get("transactions") or []
        if not transactions:
            return fifo_summary([], current_price=current_price)
        for tx in transactions:
            trade_currency = self._canonical_currency(tx.get("transaction_currency") or holding.get("currency") or target_currency)
            settlement_currency = self._canonical_currency(tx.get("settlement_currency") or tx.get("fee_currency") or trade_currency)
            trade_fx = await self._transaction_fx_rate(tx, trade_currency, target_currency)
            settlement_fx = await self._transaction_fx_rate(tx, settlement_currency, target_currency)
            costs = tx.get("costs") or {}
            explicit_costs_native = sum(max(0.0, float(costs.get(key) or 0)) for key in TRANSACTION_COST_TYPES)
            tx_type = str(tx.get("type") or "buy")
            owner_ratio_for_costs = personal_ratio(tx) if tx_type == "buy" else 1.0
            owner_explicit_costs_native = explicit_costs_native * owner_ratio_for_costs
            explicit_costs = owner_explicit_costs_native * settlement_fx
            if tx_type == "sell":
                qty = float(tx.get("quantity") or 0)
                native_unit = tx.get("sell_price")
                native_gross = tx.get("gross_sale_total")
                if native_gross is None and native_unit is not None:
                    native_gross = float(native_unit) * qty
                native_proceeds = tx.get("proceeds_total")
                if native_proceeds is None and native_gross is not None:
                    # Gross is trade currency, cash fees are settlement currency.
                    trade_to_settle = settlement_fx and (trade_fx / settlement_fx)
                    gross_settle = float(native_gross) * trade_to_settle if trade_to_settle else float(native_gross)
                    native_proceeds = max(0.0, gross_settle - explicit_costs_native)
                records.append({
                    **deepcopy(tx), "type": "sell", "sort_ts": transaction_timestamp(tx), "quantity": qty,
                    "native_unit_price": native_unit, "native_gross_value": native_gross,
                    "native_net_proceeds": native_proceeds,
                    "native_explicit_costs": owner_explicit_costs_native, "native_display_costs": owner_explicit_costs_native,
                    "unit_price": (float(native_unit) * trade_fx if native_unit is not None else None),
                    "gross_value": (float(native_gross) * trade_fx if native_gross is not None else None),
                    "net_proceeds": (float(native_proceeds) * settlement_fx if native_proceeds is not None else None),
                    "explicit_costs": explicit_costs, "display_costs": explicit_costs,
                })
                continue

            net = float(tx.get("net_quantity", tx.get("quantity", 0)) or 0)
            owner_net = personal_quantity(tx)
            owner_ratio = personal_ratio(tx)
            shared_net = shared_quantity(tx)
            gross = float(tx.get("gross_quantity", net) or 0)
            native_unit = tx.get("buy_price")
            native_principal = tx.get("investment_total")
            native_gross = tx.get("gross_trade_total")
            if native_principal is None and native_gross is not None:
                trade_to_settle = settlement_fx and (trade_fx / settlement_fx)
                native_principal = float(native_gross) * trade_to_settle if trade_to_settle else float(native_gross)
            # Embedded withheld-asset economics are derived in trade currency by
            # converting settlement cash back to the trade currency first.
            trade_to_settle = settlement_fx and (trade_fx / settlement_fx)
            principal_trade = (float(native_principal) / trade_to_settle if native_principal is not None and trade_to_settle else native_principal)
            settlement = derive_purchase_settlement(
                gross_quantity=gross, net_quantity=net, average_buy_price=native_unit,
                investment_total=principal_trade, gross_trade_total=native_gross,
            )
            records.append({
                **deepcopy(tx), "type": "buy", "sort_ts": transaction_timestamp(tx), "quantity": owner_net,
                "transaction_net_quantity": net, "personal_quantity": owner_net,
                "shared_quantity": shared_net, "personal_ratio": owner_ratio,
                "native_unit_price": native_unit,
                "native_cash_principal": (float(native_principal) * owner_ratio if native_principal is not None else None),
                "native_gross_value": (float(native_gross) * owner_ratio if native_gross is not None else None),
                "native_explicit_costs": owner_explicit_costs_native,
                "native_asset_fee_value": settlement.withheld_asset_value * trade_to_settle * owner_ratio,
                "native_embedded_asset_fee_cost": settlement.embedded_asset_fee_cost * trade_to_settle * owner_ratio,
                "native_display_costs": owner_explicit_costs_native + settlement.withheld_asset_value * trade_to_settle * owner_ratio,
                "unit_price": (float(native_unit) * trade_fx if native_unit is not None else None),
                "cash_principal": (float(native_principal) * settlement_fx * owner_ratio if native_principal is not None else None),
                "explicit_costs": explicit_costs,
                "asset_fee_value": settlement.withheld_asset_value * trade_fx * owner_ratio,
                "embedded_asset_fee_cost": settlement.embedded_asset_fee_cost * trade_fx * owner_ratio,
                "display_costs": explicit_costs + settlement.withheld_asset_value * trade_fx * owner_ratio,
            })
        return fifo_summary(records, current_price=current_price)


    async def async_update(self, user_id: str, holding_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        # In ledger mode quantity and historical prices are derived from immutable
        # BUY/SELL records. Reject legacy aggregate edits instead of silently
        # creating a portfolio state that cannot be reconciled to history.
        if changes.get("quantity") is not None or changes.get("average_buy_price") is not None:
            raise ValueError("Use Buy/Sell transactions to change quantity or cost basis")
        updated = await self.store.async_update_holding(user_id, holding_id, changes)
        if updated is None:
            raise ValueError("Holding not found")
        self._cache.clear_prefix(("portfolio", user_id))
        self._cache.clear_prefix(("scope_history", user_id))
        return updated

    async def async_remove(self, user_id: str, holding_id: str) -> bool:
        # v0.3+ treats BUY/SELL records as an audit ledger. A holding that has
        # transaction history must never be deleted, even by an older cached
        # frontend calling the legacy remove command. Fully sold assets remain
        # visible with quantity zero and their historical rows intact.
        user = await self.store.async_user(user_id)
        holding = next((item for item in user.get("holdings", []) if item.get("id") == holding_id), None)
        if holding is None:
            return False
        if holding.get("transactions"):
            raise ValueError("Transaction history cannot be deleted; use Buy/Sell records to preserve the ledger")
        removed = await self.store.async_remove_holding(user_id, holding_id)
        self._cache.clear_prefix(("portfolio", user_id))
        self._cache.clear_prefix(("scope_history", user_id))
        return removed


    async def async_set_preferences(
        self,
        user_id: str,
        *,
        base_currency: str | None = None,
        language: str | None = None,
        incognito: bool | None = None,
        incognito_reveal_seconds: int | None = None,
        exposed_entities: list[str] | None = None,
    ) -> dict[str, Any]:
        """Update private per-user display, privacy and automation preferences."""
        if (
            base_currency is None
            and language is None
            and incognito is None
            and incognito_reveal_seconds is None
            and exposed_entities is None
        ):
            raise ValueError("At least one preference is required")
        if base_currency is not None:
            if len(base_currency) != 3 or not base_currency.isalpha():
                raise ValueError("Currency must be a 3-letter code")
            base_currency = base_currency.upper()
        if language is not None:
            language = language.lower()
            if language != DEFAULT_UI_LANGUAGE and language not in SUPPORTED_UI_LANGUAGES:
                raise ValueError("Unsupported investment UI language")
        normalized_reveal_seconds = None
        if incognito_reveal_seconds is not None:
            try:
                normalized_reveal_seconds = int(incognito_reveal_seconds)
            except (TypeError, ValueError) as err:
                raise ValueError("Incognito reveal duration must be a whole number of seconds") from err
            if normalized_reveal_seconds < 0 or normalized_reveal_seconds > MAX_INCOGNITO_REVEAL_SECONDS:
                raise ValueError(
                    f"Incognito reveal duration must be between 0 and {MAX_INCOGNITO_REVEAL_SECONDS} seconds"
                )
        normalized_exposed = None
        previous_exposed: list[str] | None = None
        if exposed_entities is not None:
            if not isinstance(exposed_entities, list):
                raise ValueError("Exposed entities must be a list")
            requested = {str(metric) for metric in exposed_entities}
            unknown = requested - set(EXPOSABLE_ENTITY_METRICS)
            if unknown:
                raise ValueError(f"Unsupported automation entity metric: {sorted(unknown)[0]}")
            normalized_exposed = [metric for metric in EXPOSABLE_ENTITY_METRICS if metric in requested]
            previous_exposed = list((await self.store.async_user(user_id)).get("exposed_entities") or [])
        user = await self.store.async_set_preferences(
            user_id,
            base_currency=base_currency,
            language=language,
            incognito=incognito,
            incognito_reveal_seconds=normalized_reveal_seconds,
            exposed_entities=normalized_exposed,
        )
        self._cache.clear_prefix(("portfolio", user_id))
        self._cache.clear_prefix(("scope_history", user_id))
        if normalized_exposed is not None and previous_exposed != normalized_exposed:
            async_dispatcher_send(
                self.hass, SIGNAL_ENTITY_EXPOSURE_CHANGED, user_id, tuple(normalized_exposed)
            )
        return user


    async def async_entity_exposure_snapshot(self) -> dict[str, list[str]]:
        """Return current opt-in automation entity selections."""
        return await self.store.async_entity_exposure_snapshot()

    async def async_set_base_currency(self, user_id: str, currency: str) -> dict[str, Any]:
        """Backward-compatible currency preference helper."""
        return await self.async_set_preferences(user_id, base_currency=currency)

    async def async_set_category_expense(
        self, user_id: str, category: str, amount: float | None
    ) -> dict[str, Any]:
        user = await self.store.async_set_category_expense(user_id, category, amount)
        self._cache.clear_prefix(("portfolio", user_id))
        return user

    async def async_asset_quote(self, user_id: str, asset: dict[str, Any]) -> dict[str, Any]:
        """Return the latest verified quote for one search result/holding asset."""
        provider_id = str(asset.get("provider_id") or "").strip()
        provider_name = str(asset.get("provider") or "").strip()
        if provider_name not in self.providers or not provider_id:
            raise ValueError("Unsupported or incomplete asset")
        requested_currency = str(asset.get("currency") or "").strip()
        quote = await self._quote(asset)
        if requested_currency and not quote_currency_matches(quote.currency, requested_currency):
            raise ValueError(f"Quote currency {quote.currency} does not match selected asset currency {requested_currency}")
        return quote.as_dict()

    async def _quote(self, holding: dict[str, Any], *, force: bool = False) -> Quote:
        key = ("quote", holding["provider"], holding["provider_id"])
        if not force:
            cached = self._cache.get(key, DEFAULT_QUOTE_CACHE_SECONDS)
            if cached is not None:
                return Quote(**cached)
        provider_name = str(holding.get("provider") or "")
        provider = self.providers.get(provider_name)
        if provider is None:
            if provider_name not in {"twelve_data", "alpha_vantage"}:
                raise ProviderError(f"Unsupported provider {provider_name}")
            return await self._fallback_paid_quote(holding, ProviderError(f"{provider_name} is not configured"))
        try:
            async with self._network_sem:
                quote = await provider.async_quote(holding["provider_id"])
        except Exception as err:
            if provider_name == "yahoo":
                try:
                    async with self._network_sem:
                        quote = await self.stooq.async_quote(holding["provider_id"])
                except Exception:
                    raise ProviderError(str(err)) from err
            elif provider_name in {"twelve_data", "alpha_vantage"}:
                quote = await self._fallback_paid_quote(holding, err)
            else:
                raise
        self._cache.set(key, quote.as_dict())
        return quote

    async def _fallback_paid_quote(self, holding: dict[str, Any], first_err: Exception) -> Quote:
        """Keep API-key holdings readable if a paid feed/key becomes unavailable."""
        symbol = str(holding.get("symbol") or "").strip()
        if not symbol:
            raise ProviderError(str(first_err)) from first_err
        yahoo_symbol = symbol.replace("/", "-") if str(holding.get("category") or "") == "crypto" else symbol
        expected_currency = str(holding.get("currency") or "").strip()
        try:
            async with self._network_sem:
                quote = await self.yahoo.async_quote(yahoo_symbol)
            if expected_currency and not quote_currency_matches(
                quote.currency, expected_currency
            ):
                raise ProviderError(
                    f"Fallback quote currency {quote.currency} does not match "
                    f"holding currency {expected_currency}"
                )
            return quote
        except Exception:
            try:
                async with self._network_sem:
                    quote = await self.stooq.async_quote(symbol)
                if expected_currency and not quote_currency_matches(
                    quote.currency, expected_currency
                ):
                    raise ProviderError(
                        f"Fallback quote currency {quote.currency} does not match "
                        f"holding currency {expected_currency}"
                    )
                return quote
            except Exception:
                raise ProviderError(str(first_err)) from first_err

    async def _fx_rate(self, from_currency: str, to_currency: str, *, force: bool = False) -> float:
        from_currency, from_scale = self._normalize_currency(from_currency)
        to_currency, to_scale = self._normalize_currency(to_currency)
        if from_currency == to_currency:
            return from_scale / to_scale
        key = ("fx", from_currency, to_currency)
        if not force:
            cached = self._cache.get(key, 300)
            if cached is not None:
                return float(cached) * from_scale / to_scale
        quote = await self.frankfurter.async_quote(f"{from_currency}/{to_currency}")
        self._cache.set(key, quote.price)
        return quote.price * from_scale / to_scale

    @staticmethod
    def _canonical_currency(currency: str | None) -> str:
        return canonical_currency(currency, "USD")

    @staticmethod
    def _normalize_currency(currency: str | None) -> tuple[str, float]:
        return normalize_currency(currency, "USD")

    async def _transaction_costs_in_currency(
        self, holding: dict[str, Any], target_currency: str
    ) -> tuple[dict[str, float], float]:
        """Convert each fee using that transaction's frozen/historical settlement FX."""
        breakdown = {key: 0.0 for key in TRANSACTION_COST_TYPES}
        for tx in holding.get("transactions") or []:
            currency = self._canonical_currency(tx.get("settlement_currency") or tx.get("fee_currency") or holding.get("currency") or target_currency)
            fx = await self._transaction_fx_rate(tx, currency, target_currency)
            costs = tx.get("costs") or {}
            ratio = personal_ratio(tx) if tx.get("type", "buy") != "sell" else 1.0
            for key in TRANSACTION_COST_TYPES:
                raw = costs.get(key)
                if raw in (None, ""): continue
                value = float(raw)
                if math.isfinite(value) and value > 0: breakdown[key] += value * fx * ratio
        return breakdown, sum(breakdown.values())

    async def _transaction_principal_in_currency(
        self, holding: dict[str, Any], target_currency: str
    ) -> tuple[float | None, bool]:
        transactions = holding.get("transactions") or []
        if not transactions:
            avg = holding.get("average_buy_price")
            if avg is None: return None, False
            source = self._canonical_currency(holding.get("currency") or target_currency)
            fx = await self._fx_rate(source, target_currency)
            return float(avg) * float(holding.get("quantity") or 0) * fx, True
        total = 0.0; complete = True
        for tx in transactions:
            if tx.get("type", "buy") == "sell": continue
            raw_total = tx.get("investment_total")
            settlement_currency = self._canonical_currency(tx.get("settlement_currency") or tx.get("fee_currency") or holding.get("currency") or target_currency)
            if raw_total is None:
                price = tx.get("buy_price"); gross = tx.get("gross_quantity", tx.get("quantity"))
                if price is None or gross is None: complete = False; continue
                trade_currency = self._canonical_currency(tx.get("transaction_currency") or holding.get("currency") or target_currency)
                fx = await self._transaction_fx_rate(tx, trade_currency, target_currency)
                total += float(price) * float(gross) * fx * personal_ratio(tx)
            else:
                fx = await self._transaction_fx_rate(tx, settlement_currency, target_currency)
                total += float(raw_total) * fx * personal_ratio(tx)
        return (total if complete else None), complete

    async def _settlement_summary(
        self, holding: dict[str, Any], target_currency: str
    ) -> dict[str, float]:
        summary = {"gross_quantity":0.0,"asset_fee_quantity":0.0,"asset_fee_value":0.0,"gross_trade_value":0.0,"net_asset_value":0.0,"settlement_deduction":0.0,"embedded_asset_fee_cost":0.0,"asset_principal":0.0}
        transactions = holding.get("transactions") or []
        if not transactions:
            summary["gross_quantity"] = float(holding.get("quantity") or 0)
            avg=holding.get("average_buy_price")
            if avg is not None:
                fx=await self._fx_rate(str(holding.get("currency") or target_currency),target_currency)
                value=float(avg)*summary["gross_quantity"]*fx
                summary.update({"gross_trade_value":value,"net_asset_value":value,"asset_principal":value})
            return summary
        for tx in transactions:
            if tx.get("type", "buy") == "sell": continue
            net=float(tx.get("net_quantity",tx.get("quantity",0)) or 0); gross=float(tx.get("gross_quantity",net) or 0)
            ratio=personal_ratio(tx); owner_net=personal_quantity(tx)
            fee=float(tx.get("asset_fee_quantity",max(0.0,gross-net)) or 0); price=tx.get("buy_price")
            cash=tx.get("investment_total"); gross_trade=tx.get("gross_trade_total")
            trade_currency=self._canonical_currency(tx.get("transaction_currency") or holding.get("currency") or target_currency)
            settle_currency=self._canonical_currency(tx.get("settlement_currency") or tx.get("fee_currency") or trade_currency)
            trade_fx=await self._transaction_fx_rate(tx,trade_currency,target_currency)
            settle_fx=await self._transaction_fx_rate(tx,settle_currency,target_currency)
            trade_to_settle = trade_fx / settle_fx if settle_fx else 1.0
            cash_trade=(float(cash)/trade_to_settle if cash is not None and trade_to_settle else cash)
            settlement=derive_purchase_settlement(gross_quantity=gross,net_quantity=net,average_buy_price=price,investment_total=cash_trade,gross_trade_total=gross_trade)
            summary["gross_quantity"]+=gross*ratio; summary["asset_fee_quantity"]+=fee*ratio
            summary["asset_fee_value"] += settlement.withheld_asset_value*trade_fx*ratio
            if settlement.gross_trade_value is not None: summary["gross_trade_value"] += settlement.gross_trade_value*trade_fx*ratio
            if settlement.net_asset_value is not None: summary["net_asset_value"] += settlement.net_asset_value*trade_fx*ratio
            summary["settlement_deduction"] += settlement.settlement_deduction*trade_fx*ratio
            summary["embedded_asset_fee_cost"] += settlement.embedded_asset_fee_cost*trade_fx*ratio
            if settlement.asset_principal is not None: summary["asset_principal"] += settlement.asset_principal*trade_fx*ratio
        return summary

    async def _shared_participant_statistics(
        self,
        holding: dict[str, Any],
        target_currency: str,
        *,
        current_price: float | None,
        previous_price: float | None,
    ) -> list[dict[str, Any]]:
        """Return compact per-participant economics without affecting owner totals.

        Shared ownership is BUY-local.  Until explicit shared-person SELL records
        exist, participant rows are intentionally buy-only: the portfolio owner's
        SELL ledger must never consume another participant's units.
        """
        buckets: dict[str, dict[str, Any]] = {}
        for tx in holding.get("transactions") or []:
            if str(tx.get("type") or "buy") == "sell":
                continue
            net = max(0.0, float(tx.get("net_quantity", tx.get("quantity", 0)) or 0))
            if net <= 1e-12:
                continue
            allocations = tx.get("shared_allocations") or []
            if not allocations:
                continue

            trade_currency = self._canonical_currency(
                tx.get("transaction_currency") or holding.get("currency") or target_currency
            )
            settlement_currency = self._canonical_currency(
                tx.get("settlement_currency") or tx.get("fee_currency") or trade_currency
            )
            trade_fx = await self._transaction_fx_rate(tx, trade_currency, target_currency)
            settlement_fx = await self._transaction_fx_rate(tx, settlement_currency, target_currency)
            costs = tx.get("costs") or {}
            explicit_costs_native = sum(
                max(0.0, float(costs.get(key) or 0)) for key in TRANSACTION_COST_TYPES
            )
            gross = max(0.0, float(tx.get("gross_quantity", net) or 0))
            native_unit = tx.get("buy_price")
            native_principal = tx.get("investment_total")
            native_gross = tx.get("gross_trade_total")
            trade_to_settle = settlement_fx and (trade_fx / settlement_fx)
            if native_principal is None and native_gross is not None:
                native_principal = (
                    float(native_gross) * trade_to_settle if trade_to_settle else float(native_gross)
                )
            principal_trade = (
                float(native_principal) / trade_to_settle
                if native_principal is not None and trade_to_settle
                else native_principal
            )
            settlement = derive_purchase_settlement(
                gross_quantity=gross,
                net_quantity=net,
                average_buy_price=native_unit,
                investment_total=principal_trade,
                gross_trade_total=native_gross,
            )

            for allocation in allocations:
                try:
                    name = str(allocation.get("participant") or "").strip()
                    quantity = max(0.0, float(allocation.get("quantity") or 0))
                except (TypeError, ValueError, AttributeError):
                    continue
                if not name or quantity <= 1e-12:
                    continue
                ratio = min(1.0, quantity / net)
                key = name.casefold()
                bucket = buckets.setdefault(
                    key,
                    {
                        "participant": name,
                        "records": [],
                        "transaction_cost_total": 0.0,
                        "asset_fee_value": 0.0,
                    },
                )
                principal = (
                    float(native_principal) * settlement_fx * ratio
                    if native_principal is not None
                    else None
                )
                explicit_costs = explicit_costs_native * settlement_fx * ratio
                asset_fee_value = settlement.withheld_asset_value * trade_fx * ratio
                bucket["transaction_cost_total"] += explicit_costs
                bucket["asset_fee_value"] += asset_fee_value
                bucket["records"].append(
                    {
                        **deepcopy(tx),
                        "id": f"{tx.get('id') or ''}:shared:{key}",
                        "type": "buy",
                        "sort_ts": transaction_timestamp(tx),
                        "quantity": quantity,
                        "cash_principal": principal,
                        "explicit_costs": explicit_costs,
                    }
                )

        rows: list[dict[str, Any]] = []
        for bucket in buckets.values():
            ledger = fifo_summary(bucket["records"], current_price=current_price)
            quantity = float(ledger.quantity)
            value = current_price * quantity if current_price is not None else None
            previous_value = previous_price * quantity if previous_price is not None else None
            today_change = (value - previous_value) if value is not None and previous_value is not None else None
            today_pct = (
                (current_price / previous_price - 1.0) * 100.0
                if current_price is not None and previous_price not in (None, 0)
                else None
            )
            cost_basis = ledger.remaining_cost_basis
            unrealized = value - cost_basis if value is not None and cost_basis is not None else None
            realized = 0.0
            total_pnl = unrealized
            pnl_pct = (
                total_pnl / ledger.total_buy_cash * 100.0
                if total_pnl is not None and ledger.total_buy_cash > 0
                else None
            )
            rows.append(
                {
                    "participant": bucket["participant"],
                    "quantity": round(quantity, 12),
                    "value": value,
                    "previous_value": previous_value,
                    "today_change": today_change,
                    "today_pct": today_pct,
                    "cost_basis": cost_basis,
                    "cost_basis_complete": ledger.cost_basis_complete,
                    "transaction_cost_total": bucket["transaction_cost_total"],
                    "asset_fee_value": bucket["asset_fee_value"],
                    "other_cost_total": bucket["transaction_cost_total"] + bucket["asset_fee_value"],
                    "realized_pnl": realized,
                    "unrealized_pnl": unrealized,
                    "pnl": total_pnl,
                    "pnl_pct": pnl_pct,
                    "lifetime_buy_cash": ledger.total_buy_cash,
                    "shared_sell_tracking": False,
                }
            )
        rows.sort(key=lambda row: str(row.get("participant") or "").casefold())
        return rows


    async def async_portfolio(
        self, user_id: str, *, force: bool = False, refresh_market: bool = False
    ) -> dict[str, Any]:
        # An explicit market refresh must never be satisfied by the assembled
        # portfolio cache, even if a caller forgets to also set force=True.
        if refresh_market:
            force = True
        key = ("portfolio", user_id)
        if not force:
            cached = self._cache.get(key, DEFAULT_QUOTE_CACHE_SECONDS)
            if cached is not None:
                return cached
        user = await self.store.async_user(user_id)
        base = user.get("base_currency", "EUR")
        holdings = user.get("holdings", [])
        category_expenses = user.get("category_expenses", {}) or {}
        if refresh_market:
            # Refresh is user intent to discard stale market views. Do not fetch
            # chart history eagerly, but invalidate it so the next trend request
            # also reaches its provider instead of serving a 15-minute snapshot.
            self._cache.clear_prefix(("scope_history", user_id))
            for holding in holdings:
                provider = str(holding.get("provider") or "")
                provider_id = str(holding.get("provider_id") or "")
                if provider and provider_id:
                    self._cache.clear_prefix(("history", provider, provider_id))

        async def enrich(holding: dict[str, Any]) -> dict[str, Any]:
            item = deepcopy(holding)
            try:
                # Manual refresh is intentionally stronger than an ordinary
                # portfolio recompute: bypass the live quote and current FX caches.
                # Post-edit reloads keep refresh_market=False so they stay fast and
                # do not generate unnecessary provider traffic.
                quote = await self._quote(holding, force=refresh_market)
                fx = await self._fx_rate(quote.currency, base, force=refresh_market)
                current_price_base = quote.price * fx
                ledger = await self._ledger_in_currency(
                    holding, base, current_price=current_price_base
                )
                ledger_quote = await self._ledger_in_currency(
                    holding, quote.currency, current_price=quote.price
                )
                quantity = float(ledger.quantity)
                shared_quantity_total = sum(shared_quantity(tx) for tx in (holding.get("transactions") or []))
                previous_price_base = quote.previous_close * fx if quote.previous_close is not None else None
                shared_participants = await self._shared_participant_statistics(
                    holding, base, current_price=current_price_base, previous_price=previous_price_base
                )
                custody_quantity = quantity + shared_quantity_total
                # Keep persisted aggregate quantity self-healing for legacy data,
                # but the immutable transaction ledger is authoritative.
                item["quantity"] = quantity
                value = current_price_base * quantity
                previous_value = (
                    quote.previous_close * quantity * fx if quote.previous_close is not None else None
                )
                today_change = value - previous_value if previous_value is not None else None
                today_pct = (
                    (quote.price / quote.previous_close - 1) * 100
                    if quote.previous_close not in (None, 0)
                    else None
                )

                cost_basis = ledger.remaining_cost_basis
                cost_basis_complete = ledger.cost_basis_complete
                cost_basis_quote = ledger_quote.remaining_cost_basis
                transaction_cost_breakdown, transaction_cost_total = await self._transaction_costs_in_currency(
                    holding, base
                )
                settlement = await self._settlement_summary(holding, base)
                gross_quantity_total = settlement["gross_quantity"]
                asset_fee_quantity_total = settlement["asset_fee_quantity"]
                asset_fee_value = settlement["asset_fee_value"]
                embedded_asset_fee_cost = settlement["embedded_asset_fee_cost"]
                asset_principal = settlement["asset_principal"] if settlement["asset_principal"] else 0.0
                other_cost_total = transaction_cost_total + asset_fee_value
                unit_all_in_cost = (
                    cost_basis_quote / quantity
                    if cost_basis_quote is not None and quantity > 0
                    else None
                )
                unit_pnl = quote.price - unit_all_in_cost if unit_all_in_cost is not None else None
                unit_pnl_pct = (
                    (unit_pnl / unit_all_in_cost * 100)
                    if unit_pnl is not None and unit_all_in_cost not in (None, 0)
                    else None
                )
                unrealized_pnl = value - cost_basis if cost_basis is not None else None
                unrealized_pnl_pct = (
                    unrealized_pnl / cost_basis * 100
                    if unrealized_pnl is not None and cost_basis not in (None, 0)
                    else None
                )
                native_unrealized_pnl = (quote.price * quantity - cost_basis_quote) if cost_basis_quote is not None else None
                asset_return_pct = (native_unrealized_pnl / cost_basis_quote * 100) if native_unrealized_pnl is not None and cost_basis_quote not in (None, 0) else None
                price_effect_base = native_unrealized_pnl * fx if native_unrealized_pnl is not None else None
                currency_effect = (unrealized_pnl - price_effect_base) if unrealized_pnl is not None and price_effect_base is not None else None
                currency_effect_pct = (currency_effect / cost_basis * 100) if currency_effect is not None and cost_basis not in (None, 0) else None
                realized_pnl = ledger.realized_pnl
                total_pnl = (
                    realized_pnl + unrealized_pnl
                    if realized_pnl is not None and unrealized_pnl is not None
                    else (realized_pnl if quantity <= 1e-12 else None)
                )
                lifetime_buy_cash = ledger.total_buy_cash
                total_pnl_pct = (
                    total_pnl / lifetime_buy_cash * 100
                    if total_pnl is not None and lifetime_buy_cash > 0
                    else None
                )
                item.update(
                    {
                        "status": "ok",
                        "price": quote.price,
                        "quote_currency": quote.currency,
                        "base_currency": base,
                        "value": value,
                        "previous_value": previous_value,
                        "today_change": today_change,
                        "today_pct": today_pct,
                        "unit_pnl": unit_pnl,
                        "unit_pnl_pct": unit_pnl_pct,
                        "unit_all_in_cost": unit_all_in_cost,
                        "native_unrealized_pnl": native_unrealized_pnl,
                        "asset_return_pct": asset_return_pct,
                        "price_effect_base": price_effect_base,
                        "currency_effect": currency_effect,
                        "currency_effect_pct": currency_effect_pct,
                        "current_fx_rate": fx,
                        "cost_basis": cost_basis,
                        "cost_basis_complete": cost_basis_complete,
                        "gross_quantity": gross_quantity_total,
                        "net_quantity": quantity,
                        "personal_quantity": quantity,
                        "shared_quantity": shared_quantity_total,
                        "shared_participants": shared_participants,
                        "custody_quantity": custody_quantity,
                        "asset_fee_quantity": asset_fee_quantity_total,
                        "asset_fee_percent": (asset_fee_quantity_total / gross_quantity_total * 100.0) if gross_quantity_total else 0.0,
                        "asset_fee_value": asset_fee_value,
                        "gross_trade_value": settlement["gross_trade_value"],
                        "net_asset_value": settlement["net_asset_value"],
                        "settlement_deduction": settlement["settlement_deduction"],
                        "embedded_asset_fee_cost": embedded_asset_fee_cost,
                        "asset_principal": asset_principal,
                        "other_cost_total": other_cost_total,
                        "transaction_cost_breakdown": transaction_cost_breakdown,
                        "transaction_cost_total": transaction_cost_total,
                        "all_in_cost": cost_basis,
                        "lifetime_buy_cash": lifetime_buy_cash,
                        "total_sell_proceeds": ledger.total_sell_proceeds,
                        "realized_pnl": realized_pnl,
                        "unrealized_pnl": unrealized_pnl,
                        "unrealized_pnl_pct": unrealized_pnl_pct,
                        "transaction_count": len(holding.get("transactions") or []),
                        "ledger_rows": ledger.rows,
                        "pnl": total_pnl,
                        "pnl_pct": total_pnl_pct,
                        "market_time": quote.market_time,
                        "source": quote.source,
                        "delayed": quote.delayed,
                    }
                )
            except Exception as err:
                # Portfolio history is local user data and must remain usable even
                # when the market-data provider is offline. Rebuild the ledger
                # without a live price so dates, BUY/SELL rows, FIFO closures and
                # realized P/L remain visible; only live/unrealized values are unknown.
                try:
                    ledger = await self._ledger_in_currency(holding, base, current_price=None)
                    transaction_cost_breakdown, transaction_cost_total = await self._transaction_costs_in_currency(
                        holding, base
                    )
                    settlement = await self._settlement_summary(holding, base)
                    item.update(
                        {
                            "quantity": float(ledger.quantity),
                            "personal_quantity": float(ledger.quantity),
                            "shared_quantity": sum(shared_quantity(tx) for tx in (holding.get("transactions") or [])),
                            "shared_participants": await self._shared_participant_statistics(
                                holding, base, current_price=None, previous_price=None
                            ),
                            "custody_quantity": float(ledger.quantity) + sum(shared_quantity(tx) for tx in (holding.get("transactions") or [])),
                            "base_currency": base,
                            "cost_basis": ledger.remaining_cost_basis,
                            "cost_basis_complete": ledger.cost_basis_complete,
                            "transaction_cost_breakdown": transaction_cost_breakdown,
                            "transaction_cost_total": transaction_cost_total,
                            "asset_fee_value": settlement["asset_fee_value"],
                            "embedded_asset_fee_cost": settlement["embedded_asset_fee_cost"],
                            "settlement_deduction": settlement["settlement_deduction"],
                            "gross_trade_value": settlement["gross_trade_value"],
                            "asset_principal": settlement["asset_principal"],
                            "other_cost_total": transaction_cost_total + settlement["asset_fee_value"],
                            "lifetime_buy_cash": ledger.total_buy_cash,
                            "total_sell_proceeds": ledger.total_sell_proceeds,
                            "realized_pnl": ledger.realized_pnl,
                            "unrealized_pnl": None,
                            "transaction_count": len(holding.get("transactions") or []),
                            "ledger_rows": ledger.rows,
                        }
                    )
                except Exception as ledger_err:
                    _LOGGER.debug("Could not rebuild local ledger for %s after quote failure: %s", holding.get("id"), ledger_err)
                item.update({"status": "error", "error": str(err), "base_currency": base})
            return item

        enriched = await asyncio.gather(*(enrich(h) for h in holdings)) if holdings else []
        categories: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "value": 0.0, "today_change": 0.0, "today_current": 0.0,
                "today_previous": 0.0, "count": 0, "has_today": False,
                "calculated_cost_basis": 0.0, "cost_basis_complete": True,
                "transaction_cost_breakdown": {key: 0.0 for key in TRANSACTION_COST_TYPES},
                "transaction_cost_total": 0.0,
                "asset_fee_value": 0.0,
                "embedded_asset_fee_cost": 0.0,
                "settlement_deduction": 0.0,
                "gross_trade_value": 0.0,
                "asset_principal": 0.0,
                "lifetime_buy_cash": 0.0,
                "total_sell_proceeds": 0.0,
                "realized_pnl": 0.0,
                "realized_complete": True,
                "unrealized_pnl": 0.0,
                "unrealized_complete": True,
            }
        )
        total = 0.0
        previous_total = 0.0
        comparable_current_total = 0.0
        has_previous = False
        for item in enriched:
            cat_name = item.get("category") or "other"
            cat = categories[cat_name]
            cat["count"] += 1

            # Local ledger/cost information remains valid when a market-data
            # provider is temporarily unavailable. Keep those holdings visible
            # and keep their expenses/cost basis in category and portfolio
            # accounting; only live market-value/today calculations are skipped.
            if item.get("cost_basis") is None:
                cat["cost_basis_complete"] = False
            else:
                cat["calculated_cost_basis"] += float(item["cost_basis"])
            cat["transaction_cost_total"] += float(item.get("transaction_cost_total") or 0)
            cat["asset_fee_value"] += float(item.get("asset_fee_value") or 0)
            cat["embedded_asset_fee_cost"] += float(item.get("embedded_asset_fee_cost") or 0)
            cat["settlement_deduction"] += float(item.get("settlement_deduction") or 0)
            cat["gross_trade_value"] += float(item.get("gross_trade_value") or 0)
            cat["asset_principal"] += float(item.get("asset_principal") or 0)
            cat["lifetime_buy_cash"] += float(item.get("lifetime_buy_cash") or 0)
            cat["total_sell_proceeds"] += float(item.get("total_sell_proceeds") or 0)
            if item.get("realized_pnl") is None:
                cat["realized_complete"] = False
            else:
                cat["realized_pnl"] += float(item.get("realized_pnl") or 0)
            if item.get("unrealized_pnl") is None:
                cat["unrealized_complete"] = False
            else:
                cat["unrealized_pnl"] += float(item.get("unrealized_pnl") or 0)
            for cost_type in TRANSACTION_COST_TYPES:
                cat["transaction_cost_breakdown"][cost_type] += float(
                    (item.get("transaction_cost_breakdown") or {}).get(cost_type) or 0
                )

            if item.get("status") != "ok":
                continue

            value = float(item.get("value") or 0)
            total += value
            cat["value"] += value
            if item.get("previous_value") is not None:
                previous_total += float(item["previous_value"])
                comparable_current_total += value
                has_previous = True
                cat["today_change"] += float(item.get("today_change") or 0)
                cat["today_current"] += value
                cat["today_previous"] += float(item["previous_value"])
                cat["has_today"] = True

        # Preserve manually-entered category expenses even when a category currently has no holdings.
        for cat_name in category_expenses:
            categories[cat_name]

        total_today = comparable_current_total - previous_total if has_previous else None
        total_today_pct = (comparable_current_total / previous_total - 1) * 100 if has_previous and previous_total else None
        cats_out = []
        grand_cost_basis = 0.0
        grand_transaction_cost_total = 0.0
        grand_asset_fee_value = 0.0
        grand_embedded_asset_fee_cost = 0.0
        grand_settlement_deduction = 0.0
        grand_gross_trade_value = 0.0
        grand_asset_principal = 0.0
        grand_lifetime_buy_cash = 0.0
        grand_total_sell_proceeds = 0.0
        grand_realized_pnl = 0.0
        grand_realized_complete = True
        grand_unrealized_pnl = 0.0
        grand_unrealized_complete = True
        grand_transaction_cost_breakdown = {key: 0.0 for key in TRANSACTION_COST_TYPES}
        grand_cost_basis_complete = True
        for name, cat in categories.items():
            cat_today_pct = None
            if cat["has_today"] and cat["today_previous"]:
                cat_today_pct = (cat["today_current"] / cat["today_previous"] - 1) * 100
            manual_expense = category_expenses.get(name)
            if manual_expense is not None:
                cost_basis = float(manual_expense)
                source = "manual"
            elif cat["cost_basis_complete"]:
                cost_basis = float(cat["calculated_cost_basis"])
                source = "holdings"
            else:
                cost_basis = None
                source = None
            transaction_cost_total = float(cat["transaction_cost_total"])
            embedded_asset_fee_cost = float(cat.get("embedded_asset_fee_cost") or 0)
            asset_principal = float(cat.get("asset_principal") or 0)
            other_cost_total = transaction_cost_total + float(cat.get("asset_fee_value") or 0)
            # FIFO cost basis already contains purchase-side cash fees allocated
            # to the units still owned; do not add them a second time here.
            all_in_cost = cost_basis
            realized_pnl = float(cat["realized_pnl"]) if cat["realized_complete"] else None
            if manual_expense is not None:
                unrealized_pnl = float(cat["value"]) - float(manual_expense)
            else:
                unrealized_pnl = float(cat["unrealized_pnl"]) if cat["unrealized_complete"] else None
            pnl = (
                realized_pnl + unrealized_pnl
                if realized_pnl is not None and unrealized_pnl is not None
                else None
            )
            lifetime_buy_cash = float(cat.get("lifetime_buy_cash") or 0)
            pnl_pct = (pnl / lifetime_buy_cash * 100) if pnl is not None and lifetime_buy_cash else None
            if all_in_cost is None:
                grand_cost_basis_complete = False
            else:
                grand_cost_basis += cost_basis or 0.0
            grand_transaction_cost_total += transaction_cost_total
            grand_asset_fee_value += float(cat.get("asset_fee_value") or 0)
            grand_embedded_asset_fee_cost += embedded_asset_fee_cost
            grand_settlement_deduction += float(cat.get("settlement_deduction") or 0)
            grand_gross_trade_value += float(cat.get("gross_trade_value") or 0)
            grand_asset_principal += asset_principal
            grand_lifetime_buy_cash += float(cat.get("lifetime_buy_cash") or 0)
            grand_total_sell_proceeds += float(cat.get("total_sell_proceeds") or 0)
            if realized_pnl is None:
                grand_realized_complete = False
            else:
                grand_realized_pnl += realized_pnl
            if unrealized_pnl is None:
                grand_unrealized_complete = False
            else:
                grand_unrealized_pnl += unrealized_pnl
            for cost_type in TRANSACTION_COST_TYPES:
                grand_transaction_cost_breakdown[cost_type] += float(
                    cat["transaction_cost_breakdown"].get(cost_type) or 0
                )
            cats_out.append(
                {
                    "category": name,
                    "value": cat["value"],
                    "today_change": cat["today_change"] if cat["has_today"] else None,
                    "today_pct": cat_today_pct,
                    "manual_expense": float(manual_expense) if manual_expense is not None else None,
                    "calculated_cost_basis": cat["calculated_cost_basis"] if cat["cost_basis_complete"] else None,
                    "cost_basis": cost_basis,
                    "cost_basis_source": source,
                    "transaction_cost_breakdown": cat["transaction_cost_breakdown"],
                    "transaction_cost_total": transaction_cost_total,
                    "asset_fee_value": float(cat.get("asset_fee_value") or 0),
                    "embedded_asset_fee_cost": embedded_asset_fee_cost,
                    "settlement_deduction": float(cat.get("settlement_deduction") or 0),
                    "gross_trade_value": float(cat.get("gross_trade_value") or 0),
                    "asset_principal": asset_principal,
                    "other_cost_total": other_cost_total,
                    "all_in_cost": all_in_cost,
                    "lifetime_buy_cash": lifetime_buy_cash,
                    "total_sell_proceeds": float(cat.get("total_sell_proceeds") or 0),
                    "realized_pnl": realized_pnl,
                    "unrealized_pnl": unrealized_pnl,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "count": cat["count"],
                }
            )
        cats_out.sort(key=lambda x: x["value"], reverse=True)
        grand_all_in_cost = grand_cost_basis if grand_cost_basis_complete else None
        total_realized_pnl = grand_realized_pnl if grand_realized_complete else None
        total_unrealized_pnl = grand_unrealized_pnl if grand_unrealized_complete else None
        total_pnl = (
            total_realized_pnl + total_unrealized_pnl
            if total_realized_pnl is not None and total_unrealized_pnl is not None
            else None
        )
        total_pnl_pct = (
            total_pnl / grand_lifetime_buy_cash * 100
            if total_pnl is not None and grand_lifetime_buy_cash
            else None
        )
        result = {
            "base_currency": base,
            "language": str(user.get("language") or DEFAULT_UI_LANGUAGE),
            "incognito": bool(user.get("incognito", False)),
            "incognito_reveal_seconds": int(user.get("incognito_reveal_seconds", DEFAULT_INCOGNITO_REVEAL_SECONDS)),
            "exposed_entities": list(user.get("exposed_entities") or []),
            "total": total,
            "today_change": total_today,
            "today_pct": total_today_pct,
            "cost_basis": grand_cost_basis if grand_cost_basis_complete else None,
            "transaction_cost_breakdown": grand_transaction_cost_breakdown,
            "transaction_cost_total": grand_transaction_cost_total,
            "asset_fee_value": grand_asset_fee_value,
            "embedded_asset_fee_cost": grand_embedded_asset_fee_cost,
            "settlement_deduction": grand_settlement_deduction,
            "gross_trade_value": grand_gross_trade_value,
            "asset_principal": grand_asset_principal if grand_cost_basis_complete else None,
            "other_cost_total": grand_transaction_cost_total + grand_asset_fee_value,
            "all_in_cost": grand_all_in_cost,
            "lifetime_buy_cash": grand_lifetime_buy_cash,
            "total_sell_proceeds": grand_total_sell_proceeds,
            "realized_pnl": total_realized_pnl,
            "unrealized_pnl": total_unrealized_pnl,
            "pnl": total_pnl,
            "pnl_pct": total_pnl_pct,
            "categories": cats_out,
            "holdings": enriched,
            "updated_at": int(time.time()),
            "market_refreshed": bool(refresh_market),
        }
        self._cache.set(key, result)
        return result

    @staticmethod
    def _candidate_key(asset: dict[str, Any]) -> tuple[str, str]:
        return str(asset.get("provider") or ""), str(asset.get("provider_id") or "")


    async def _history(self, holding: dict[str, Any], period: str) -> list[HistoryPoint]:
        key = ("history", holding["provider"], holding["provider_id"], period)
        cached = self._cache.get(key, DEFAULT_HISTORY_CACHE_SECONDS)
        if cached is not None:
            return [HistoryPoint(**x) for x in cached]
        provider_name = str(holding.get("provider") or "")
        provider = self.providers.get(provider_name)
        if provider is None:
            if provider_name not in {"twelve_data", "alpha_vantage"}:
                raise ProviderError(f"Unsupported provider {provider_name}")
            points = await self._fallback_paid_history(holding, period, ProviderError(f"{provider_name} is not configured"))
        else:
            try:
                async with self._network_sem:
                    points = list(await provider.async_history(holding["provider_id"], period))
            except Exception as first_err:
                if provider_name == "yahoo":
                    async with self._network_sem:
                        points = list(await self.stooq.async_history(holding["provider_id"], period))
                elif provider_name == "kraken":
                    # Kraken's OHLC endpoint is intentionally bounded. Use Yahoo's no-key
                    # chart history for long crypto ranges when a conventional pair exists.
                    symbol = holding.get("symbol", "")
                    if "/" not in symbol:
                        raise first_err
                    base, quote = symbol.split("/", 1)
                    async with self._network_sem:
                        points = list(await self.yahoo.async_history(f"{base}-{quote}", period))
                elif provider_name in {"twelve_data", "alpha_vantage"}:
                    points = await self._fallback_paid_history(holding, period, first_err)
                else:
                    raise first_err
        self._cache.set(key, [x.as_dict() for x in points])
        return points

    async def _fallback_paid_history(
        self, holding: dict[str, Any], period: str, first_err: Exception
    ) -> list[HistoryPoint]:
        """Use existing no-key history as a continuity fallback for paid holdings."""
        symbol = str(holding.get("symbol") or "").strip()
        if not symbol:
            raise ProviderError(str(first_err)) from first_err
        yahoo_symbol = symbol.replace("/", "-") if str(holding.get("category") or "") == "crypto" else symbol
        try:
            async with self._network_sem:
                return list(await self.yahoo.async_history(yahoo_symbol, period))
        except Exception:
            try:
                async with self._network_sem:
                    return list(await self.stooq.async_history(symbol, period))
            except Exception:
                raise ProviderError(str(first_err)) from first_err

    async def _convert_history(
        self, points: list[HistoryPoint], currency: str, base: str, period: str
    ) -> list[HistoryPoint]:
        norm_currency, scale = self._normalize_currency(currency)
        norm_base, _ = self._normalize_currency(base)
        if norm_currency == norm_base:
            return [HistoryPoint(p.ts, p.value * scale) for p in points]
        try:
            fx_points = list(await self.frankfurter.async_history(f"{norm_currency}/{norm_base}", period))
        except Exception as err:
            # Never paint a historical foreign-currency chart using today's FX.
            # If the historical series is unavailable, let this holding's trend
            # fail cleanly while the local transaction ledger remains visible.
            raise ProviderError(f"Historical FX unavailable for {norm_currency}/{norm_base}: {err}") from err
        fx_points.sort(key=lambda x: x.ts)
        out: list[HistoryPoint] = []
        idx = 0
        last_rate = fx_points[0].value if fx_points else 1.0
        for point in sorted(points, key=lambda x: x.ts):
            while idx < len(fx_points) and fx_points[idx].ts <= point.ts:
                last_rate = fx_points[idx].value
                idx += 1
            out.append(HistoryPoint(point.ts, point.value * last_rate * scale))
        return out

    async def async_scope_history(
        self, user_id: str, scope: str, scope_id: str | None, period: str
    ) -> dict[str, Any]:
        key = ("scope_history", user_id, scope, scope_id or "", period)
        cached = self._cache.get(key, DEFAULT_HISTORY_CACHE_SECONDS)
        if cached is not None:
            return cached
        user = await self.store.async_user(user_id)
        base = user.get("base_currency", "EUR")
        all_holdings = user.get("holdings", [])
        if scope == "holding":
            holdings = [h for h in all_holdings if h["id"] == scope_id]
        elif scope == "category":
            holdings = [h for h in all_holdings if h.get("category") == scope_id]
        elif scope == "portfolio":
            holdings = all_holdings
        else:
            raise ValueError("Invalid scope")
        if not holdings:
            return {"period": period, "scope": scope, "scope_id": scope_id, "currency": base, "points": []}

        async def series_for(holding: dict[str, Any]) -> list[HistoryPoint]:
            points = await self._history(holding, period)
            quote = await self._quote(holding)
            converted = await self._convert_history(points, quote.currency, base, period)
            transactions = holding.get("transactions") or []
            if not transactions:
                quantity = float(holding.get("quantity") or 0)
                return [HistoryPoint(p.ts, p.value * quantity) for p in converted]
            return [
                HistoryPoint(p.ts, p.value * quantity_at(transactions, p.ts))
                for p in converted
            ]

        series = await asyncio.gather(*(series_for(h) for h in holdings), return_exceptions=True)
        valid = [s for s in series if isinstance(s, list) and s]
        if not valid:
            result = {"period": period, "scope": scope, "scope_id": scope_id, "currency": base, "points": []}
            self._cache.set(key, result)
            return result
        if len(valid) == 1:
            points = valid[0]
        else:
            points = self._aggregate_series(valid, period)
        result = {
            "period": period,
            "scope": scope,
            "scope_id": scope_id,
            "currency": base,
            "points": [p.as_dict() for p in points],
        }
        self._cache.set(key, result)
        return result

    @staticmethod
    def _aggregate_series(series: list[list[HistoryPoint]], period: str) -> list[HistoryPoint]:
        bucket_seconds = {
            "1d": 15 * 60,
            "7d": 6 * 3600,
            "1m": 24 * 3600,
            "3m": 24 * 3600,
            "1y": 7 * 24 * 3600,
            "5y": 30 * 24 * 3600,
        }.get(period, 24 * 3600)
        bucketed: list[dict[int, float]] = []
        all_buckets: set[int] = set()
        for points in series:
            mapping: dict[int, float] = {}
            for point in points:
                bucket = point.ts - (point.ts % bucket_seconds)
                mapping[bucket] = point.value
                all_buckets.add(bucket)
            bucketed.append(mapping)
        latest: list[float | None] = [None] * len(bucketed)
        out: list[HistoryPoint] = []
        for bucket in sorted(all_buckets):
            for idx, mapping in enumerate(bucketed):
                if bucket in mapping:
                    latest[idx] = mapping[bucket]
            values = [x for x in latest if x is not None and math.isfinite(x)]
            if values:
                out.append(HistoryPoint(bucket, sum(values)))
        return out
