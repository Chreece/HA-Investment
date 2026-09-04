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
    DEFAULT_HISTORY_CACHE_SECONDS,
    DEFAULT_INDICATION_PREFERENCES,
    DEFAULT_INCOGNITO_REVEAL_SECONDS,
    DEFAULT_INDICATION_LEGAL_REGION,
    DEFAULT_QUOTE_CACHE_SECONDS,
    DEFAULT_SEARCH_CACHE_SECONDS,
    DEFAULT_UI_LANGUAGE,
    EXPOSABLE_ENTITY_METRICS,
    INDICATION_DISCLAIMER_VERSION,
    INDICATION_LEGAL_REGIONS,
    MAX_INCOGNITO_REVEAL_SECONDS,
    SIGNAL_ENTITY_EXPOSURE_CHANGED,
    SUPPORTED_UI_LANGUAGES,
    TRANSACTION_COST_TYPES,
)
from .indication import (
    DIVERSIFICATION_DEFAULT_MAX_FRACTION,
    allocate_budget,
    analyze_prices,
    attach_relative_strength,
    balanced_discovery_sample,
    sanitize_ai_ranking,
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
from .providers import FrankfurterProvider, KrakenProvider, ProviderError, StooqProvider, YahooProvider
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

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.store = InvestmentStore(hass)
        self.yahoo = YahooProvider(hass)
        self.kraken = KrakenProvider(hass)
        self.frankfurter = FrankfurterProvider(hass)
        self.stooq = StooqProvider(hass)
        self.providers = {
            self.yahoo.provider_id: self.yahoo,
            self.kraken.provider_id: self.kraken,
            self.frankfurter.provider_id: self.frankfurter,
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

        chunks = await asyncio.gather(
            run(self.yahoo), run(self.kraken), run(self.frankfurter)
        )
        raw_results: list[dict[str, Any]] = []
        for chunk in chunks:
            raw_results.extend(item.as_dict() for item in chunk)
        # Prefer dedicated no-key crypto/FX feeds, then Yahoo for broad discovery.
        raw_results.sort(
            key=lambda x: (
                {"kraken": 0, "frankfurter": 0, "yahoo": 1}.get(x["provider"], 9),
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

    @staticmethod
    def _validated_indication_preferences(raw: dict[str, Any] | None) -> dict[str, Any]:
        """Normalize persisted indication options using the same public constraints as the engine."""
        values = dict(DEFAULT_INDICATION_PREFERENCES)
        if isinstance(raw, dict):
            values.update({key: raw[key] for key in DEFAULT_INDICATION_PREFERENCES if key in raw})
        if values["scope"] not in {"discover", "portfolio", "search"}:
            raise ValueError("Unsupported indication candidate scope")
        if values["mode"] not in {"deterministic", "deterministic_ai", "full_ai"}:
            raise ValueError("Unsupported indication mode")
        if values["risk_tolerance"] not in {"very_low", "low", "medium", "high", "very_high"}:
            raise ValueError("Unsupported risk tolerance")
        if values["horizon"] not in {"very_short", "short", "medium", "long", "very_long"}:
            raise ValueError("Unsupported investment horizon")
        if values["strategy"] not in {"adaptive", "balanced", "momentum", "trend", "risk_adjusted", "pullback"}:
            raise ValueError("Unsupported indication strategy")
        if values["overlap_policy"] not in {"allow", "penalize", "exclude"}:
            raise ValueError("Unsupported portfolio overlap policy")
        if values["diversification"] not in {"low", "medium", "high"}:
            raise ValueError("Unsupported diversification preference")
        category = values.get("category")
        if category is None or category == "":
            values["category"] = None
        elif category not in {"crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"}:
            raise ValueError("Unsupported investment category")
        entity = str(values.get("ai_task_entity_id") or "").strip()
        if entity and not entity.startswith("ai_task."):
            raise ValueError("AI Task entity must use the ai_task domain")
        values["ai_task_entity_id"] = entity or None
        amount = values.get("amount")
        if amount is None or amount == "":
            values["amount"] = None
        else:
            amount = float(amount)
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("Investment amount must be zero or greater")
            values["amount"] = amount
        for key, label in (("overlap_threshold_pct", "Overlap threshold"), ("min_confidence_pct", "Minimum confidence"), ("min_cash_reserve_pct", "Minimum cash reserve")):
            value = float(values[key])
            if not math.isfinite(value) or value < 0 or value > 100:
                raise ValueError(f"{label} must be between 0 and 100")
            values[key] = value
        cap = values.get("max_candidate_pct")
        if cap is None or cap == "":
            values["max_candidate_pct"] = None
        else:
            cap = float(cap)
            if not math.isfinite(cap) or cap <= 0 or cap > 100:
                raise ValueError("Maximum candidate allocation must be greater than zero and at most 100")
            values["max_candidate_pct"] = cap
        values["whole_units_only"] = bool(values.get("whole_units_only", False))
        return values

    async def async_set_preferences(
        self,
        user_id: str,
        *,
        base_currency: str | None = None,
        language: str | None = None,
        incognito: bool | None = None,
        incognito_reveal_seconds: int | None = None,
        developer_indicator_unlocked: bool | None = None,
        indication_preferences: dict[str, Any] | None = None,
        exposed_entities: list[str] | None = None,
        indication_disclaimer_version: int | None = None,
        indication_disclaimer_region: str | None = None,
        indication_disclaimer_language: str | None = None,
    ) -> dict[str, Any]:
        """Update private per-user display, indication, automation and legal preferences."""
        if base_currency is None and language is None and incognito is None and incognito_reveal_seconds is None and developer_indicator_unlocked is None and indication_preferences is None and exposed_entities is None and indication_disclaimer_version is None and indication_disclaimer_region is None and indication_disclaimer_language is None:
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
                raise ValueError(f"Incognito reveal duration must be between 0 and {MAX_INCOGNITO_REVEAL_SECONDS} seconds")
        normalized_indication = None if indication_preferences is None else self._validated_indication_preferences(indication_preferences)
        normalized_disclaimer = None
        normalized_disclaimer_region = None
        normalized_disclaimer_language = None
        if indication_disclaimer_version is not None:
            try:
                normalized_disclaimer = int(indication_disclaimer_version)
            except (TypeError, ValueError) as err:
                raise ValueError("Invalid indication disclaimer version") from err
            if normalized_disclaimer != INDICATION_DISCLAIMER_VERSION:
                raise ValueError("The current investment indication legal terms must be acknowledged")
            normalized_disclaimer_region = str(indication_disclaimer_region or "").strip().lower()
            if normalized_disclaimer_region not in INDICATION_LEGAL_REGIONS:
                raise ValueError("A supported legal region must be selected before accepting the investment indication terms")
            normalized_disclaimer_language = str(indication_disclaimer_language or language or DEFAULT_UI_LANGUAGE).strip().lower()
            if normalized_disclaimer_language != DEFAULT_UI_LANGUAGE and normalized_disclaimer_language not in SUPPORTED_UI_LANGUAGES:
                raise ValueError("Unsupported legal-terms language")
        elif indication_disclaimer_region is not None or indication_disclaimer_language is not None:
            raise ValueError("Legal region/language can only be stored together with acceptance of the current terms")
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
            developer_indicator_unlocked=developer_indicator_unlocked,
            indication_preferences=normalized_indication,
            exposed_entities=normalized_exposed,
            indication_disclaimer_version=normalized_disclaimer,
            indication_disclaimer_region=normalized_disclaimer_region,
            indication_disclaimer_language=normalized_disclaimer_language,
        )
        self._cache.clear_prefix(("portfolio", user_id))
        self._cache.clear_prefix(("scope_history", user_id))
        if normalized_exposed is not None and previous_exposed != normalized_exposed:
            async_dispatcher_send(self.hass, SIGNAL_ENTITY_EXPOSURE_CHANGED, user_id, tuple(normalized_exposed))
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
        quote = await self._quote({"provider": provider_name, "provider_id": provider_id})
        if requested_currency and not quote_currency_matches(quote.currency, requested_currency):
            raise ValueError(f"Quote currency {quote.currency} does not match selected asset currency {requested_currency}")
        return quote.as_dict()

    async def _quote(self, holding: dict[str, Any], *, force: bool = False) -> Quote:
        key = ("quote", holding["provider"], holding["provider_id"])
        if not force:
            cached = self._cache.get(key, DEFAULT_QUOTE_CACHE_SECONDS)
            if cached is not None:
                return Quote(**cached)
        provider = self.providers[holding["provider"]]
        try:
            async with self._network_sem:
                quote = await provider.async_quote(holding["provider_id"])
        except Exception as err:
            if holding["provider"] == "yahoo":
                try:
                    async with self._network_sem:
                        quote = await self.stooq.async_quote(holding["provider_id"])
                except Exception:
                    raise ProviderError(str(err)) from err
            else:
                raise
        self._cache.set(key, quote.as_dict())
        return quote

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
            "developer_indicator_unlocked": bool(user.get("developer_indicator_unlocked", False)),
            "indication_disclaimer_version": int(user.get("indication_disclaimer_version") or 0),
            "indication_disclaimer_accepted_at": user.get("indication_disclaimer_accepted_at"),
            "indication_disclaimer_region": user.get("indication_disclaimer_region"),
            "indication_disclaimer_language": user.get("indication_disclaimer_language"),
            "required_indication_disclaimer_version": INDICATION_DISCLAIMER_VERSION,
            "supported_indication_legal_regions": list(INDICATION_LEGAL_REGIONS),
            "indication_preferences": deepcopy(user.get("indication_preferences") or DEFAULT_INDICATION_PREFERENCES),
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

    @staticmethod
    def _ai_speech(payload: dict[str, Any]) -> str:
        """Extract plain speech from a Home Assistant Conversation result."""
        response = payload.get("response") or {}
        speech = response.get("speech") or {}
        plain = speech.get("plain") or {}
        value = plain.get("speech")
        if isinstance(value, str):
            return value.strip()
        if isinstance(speech, str):
            return speech.strip()
        return ""

    @staticmethod
    def _json_from_ai_text(text: str) -> dict[str, Any] | None:
        """Best-effort parse of a JSON object from an AI response."""
        raw = str(text or "").strip()
        if not raw:
            return None
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, re.I | re.S)
        candidates = [fenced.group(1)] if fenced else []
        candidates.append(raw)
        brace = re.search(r"(\{.*\})", raw, re.S)
        if brace:
            candidates.append(brace.group(1))
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    async def _ai_indication_review(
        self,
        user_id: str,
        *,
        mode: str,
        ai_task_entity_id: str | None,
        language: str,
        amount: float | None,
        category: str | None,
        risk_tolerance: str,
        horizon: str,
        strategy: str,
        overlap_policy: str,
        diversification: str,
        max_candidate_pct: float | None,
        min_confidence_pct: float,
        min_cash_reserve_pct: float,
        whole_units_only: bool,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Ask Home Assistant AI Task to review/rank supplied deterministic data.

        When ``ai_task_entity_id`` is omitted Home Assistant's preferred AI Task
        entity is intentionally used. The model sees only already-evaluated
        candidates and their metrics; execution/budget constraints remain local.
        """
        from homeassistant.core import Context

        if not self.hass.services.has_service("ai_task", "generate_data"):
            raise ValueError("Home Assistant AI Task generate_data is unavailable")

        compact = []
        for item in results[:20]:
            compact.append(
                {
                    "provider": item.get("provider"),
                    "provider_id": item.get("provider_id"),
                    "symbol": item.get("symbol"),
                    "name": item.get("name"),
                    "category": item.get("category"),
                    "price": item.get("price"),
                    "quote_currency": item.get("quote_currency"),
                    "portfolio_price": item.get("portfolio_price"),
                    "deterministic_score": item.get("score"),
                    "confidence": item.get("confidence"),
                    "metrics": item.get("metrics"),
                    "portfolio_overlap": item.get("portfolio_overlap"),
                    "portfolio_overlap_known": item.get("portfolio_overlap_known"),
                    "portfolio_overlap_sources": item.get("portfolio_overlap_sources"),
                    "reasons": item.get("reasons"),
                    "warnings": item.get("warnings"),
                }
            )
        data_json = json.dumps(compact, separators=(",", ":"), ensure_ascii=False)
        max_candidate_text = f"{max_candidate_pct:.2f}%" if max_candidate_pct is not None else "automatic from diversification"
        preferences = (
            f"risk={risk_tolerance}; intended_holding_period={horizon}; strategy={strategy}; "
            f"overlap_policy={overlap_policy}; diversification={diversification}; "
            f"max_per_candidate={max_candidate_text}; minimum_confidence={min_confidence_pct:.2f}%; "
            f"minimum_cash_reserve={min_cash_reserve_pct:.2f}%; whole_units_only={bool(whole_units_only)}"
        )
        if mode == "deterministic_ai":
            task = (
                "You are the second-pass reviewer of a deterministic investment indication. "
                "Do NOT change its deterministic ranking or scores. Check whether the supplied evidence is "
                "consistent with the user's risk tolerance and intended holding period, and flag overlap, "
                "concentration, volatility, drawdown, regime, or data-quality concerns. Use only supplied data; "
                "do not invent news, fundamentals or forecasts. Return JSON only with keys verdict "
                "(approve|caution|reject), summary, and notes (array of short strings)."
            )
        else:
            task = (
                "You are the full-AI ranking layer of an investment indication. Rank only the supplied candidates "
                "using their supplied market metrics, portfolio exposure and overlap evidence, while respecting the "
                "user preferences. Do not invent news, fundamentals, financial statements, forecasts or candidates. "
                "Return JSON only with keys summary and ranking. ranking is an array with provider, provider_id, "
                "score (0-100), action (consider|watch|avoid), suggested_amount (number or null), and reason."
            )
        prompt = (
            f"{task}\nUSER_PREFERENCES={preferences}.\n"
            f"Portfolio budget: {amount if amount is not None else 'not supplied'}; "
            f"category filter: {category or 'any'}.\nCANDIDATES={data_json}"
        )
        service_data: dict[str, Any] = {
            "task_name": "HA Investment indication review" if mode == "deterministic_ai" else "HA Investment indication ranking",
            "instructions": prompt,
        }
        selected = str(ai_task_entity_id or "").strip()
        if selected:
            if not selected.startswith("ai_task."):
                raise ValueError("AI Task entity must use the ai_task domain")
            service_data["entity_id"] = selected
        response = await self.hass.services.async_call(
            "ai_task",
            "generate_data",
            service_data,
            blocking=True,
            return_response=True,
            context=Context(user_id=user_id),
        )
        generated = (response or {}).get("data") if isinstance(response, dict) else None
        if isinstance(generated, dict):
            parsed = generated
            text = json.dumps(generated, ensure_ascii=False)
        else:
            text = str(generated or "").strip()
            parsed = self._json_from_ai_text(text)
        return {
            "text": text,
            "structured": parsed,
            "entity_id": selected or None,
            "uses_preferred_entity": not bool(selected),
        }

    @staticmethod
    def _overlap_symbol(value: Any) -> str:
        return str(value or "").strip().upper()

    @classmethod
    def _overlap_aliases(cls, *values: Any) -> set[str]:
        """Return conservative ticker aliases for constituent matching.

        Yahoo search symbols can include an exchange suffix (for example
        ``SAP.DE``) while a fund constituent list can expose the same holding as
        ``SAP``. Exact symbols remain preferred; only a final dot suffix is
        stripped as a secondary alias to avoid broad name-based guessing.
        """
        aliases: set[str] = set()
        for value in values:
            raw = cls._overlap_symbol(value)
            if not raw:
                continue
            aliases.add(raw)
            if "." in raw:
                base, suffix = raw.rsplit(".", 1)
                if base and 1 <= len(suffix) <= 4 and suffix.isalnum():
                    aliases.add(base)
        return aliases

    async def _fund_top_holdings(self, asset: dict[str, Any]) -> list[dict[str, Any]]:
        if str(asset.get("category") or "") not in {"etf", "fund"}:
            return []
        provider = self.providers.get(str(asset.get("provider") or ""))
        getter = getattr(provider, "async_fund_top_holdings", None) if provider else None
        if not callable(getter):
            return []
        try:
            async with self._network_sem:
                rows = await getter(str(asset.get("provider_id") or ""))
            return [dict(row) for row in rows if isinstance(row, dict)]
        except Exception as err:
            _LOGGER.debug("Fund holdings unavailable for %s: %s", asset.get("provider_id"), err)
            return []

    async def _owned_fund_holdings(self, live_holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        funds = [h for h in live_holdings if str(h.get("category") or "") in {"etf", "fund"}]
        if not funds:
            return []
        fetched = await asyncio.gather(*(self._fund_top_holdings(fund) for fund in funds))
        return [
            {"asset": fund, "holdings": rows, "known": bool(rows)}
            for fund, rows in zip(funds, fetched, strict=True)
        ]

    async def _candidate_portfolio_overlap(
        self,
        asset: dict[str, Any],
        owned_funds: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Estimate known overlap with ETFs/funds already in the portfolio."""
        category = str(asset.get("category") or "other")
        if category not in {"stock", "etf", "fund"} or not owned_funds:
            return {"known": False, "matched": False, "overlap": 0.0, "sources": [], "kind": None, "method": None}

        if category == "stock":
            candidate_aliases = self._overlap_aliases(asset.get("provider_id"), asset.get("symbol"))
            sources: list[dict[str, Any]] = []
            any_known = False
            for record in owned_funds:
                rows = record.get("holdings") or []
                any_known = any_known or bool(rows)
                for row in rows:
                    if not (candidate_aliases & self._overlap_aliases(row.get("symbol"))):
                        continue
                    weight = max(0.0, min(1.0, float(row.get("weight") or 0.0)))
                    fund = record.get("asset") or {}
                    sources.append({"symbol": fund.get("symbol"), "name": fund.get("name"), "weight": weight})
                    break
            return {
                "known": any_known,
                "matched": bool(sources),
                "overlap": max((float(row.get("weight") or 0.0) for row in sources), default=0.0),
                "sources": sources,
                "kind": "stock_in_owned_fund" if sources else None,
                "method": "yahoo_top_holdings",
            }

        candidate_rows = await self._fund_top_holdings(asset)
        if not candidate_rows:
            return {"known": False, "matched": False, "overlap": 0.0, "sources": [], "kind": "fund_to_fund", "method": "yahoo_top_holdings"}
        candidate_map = {
            self._overlap_symbol(row.get("symbol")): max(0.0, min(1.0, float(row.get("weight") or 0.0)))
            for row in candidate_rows
            if self._overlap_symbol(row.get("symbol"))
        }
        candidate_symbols = set(candidate_map)
        best = 0.0
        sources: list[dict[str, Any]] = []
        candidate_key = self._candidate_key(asset)
        for record in owned_funds:
            fund = record.get("asset") or {}
            if self._candidate_key(fund) == candidate_key:
                continue
            rows = record.get("holdings") or []
            if not rows:
                continue
            owned_map = {
                self._overlap_symbol(row.get("symbol")): max(0.0, min(1.0, float(row.get("weight") or 0.0)))
                for row in rows
                if self._overlap_symbol(row.get("symbol"))
            }
            common = candidate_symbols & set(owned_map)
            if not common:
                continue
            weighted = sum(min(candidate_map[symbol], owned_map[symbol]) for symbol in common)
            # If a provider reports symbols but omits weights, retain a bounded
            # top-holdings set overlap rather than falsely reporting zero.
            if weighted <= 1e-12:
                weighted = len(common) / max(1, min(len(candidate_map), len(owned_map)))
            weighted = max(0.0, min(1.0, weighted))
            best = max(best, weighted)
            sources.append({"symbol": fund.get("symbol"), "name": fund.get("name"), "overlap": weighted})
        return {
            "known": True,
            "matched": bool(sources),
            "overlap": best,
            "sources": sources,
            "kind": "fund_to_fund",
            "method": "yahoo_top_holdings",
        }

    async def _discover_indication_candidates(
        self, base_currency: str, category: str | None
    ) -> tuple[list[dict[str, Any]], int]:
        """Build a bounded, category-balanced discovery universe.

        Discovery is intentionally independent from the user's holdings. The
        portfolio is consulted later only for concentration/diversification
        penalties. For broad discovery, candidates are round-robin sampled from
        supported categories so one large equity feed cannot crowd out every
        ETF/crypto/FX candidate before deterministic scoring begins.
        """
        categories = (category,) if category else (
            "etf", "stock", "crypto", "fund", "commodity", "fx", "index"
        )
        per_category_limit = 20 if category else 8

        async def discover_one(provider, cat: str):
            try:
                async with self._network_sem:
                    return list(
                        await provider.async_discover(
                            base_currency, cat, limit=per_category_limit
                        )
                    )
            except Exception as err:
                _LOGGER.debug(
                    "Discovery provider %s/%s failed: %s",
                    provider.provider_id, cat, err,
                )
                return []

        tasks = [
            discover_one(provider, cat)
            for cat in categories
            for provider in self.providers.values()
        ]
        chunks = await asyncio.gather(*tasks)
        buckets: dict[str, list[dict[str, Any]]] = {cat: [] for cat in categories}
        seen: set[tuple[str, str]] = set()
        pool_size = 0
        for chunk in chunks:
            for raw in chunk:
                item = raw.as_dict() if hasattr(raw, "as_dict") else dict(raw)
                cat = str(item.get("category") or "other")
                if cat not in buckets:
                    continue
                key = self._candidate_key(item)
                if not all(key) or key in seen:
                    continue
                seen.add(key)
                buckets[cat].append(item)
                pool_size += 1

        if category:
            selected = buckets.get(category, [])[:20]
        else:
            selected = balanced_discovery_sample(buckets, categories, limit=20)
        return selected, pool_size

    async def async_indication(
        self,
        user_id: str,
        *,
        candidates: list[dict[str, Any]] | None = None,
        amount: float | None = None,
        category: str | None = None,
        scope: str | None = None,
        mode: str = "deterministic",
        ai_task_entity_id: str | None = None,
        risk_tolerance: str = "medium",
        horizon: str = "medium",
        strategy: str = "adaptive",
        overlap_policy: str = "penalize",
        overlap_threshold_pct: float = 20.0,
        diversification: str = "medium",
        max_candidate_pct: float | None = None,
        min_confidence_pct: float = 45.0,
        min_cash_reserve_pct: float = 0.0,
        whole_units_only: bool = False,
    ) -> dict[str, Any]:
        """Rank candidate buys using explicit horizon/risk/overlap preferences."""
        if mode not in {"deterministic", "deterministic_ai", "full_ai"}:
            raise ValueError("Unsupported indication mode")
        if scope is None:
            scope = "search" if candidates else "discover"
        if scope not in {"discover", "portfolio", "search"}:
            raise ValueError("Unsupported indication candidate scope")
        if risk_tolerance not in {"very_low", "low", "medium", "high", "very_high"}:
            raise ValueError("Unsupported risk tolerance")
        if horizon not in {"very_short", "short", "medium", "long", "very_long"}:
            raise ValueError("Unsupported investment horizon")
        if strategy not in {"adaptive", "balanced", "momentum", "trend", "risk_adjusted", "pullback"}:
            raise ValueError("Unsupported indication strategy")
        if overlap_policy not in {"allow", "penalize", "exclude"}:
            raise ValueError("Unsupported portfolio overlap policy")
        if diversification not in {"low", "medium", "high"}:
            raise ValueError("Unsupported diversification preference")
        if amount is not None:
            amount = float(amount)
            if not math.isfinite(amount) or amount < 0:
                raise ValueError("Investment amount must be zero or greater")
        if category is not None and category not in {"crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"}:
            raise ValueError("Unsupported investment category")
        overlap_threshold_pct = float(overlap_threshold_pct)
        max_candidate_pct = None if max_candidate_pct is None else float(max_candidate_pct)
        min_confidence_pct = float(min_confidence_pct)
        min_cash_reserve_pct = float(min_cash_reserve_pct)
        for value, name in (
            (overlap_threshold_pct, "Overlap threshold"),
            (min_confidence_pct, "Minimum confidence"),
            (min_cash_reserve_pct, "Minimum cash reserve"),
        ):
            if not math.isfinite(value) or value < 0 or value > 100:
                raise ValueError(f"{name} must be between 0 and 100")
        if max_candidate_pct is not None:
            if not math.isfinite(max_candidate_pct) or max_candidate_pct <= 0 or max_candidate_pct > 100:
                raise ValueError("Maximum candidate allocation must be greater than zero and at most 100")
        effective_max_candidate_fraction = (
            DIVERSIFICATION_DEFAULT_MAX_FRACTION[diversification]
            if max_candidate_pct is None
            else max_candidate_pct / 100.0
        )

        user = await self.store.async_user(user_id)
        if not bool(user.get("developer_indicator_unlocked", False)):
            raise ValueError("This function is not enabled for this user")
        if int(user.get("indication_disclaimer_version") or 0) < INDICATION_DISCLAIMER_VERSION:
            raise ValueError("The investment indication legal terms must be acknowledged before analysis")
        accepted_region = str(user.get("indication_disclaimer_region") or "").strip().lower()
        if accepted_region not in INDICATION_LEGAL_REGIONS:
            raise ValueError("A legal region must be selected and accepted before investment indication analysis")

        remembered = self._validated_indication_preferences({
            "scope": scope, "mode": mode, "amount": amount, "category": category,
            "ai_task_entity_id": ai_task_entity_id, "risk_tolerance": risk_tolerance,
            "horizon": horizon, "strategy": strategy, "overlap_policy": overlap_policy,
            "overlap_threshold_pct": overlap_threshold_pct, "diversification": diversification,
            "max_candidate_pct": max_candidate_pct, "min_confidence_pct": min_confidence_pct,
            "min_cash_reserve_pct": min_cash_reserve_pct, "whole_units_only": bool(whole_units_only),
        })
        await self.store.async_set_preferences(user_id, indication_preferences=remembered)
        self._cache.clear_prefix(("portfolio", user_id))

        base = self._canonical_currency(user.get("base_currency") or "EUR")
        portfolio = await self.async_portfolio(user_id)
        live_holdings = portfolio.get("holdings") or []
        holding_by_key = {self._candidate_key(h): h for h in live_holdings}
        category_values = {str(cat.get("category")): float(cat.get("value") or 0) for cat in (portfolio.get("categories") or [])}
        portfolio_total = float(portfolio.get("total") or 0)
        owned_funds = await self._owned_fund_holdings(live_holdings) if overlap_policy != "allow" else []

        candidate_pool_size = 0
        if scope == "discover":
            requested, candidate_pool_size = await self._discover_indication_candidates(base, category)
            source = "discovery_category" if category else "discovery_all"
        elif scope == "portfolio":
            source = "portfolio"
            requested = [
                {
                    "provider": h.get("provider"), "provider_id": h.get("provider_id"),
                    "symbol": h.get("symbol"), "name": h.get("name"),
                    "category": h.get("category"), "currency": h.get("currency") or h.get("quote_currency"),
                    "exchange": h.get("exchange"),
                }
                for h in live_holdings
            ]
            candidate_pool_size = len(requested)
        else:
            source = "search"
            requested = list(candidates or [])
            candidate_pool_size = len(requested)
            if not requested:
                raise ValueError("Current search has no indication candidates")

        clean: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for raw in requested:
            if not isinstance(raw, dict):
                continue
            provider = str(raw.get("provider") or "")
            provider_id = str(raw.get("provider_id") or "")
            if provider not in self.providers or not provider_id:
                continue
            key = (provider, provider_id)
            if key in seen:
                continue
            seen.add(key)
            candidate = {
                "provider": provider,
                "provider_id": provider_id,
                "symbol": str(raw.get("symbol") or provider_id),
                "name": str(raw.get("name") or raw.get("symbol") or provider_id),
                "category": str(raw.get("category") or "other"),
                "currency": raw.get("currency"),
                "exchange": raw.get("exchange"),
            }
            if category and candidate["category"] != category:
                continue
            clean.append(candidate)
            if len(clean) >= 20:
                break
        if not clean:
            raise ValueError("No candidates are available for this indication")

        overlap_rows = await asyncio.gather(*(self._candidate_portfolio_overlap(asset, owned_funds) for asset in clean))
        excluded_candidates: list[dict[str, Any]] = []
        eligible_assets: list[tuple[dict[str, Any], dict[str, Any]]] = []
        overlap_threshold = overlap_threshold_pct / 100.0
        for asset, overlap in zip(clean, overlap_rows, strict=True):
            should_exclude = False
            if overlap_policy == "exclude" and overlap.get("matched"):
                if overlap.get("kind") == "stock_in_owned_fund":
                    should_exclude = True
                elif float(overlap.get("overlap") or 0.0) >= overlap_threshold:
                    should_exclude = True
            if should_exclude:
                excluded_candidates.append(
                    {
                        "provider": asset["provider"], "provider_id": asset["provider_id"],
                        "symbol": asset["symbol"], "name": asset["name"], "category": asset["category"],
                        "reason": "portfolio_fund_overlap", "overlap": overlap.get("overlap"),
                        "overlap_kind": overlap.get("kind"), "overlap_sources": overlap.get("sources") or [],
                    }
                )
                continue
            eligible_assets.append((asset, overlap))
        if not eligible_assets:
            raise ValueError("All indication candidates were excluded by the portfolio overlap policy")

        history_period = "5y" if horizon in {"long", "very_long"} else "1y"

        async def evaluate(asset: dict[str, Any], overlap: dict[str, Any]) -> dict[str, Any]:
            quote = await self._quote(asset)
            points = await self._history(asset, history_period)
            key = self._candidate_key(asset)
            existing = holding_by_key.get(key) or {}
            holding_value = float(existing.get("value") or 0)
            holding_weight = holding_value / portfolio_total if portfolio_total > 0 else 0.0
            cat_value = category_values.get(str(asset.get("category") or "other"), 0.0)
            category_weight = cat_value / portfolio_total if portfolio_total > 0 else 0.0
            overlap_score = float(overlap.get("overlap") or 0.0)
            if overlap.get("matched") and overlap_score <= 0:
                overlap_score = 0.15
            deterministic = analyze_prices(
                [point.value for point in points], current_price=quote.price,
                category=str(asset.get("category") or "other"),
                holding_weight=holding_weight, category_weight=category_weight,
                risk_tolerance=risk_tolerance, horizon=horizon, strategy=strategy,
                portfolio_overlap=overlap_score, overlap_policy=overlap_policy,
            ).as_dict()
            fx = await self._fx_rate(quote.currency, base)
            deterministic.update(
                {
                    "provider": asset["provider"], "provider_id": asset["provider_id"],
                    "symbol": asset["symbol"], "name": asset["name"],
                    "category": asset.get("category") or "other", "exchange": asset.get("exchange"),
                    "quote_currency": quote.currency, "portfolio_currency": base,
                    "price": quote.price, "portfolio_price": quote.price * fx,
                    "source": quote.source, "delayed": quote.delayed,
                    "portfolio_overlap": overlap_score,
                    "portfolio_overlap_known": bool(overlap.get("known")),
                    "portfolio_overlap_matched": bool(overlap.get("matched")),
                    "portfolio_overlap_kind": overlap.get("kind"),
                    "portfolio_overlap_method": overlap.get("method"),
                    "portfolio_overlap_sources": overlap.get("sources") or [],
                }
            )
            return deterministic

        evaluated = await asyncio.gather(*(evaluate(asset, overlap) for asset, overlap in eligible_assets), return_exceptions=True)
        results: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for (asset, _overlap), value in zip(eligible_assets, evaluated, strict=True):
            if isinstance(value, Exception):
                errors.append({"symbol": str(asset.get("symbol") or ""), "error": str(value)})
            else:
                results.append(value)
        if not results:
            raise ValueError("Market history is unavailable for all indication candidates")
        attach_relative_strength(results)
        results.sort(
            key=lambda item: (
                float(item.get("score") or 0),
                float(item.get("relative_strength") if item.get("relative_strength") is not None else -1),
                float(item.get("confidence") or 0),
            ),
            reverse=True,
        )
        if whole_units_only and amount is not None and amount > 0:
            # Query-level whole-unit mode may discard assets that can never fit
            # even one unit inside the user's *hard* constraints. Automatic
            # diversification is intentionally not a hard affordability filter:
            # the lot-aware allocator may allow one whole unit to cross that
            # automatic percentage cap. An explicitly entered cap remains hard.
            max_whole_spend = amount * max(0.0, 1.0 - min_cash_reserve_pct / 100.0)
            explicit_candidate_cap = (amount * max_candidate_pct / 100.0) if max_candidate_pct is not None else None
            hard_whole_limit = min(max_whole_spend, explicit_candidate_cap) if explicit_candidate_cap is not None else max_whole_spend
            affordable: list[dict[str, Any]] = []
            for item in results:
                price = float(item.get("portfolio_price") or 0.0)
                if math.isfinite(price) and price > 0 and price <= hard_whole_limit + 0.005:
                    affordable.append(item)
                else:
                    excluded_candidates.append({
                        "provider": item.get("provider"), "provider_id": item.get("provider_id"),
                        "symbol": item.get("symbol"), "name": item.get("name"), "category": item.get("category"),
                        "reason": "whole_unit_above_explicit_cap" if explicit_candidate_cap is not None and price > explicit_candidate_cap + 0.005 else "whole_unit_above_available_budget",
                        "portfolio_price": item.get("portfolio_price"),
                        "whole_unit_hard_cap": round(hard_whole_limit, 2),
                    })
            results = affordable
        allocation = allocate_budget(
            results,
            amount,
            risk_tolerance=risk_tolerance,
            diversification=diversification,
            min_confidence=min_confidence_pct / 100.0,
            max_candidate_fraction=None if max_candidate_pct is None else max_candidate_pct / 100.0,
            minimum_cash_reserve_fraction=min_cash_reserve_pct / 100.0,
            whole_units_only=whole_units_only,
        )

        ai_review = None
        ai_allocation = None
        if mode != "deterministic":
            ai_review = await self._ai_indication_review(
                user_id,
                mode=mode,
                ai_task_entity_id=ai_task_entity_id,
                language=str(user.get("language") or DEFAULT_UI_LANGUAGE),
                amount=amount,
                category=category,
                risk_tolerance=risk_tolerance,
                horizon=horizon,
                strategy=strategy,
                overlap_policy=overlap_policy,
                diversification=diversification,
                max_candidate_pct=max_candidate_pct,
                min_confidence_pct=min_confidence_pct,
                min_cash_reserve_pct=min_cash_reserve_pct,
                whole_units_only=whole_units_only,
                results=results,
            )
            structured = (ai_review or {}).get("structured") or {}
            if mode == "deterministic_ai" and isinstance(structured, dict):
                verdict = str(structured.get("verdict") or "caution").strip().lower()
                structured["verdict"] = verdict if verdict in {"approve", "caution", "reject"} else "caution"
                notes = structured.get("notes")
                structured["notes"] = [str(note)[:300] for note in notes[:12]] if isinstance(notes, list) else []
                structured["summary"] = str(structured.get("summary") or "")[:1200]
            if mode == "full_ai":
                if isinstance(structured, dict):
                    structured["summary"] = str(structured.get("summary") or "")[:1200]
                ai_allocation = sanitize_ai_ranking(
                    results,
                    structured.get("ranking"),
                    amount,
                    max_candidate_fraction=effective_max_candidate_fraction,
                    minimum_cash_reserve_fraction=min_cash_reserve_pct / 100.0,
                    whole_units_only=whole_units_only,
                    max_candidate_fraction_is_hard=max_candidate_pct is not None,
                    risk_tolerance=risk_tolerance,
                )
                results.sort(
                    key=lambda item: (
                        item.get("ai_score") is not None,
                        float(item.get("ai_score") if item.get("ai_score") is not None else item.get("score") or 0),
                    ),
                    reverse=True,
                )

        if whole_units_only and amount is not None:
            unit_key = "ai_suggested_units" if mode == "full_ai" else "suggested_units"
            kept: list[dict[str, Any]] = []
            for item in results:
                units = float(item.get(unit_key) or 0.0)
                if units >= 1.0:
                    kept.append(item)
                else:
                    excluded_candidates.append({
                        "provider": item.get("provider"), "provider_id": item.get("provider_id"),
                        "symbol": item.get("symbol"), "name": item.get("name"), "category": item.get("category"),
                        "reason": "whole_unit_not_selected_by_integer_allocator", "portfolio_price": item.get("portfolio_price"),
                    })
            results = kept
            active_allocation = ai_allocation if mode == "full_ai" else allocation
            deployed = round(sum(float(item.get("ai_suggested_amount" if mode == "full_ai" else "suggested_amount") or 0.0) for item in results), 2)
            if active_allocation is not None and amount is not None:
                active_allocation["deployed"] = deployed
                active_allocation["cash_reserve"] = round(max(0.0, amount - deployed), 2)
                active_allocation["deployment_fraction"] = round(deployed / amount, 4) if amount > 0 else 0.0

        return {
            "mode": mode,
            "scope": scope,
            "source": source,
            "candidate_pool_size": candidate_pool_size,
            "selected_candidate_count": len(clean),
            "overlap_eligible_candidate_count": len(eligible_assets),
            "excluded_candidate_count": len(excluded_candidates),
            "evaluated_candidate_count": len(results),
            "amount": amount,
            "category": category,
            "portfolio_currency": base,
            "method": "risk_horizon_overlap_momentum_trend_v6",
            "preferences": {
                "risk_tolerance": risk_tolerance,
                "horizon": horizon,
                "history_period": history_period,
                "strategy": strategy,
                "overlap_policy": overlap_policy,
                "overlap_threshold_pct": overlap_threshold_pct,
                "diversification": diversification,
                "max_candidate_pct": max_candidate_pct,
                "effective_max_candidate_pct": round(effective_max_candidate_fraction * 100.0, 2),
                "candidate_cap_is_explicit": max_candidate_pct is not None,
                "min_confidence_pct": min_confidence_pct,
                "min_cash_reserve_pct": min_cash_reserve_pct,
                "whole_units_only": bool(whole_units_only),
                "ai_task_entity_id": ai_task_entity_id,
                "ai_uses_home_assistant_preferred": not bool(str(ai_task_entity_id or "").strip()),
            },
            "results": results,
            "excluded_candidates": excluded_candidates,
            "errors": errors,
            "allocation": allocation,
            "ai_allocation": ai_allocation,
            "ai_review": ai_review,
            "generated_at": int(time.time()),
        }

    async def _history(self, holding: dict[str, Any], period: str) -> list[HistoryPoint]:
        key = ("history", holding["provider"], holding["provider_id"], period)
        cached = self._cache.get(key, DEFAULT_HISTORY_CACHE_SECONDS)
        if cached is not None:
            return [HistoryPoint(**x) for x in cached]
        provider = self.providers[holding["provider"]]
        try:
            async with self._network_sem:
                points = list(await provider.async_history(holding["provider_id"], period))
        except Exception as first_err:
            if holding["provider"] == "yahoo":
                async with self._network_sem:
                    points = list(await self.stooq.async_history(holding["provider_id"], period))
            elif holding["provider"] == "kraken":
                # Kraken's OHLC endpoint is intentionally bounded. Use Yahoo's no-key
                # chart history for long crypto ranges when a conventional pair exists.
                symbol = holding.get("symbol", "")
                if "/" not in symbol:
                    raise first_err
                base, quote = symbol.split("/", 1)
                async with self._network_sem:
                    points = list(await self.yahoo.async_history(f"{base}-{quote}", period))
            else:
                raise first_err
        self._cache.set(key, [x.as_dict() for x in points])
        return points

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
