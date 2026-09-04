"""Private per-user portfolio storage."""
from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import (
    DEFAULT_BASE_CURRENCY,
    DEFAULT_INDICATION_PREFERENCES,
    DEFAULT_INCOGNITO_REVEAL_SECONDS,
    DEFAULT_INDICATION_LEGAL_REGION,
    DEFAULT_UI_LANGUAGE,
    EXPOSABLE_ENTITY_METRICS,
    INDICATION_DISCLAIMER_VERSION,
    INDICATION_LEGAL_REGIONS,
    MAX_INCOGNITO_REVEAL_SECONDS,
    STORE_KEY,
    STORE_VERSION,
    SUPPORTED_UI_LANGUAGES,
    TRANSACTION_COST_TYPES,
)
from .currency import canonical_currency, default_settlement_currency
from .ledger import (
    fifo_summary,
    normalize_legacy_transaction,
    normalize_shared_allocations,
    normalize_shared_ownership,
    personal_quantity,
    personal_ratio,
    shared_quantity,
    transaction_timestamp,
)


class InvestmentStore:
    """Persist portfolios in Home Assistant's private .storage area."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store = Store[dict[str, Any]](hass, STORE_VERSION, STORE_KEY)
        self._data: dict[str, Any] = {"users": {}}
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if isinstance(data, dict):
            self._data = data
        users = self._data.setdefault("users", {})
        migrated = False
        for user in users.values():
            if isinstance(user, dict):
                migrated = self._normalize_user(user) or migrated
        # Persist the one-way ledger migration immediately. Otherwise a legacy
        # holding would be rebuilt in memory on every restart until the user
        # happened to perform another write.
        if migrated:
            await self._store.async_save(self._data)

    def _normalize_user(self, user: dict[str, Any]) -> bool:
        """Normalize one stored user and return whether persistent data changed."""
        changed = False
        if "base_currency" not in user:
            user["base_currency"] = DEFAULT_BASE_CURRENCY
            changed = True
        language = str(user.get("language") or DEFAULT_UI_LANGUAGE).lower()
        if language != DEFAULT_UI_LANGUAGE and language not in SUPPORTED_UI_LANGUAGES:
            language = DEFAULT_UI_LANGUAGE
        if user.get("language") != language:
            user["language"] = language
            changed = True
        if "incognito" not in user:
            user["incognito"] = False
            changed = True
        try:
            reveal_seconds = int(user.get("incognito_reveal_seconds", DEFAULT_INCOGNITO_REVEAL_SECONDS))
        except (TypeError, ValueError):
            reveal_seconds = DEFAULT_INCOGNITO_REVEAL_SECONDS
        reveal_seconds = max(0, min(MAX_INCOGNITO_REVEAL_SECONDS, reveal_seconds))
        if user.get("incognito_reveal_seconds") != reveal_seconds:
            user["incognito_reveal_seconds"] = reveal_seconds
            changed = True
        if "developer_indicator_unlocked" not in user:
            user["developer_indicator_unlocked"] = False
            changed = True
        try:
            disclaimer_version = int(user.get("indication_disclaimer_version") or 0)
        except (TypeError, ValueError):
            disclaimer_version = 0
        disclaimer_version = max(0, min(INDICATION_DISCLAIMER_VERSION, disclaimer_version))
        if user.get("indication_disclaimer_version") != disclaimer_version:
            user["indication_disclaimer_version"] = disclaimer_version
            changed = True
        accepted_at = user.get("indication_disclaimer_accepted_at")
        if accepted_at is not None:
            try:
                accepted_at = int(accepted_at)
            except (TypeError, ValueError):
                accepted_at = None
            if user.get("indication_disclaimer_accepted_at") != accepted_at:
                user["indication_disclaimer_accepted_at"] = accepted_at
                changed = True
        region = str(user.get("indication_disclaimer_region") or "").strip().lower()
        if region not in INDICATION_LEGAL_REGIONS:
            region = None
        if user.get("indication_disclaimer_region") != region:
            user["indication_disclaimer_region"] = region
            changed = True
        disclaimer_language = str(user.get("indication_disclaimer_language") or "").strip().lower() or None
        if disclaimer_language is not None and disclaimer_language != DEFAULT_UI_LANGUAGE and disclaimer_language not in SUPPORTED_UI_LANGUAGES:
            disclaimer_language = None
        if user.get("indication_disclaimer_language") != disclaimer_language:
            user["indication_disclaimer_language"] = disclaimer_language
            changed = True
        raw_indication = user.get("indication_preferences")
        indication = dict(DEFAULT_INDICATION_PREFERENCES)
        if isinstance(raw_indication, dict):
            indication.update({key: raw_indication[key] for key in DEFAULT_INDICATION_PREFERENCES if key in raw_indication})
        if raw_indication != indication:
            user["indication_preferences"] = indication
            changed = True
        raw_exposed = user.get("exposed_entities")
        raw_selected = raw_exposed if isinstance(raw_exposed, (list, tuple, set)) else []
        exposed = [metric for metric in EXPOSABLE_ENTITY_METRICS if metric in {str(value) for value in raw_selected}]
        if raw_exposed != exposed:
            user["exposed_entities"] = exposed
            changed = True
        if "holdings" not in user:
            user["holdings"] = []
            changed = True
        if "category_expenses" not in user:
            user["category_expenses"] = {}
            changed = True
        for holding in user["holdings"]:
            holding_currency = canonical_currency(holding.get("currency") or user.get("base_currency") or DEFAULT_BASE_CURRENCY)
            if holding.get("currency") != holding_currency:
                holding["currency"] = holding_currency
                changed = True
            if "average_buy_price" not in holding:
                holding["average_buy_price"] = None
                changed = True
            if "transactions" not in holding:
                holding["transactions"] = []
                changed = True
            transactions = holding["transactions"]
            if not transactions and float(holding.get("quantity") or 0) > 0:
                # Legacy pre-ledger holding: preserve it as one historical BUY.
                created = int(time.time())
                quantity = float(holding.get("quantity") or 0)
                avg = holding.get("average_buy_price")
                transactions.append(
                    {
                        "id": uuid4().hex,
                        "type": "buy",
                        "date": datetime.fromtimestamp(created, UTC).date().isoformat(),
                        "created_at": created,
                        "quantity": quantity,
                        "net_quantity": quantity,
                        "gross_quantity": quantity,
                        "asset_fee_quantity": 0.0,
                        "asset_fee_percent": 0.0,
                        "buy_price": avg,
                        "gross_trade_total": (round(float(avg) * quantity, 2) if avg is not None else None),
                        "investment_total": (round(float(avg) * quantity, 2) if avg is not None else None),
                        "costs": {},
                        "cost_total": 0.0,
                        "all_in_total": (round(float(avg) * quantity, 2) if avg is not None else None),
                        "fee_currency": holding.get("currency") or user.get("base_currency"),
                        "note": "Legacy position imported during ledger migration",
                        "legacy": True,
                    }
                )
                changed = True
            for transaction in transactions:
                changed = normalize_legacy_transaction(transaction) or changed
                trade_currency = canonical_currency(transaction.get("transaction_currency") or holding.get("currency") or user.get("base_currency") or DEFAULT_BASE_CURRENCY)
                default_settlement = default_settlement_currency(trade_currency)
                settlement_currency = canonical_currency(transaction.get("settlement_currency") or transaction.get("fee_currency") or default_settlement)
                if transaction.get("transaction_currency") != trade_currency:
                    transaction["transaction_currency"] = trade_currency; changed = True
                if transaction.get("settlement_currency") != settlement_currency:
                    transaction["settlement_currency"] = settlement_currency; changed = True
                if transaction.get("fee_currency") != settlement_currency:
                    transaction["fee_currency"] = settlement_currency; changed = True
                portfolio_currency = canonical_currency(transaction.get("portfolio_currency_at_transaction") or user.get("base_currency") or DEFAULT_BASE_CURRENCY)
                if transaction.get("portfolio_currency_at_transaction") != portfolio_currency:
                    transaction["portfolio_currency_at_transaction"] = portfolio_currency; changed = True
                if settlement_currency == portfolio_currency and transaction.get("fx_rate") is None:
                    transaction["fx_rate"] = 1.0; changed = True
                if trade_currency == portfolio_currency and transaction.get("quote_fx_rate") is None:
                    transaction["quote_fx_rate"] = 1.0; changed = True
                if transaction.get("trade_fx_rate") is None:
                    if trade_currency == settlement_currency:
                        transaction["trade_fx_rate"] = 1.0; changed = True
                    elif transaction.get("quote_fx_rate") is not None and transaction.get("fx_rate") not in (None, 0):
                        transaction["trade_fx_rate"] = float(transaction["quote_fx_rate"]) / float(transaction["fx_rate"]); changed = True
                if not transaction.get("fx_date"):
                    transaction["fx_date"] = transaction.get("date"); changed = True
                if not transaction.get("trade_fx_source"):
                    transaction["trade_fx_source"] = "identity" if trade_currency == settlement_currency else transaction.get("fx_source") or "historical"; changed = True
                if not transaction.get("fx_source"):
                    transaction["fx_source"] = "identity" if settlement_currency == portfolio_currency else "historical"; changed = True
        return changed

    def _ensure_user(self, user_id: str) -> dict[str, Any]:
        users = self._data.setdefault("users", {})
        user = users.setdefault(
            user_id,
            {
                "base_currency": DEFAULT_BASE_CURRENCY,
                "language": DEFAULT_UI_LANGUAGE,
                "incognito": False,
                "incognito_reveal_seconds": DEFAULT_INCOGNITO_REVEAL_SECONDS,
                "developer_indicator_unlocked": False,
                "indication_disclaimer_version": 0,
                "indication_disclaimer_accepted_at": None,
                "indication_disclaimer_region": None,
                "indication_disclaimer_language": None,
                "indication_preferences": dict(DEFAULT_INDICATION_PREFERENCES),
                "exposed_entities": [],
                "holdings": [],
                "category_expenses": {},
            },
        )
        self._normalize_user(user)
        return user

    async def async_user(self, user_id: str) -> dict[str, Any]:
        async with self._lock:
            return deepcopy(self._ensure_user(user_id))

    async def async_set_category_expense(
        self, user_id: str, category: str, amount: float | None
    ) -> dict[str, Any]:
        """Set manual investment principal for one category in portfolio currency."""
        async with self._lock:
            user = self._ensure_user(user_id)
            expenses = user.setdefault("category_expenses", {})
            if amount is None:
                expenses.pop(category, None)
            else:
                expenses[category] = round(max(0.0, float(amount)), 2)
            await self._store.async_save(self._data)
            return deepcopy(user)

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
        """Persist per-user display, indication and automation preferences."""
        async with self._lock:
            user = self._ensure_user(user_id)
            if base_currency is not None:
                user["base_currency"] = base_currency.upper()
            if language is not None:
                user["language"] = language.lower()
            if incognito is not None:
                user["incognito"] = bool(incognito)
            if incognito_reveal_seconds is not None:
                user["incognito_reveal_seconds"] = int(incognito_reveal_seconds)
            if developer_indicator_unlocked is not None:
                # One-way latch: once exposed for this HA user it cannot be reset
                # through the public preference command.
                user["developer_indicator_unlocked"] = bool(user.get("developer_indicator_unlocked")) or bool(developer_indicator_unlocked)
            if indication_preferences is not None:
                user["indication_preferences"] = deepcopy(indication_preferences)
            if exposed_entities is not None:
                selected = set(exposed_entities)
                user["exposed_entities"] = [metric for metric in EXPOSABLE_ENTITY_METRICS if metric in selected]
            if indication_disclaimer_version is not None:
                user["indication_disclaimer_version"] = int(indication_disclaimer_version)
                user["indication_disclaimer_accepted_at"] = int(time.time())
                user["indication_disclaimer_region"] = str(indication_disclaimer_region or DEFAULT_INDICATION_LEGAL_REGION)
                user["indication_disclaimer_language"] = str(indication_disclaimer_language or DEFAULT_UI_LANGUAGE)
            await self._store.async_save(self._data)
            return deepcopy(user)

    async def async_entity_exposure_snapshot(self) -> dict[str, list[str]]:
        """Return selected automation entity metrics for every stored user."""
        async with self._lock:
            return {
                user_id: list(self._ensure_user(user_id).get("exposed_entities") or [])
                for user_id in list(self._data.setdefault("users", {}))
            }

    async def async_set_base_currency(self, user_id: str, currency: str) -> dict[str, Any]:
        """Backward-compatible currency preference helper."""
        return await self.async_set_preferences(user_id, base_currency=currency)

    @staticmethod
    def _clean_costs(costs: dict[str, Any] | None) -> dict[str, float]:
        clean: dict[str, float] = {}
        for key in TRANSACTION_COST_TYPES:
            raw = (costs or {}).get(key)
            if raw in (None, ""):
                continue
            value = round(max(0.0, float(raw)), 2)
            if value:
                clean[key] = value
        return clean

    @staticmethod
    def _weighted_average(
        old_quantity: float,
        old_average: float | None,
        added_quantity: float,
        added_price: float | None,
    ) -> float | None:
        """Safely update an average price without inventing missing legacy cost data."""
        if added_price is None:
            return old_average
        if old_quantity <= 0:
            return added_price
        if old_average is None:
            # Existing units have unknown cost, so a complete weighted average cannot be known.
            return None
        new_quantity = old_quantity + added_quantity
        if new_quantity <= 0:
            return old_average
        return round(((old_average * old_quantity) + (added_price * added_quantity)) / new_quantity, 12)

    async def async_add_holding(
        self,
        user_id: str,
        asset: dict[str, Any],
        quantity: float = 1.0,
        *,
        average_buy_price: float | None = None,
        gross_trade_total: float | None = None,
        investment_total: float | None = None,
        transaction_costs: dict[str, Any] | None = None,
        transaction_cost_total: float | None = None,
        all_in_total: float | None = None,
        transaction_note: str | None = None,
        transaction_date: str | None = None,
        fee_currency: str | None = None,
        gross_quantity: float | None = None,
        asset_fee_quantity: float = 0.0,
        asset_fee_percent: float = 0.0,
        transaction_currency: str | None = None,
        settlement_currency: str | None = None,
        portfolio_currency_at_transaction: str | None = None,
        fx_rate: float | None = None,
        quote_fx_rate: float | None = None,
        trade_fx_rate: float | None = None,
        fx_date: str | None = None,
        fx_source: str | None = None,
        trade_fx_source: str | None = None,
        shared_allocations: list[dict[str, Any]] | None = None,
        shared_ownership: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add an asset purchase and preserve its transaction costs."""
        async with self._lock:
            user = self._ensure_user(user_id)
            quantity = max(0.0, float(quantity))
            average_buy_price = (
                None if average_buy_price is None else round(max(0.0, float(average_buy_price)), 12)
            )
            gross_trade_total = (
                None if gross_trade_total is None else round(max(0.0, float(gross_trade_total)), 2)
            )
            investment_total = (
                None if investment_total is None else round(max(0.0, float(investment_total)), 2)
            )
            costs = self._clean_costs(transaction_costs)
            transaction_cost_total = (
                sum(costs.values())
                if transaction_cost_total is None
                else round(max(0.0, float(transaction_cost_total)), 2)
            )
            all_in_total = (
                None if all_in_total is None else round(max(0.0, float(all_in_total)), 2)
            )
            note = (transaction_note or "").strip()[:500] or None
            transaction_currency = canonical_currency(transaction_currency or asset.get("currency") or user.get("base_currency") or DEFAULT_BASE_CURRENCY)
            default_settlement = default_settlement_currency(transaction_currency)
            settlement_currency = canonical_currency(settlement_currency or fee_currency or default_settlement)
            fee_currency = settlement_currency
            portfolio_currency_at_transaction = canonical_currency(portfolio_currency_at_transaction or user.get("base_currency") or DEFAULT_BASE_CURRENCY)
            gross_quantity = quantity if gross_quantity is None else max(0.0, float(gross_quantity))
            asset_fee_quantity = max(0.0, float(asset_fee_quantity))
            asset_fee_percent = max(0.0, float(asset_fee_percent))
            shared_allocations = normalize_shared_allocations(shared_allocations, quantity)
            shared_ownership = normalize_shared_ownership(shared_ownership)
            for allocation in shared_allocations:
                allocation.setdefault("id", uuid4().hex)
            owner_quantity = max(0.0, quantity - sum(float(a["quantity"]) for a in shared_allocations))
            created_at = int(time.time())
            date_value = (transaction_date or "").strip()[:10]
            if date_value:
                datetime.strptime(date_value, "%Y-%m-%d")
            else:
                date_value = datetime.fromtimestamp(created_at, UTC).date().isoformat()
            transaction = {
                "id": uuid4().hex,
                "type": "buy",
                "date": date_value,
                "created_at": created_at,
                "quantity": quantity,
                "net_quantity": quantity,
                "gross_quantity": gross_quantity,
                "shared_allocations": shared_allocations,
                "shared_ownership": shared_ownership,
                "shared_quantity": round(sum(float(a["quantity"]) for a in shared_allocations), 12),
                "personal_quantity": round(owner_quantity, 12),
                "asset_fee_quantity": asset_fee_quantity,
                "asset_fee_percent": asset_fee_percent,
                "buy_price": average_buy_price,
                "gross_trade_total": gross_trade_total,
                "investment_total": investment_total,
                "costs": costs,
                "cost_total": transaction_cost_total,
                "all_in_total": all_in_total,
                "fee_currency": fee_currency,
                "transaction_currency": transaction_currency,
                "settlement_currency": settlement_currency,
                "portfolio_currency_at_transaction": portfolio_currency_at_transaction,
                "fx_rate": None if fx_rate is None else float(fx_rate),
                "quote_fx_rate": None if quote_fx_rate is None else float(quote_fx_rate),
                "trade_fx_rate": None if trade_fx_rate is None else float(trade_fx_rate),
                "fx_date": fx_date or date_value,
                "fx_source": fx_source,
                "trade_fx_source": trade_fx_source,
                "note": note,
            }

            for existing in user["holdings"]:
                if (
                    existing.get("provider") == asset["provider"]
                    and existing.get("provider_id") == asset["provider_id"]
                ):
                    old_quantity = float(existing.get("quantity") or 0)
                    old_average_raw = existing.get("average_buy_price")
                    old_average = float(old_average_raw) if old_average_raw is not None else None
                    old_gross = sum(
                        float(tx.get("gross_quantity", tx.get("quantity", 0)) or 0)
                        for tx in (existing.get("transactions") or [])
                        if tx.get("type", "buy") == "buy"
                    ) or old_quantity
                    existing["average_buy_price"] = self._weighted_average(
                        old_gross, old_average, gross_quantity, average_buy_price
                    )
                    existing.setdefault("transactions", []).append(transaction)
                    self._recompute_holding_aggregate(existing)
                    await self._store.async_save(self._data)
                    return deepcopy(existing)

            holding = {
                "id": uuid4().hex,
                "provider": asset["provider"],
                "provider_id": asset["provider_id"],
                "symbol": asset["symbol"],
                "name": asset["name"],
                "category": asset.get("category") or "other",
                "currency": canonical_currency(asset.get("currency") or transaction_currency),
                "exchange": asset.get("exchange"),
                "quantity": owner_quantity,
                "shared_quantity": round(sum(float(a["quantity"]) for a in shared_allocations), 12),
                "custody_quantity": round(quantity, 12),
                "average_buy_price": average_buy_price,
                "transactions": [transaction],
            }
            user["holdings"].append(holding)
            await self._store.async_save(self._data)
            return deepcopy(holding)

    async def async_add_sell_transaction(
        self,
        user_id: str,
        holding_id: str,
        quantity: float,
        *,
        sell_price: float | None = None,
        gross_sale_total: float | None = None,
        proceeds_total: float | None = None,
        transaction_costs: dict[str, Any] | None = None,
        transaction_cost_total: float | None = None,
        transaction_note: str | None = None,
        transaction_date: str | None = None,
        fee_currency: str | None = None,
        transaction_currency: str | None = None,
        settlement_currency: str | None = None,
        portfolio_currency_at_transaction: str | None = None,
        fx_rate: float | None = None,
        quote_fx_rate: float | None = None,
        trade_fx_rate: float | None = None,
        fx_date: str | None = None,
        fx_source: str | None = None,
        trade_fx_source: str | None = None,
    ) -> dict[str, Any] | None:
        """Append an immutable SELL ledger record and update the current quantity."""
        async with self._lock:
            user = self._ensure_user(user_id)
            for holding in user["holdings"]:
                if holding["id"] != holding_id:
                    continue
                quantity = max(0.0, float(quantity))
                if quantity <= 0:
                    raise ValueError("Sell quantity must be greater than zero")
                current_quantity = float(holding.get("quantity") or 0)
                if quantity > current_quantity + 1e-12:
                    raise ValueError("Sell quantity exceeds units currently owned")

                sell_price = None if sell_price is None else round(max(0.0, float(sell_price)), 12)
                gross_sale_total = (
                    None if gross_sale_total is None else round(max(0.0, float(gross_sale_total)), 2)
                )
                proceeds_total = (
                    None if proceeds_total is None else round(max(0.0, float(proceeds_total)), 2)
                )
                costs = self._clean_costs(transaction_costs)
                transaction_cost_total = (
                    sum(costs.values())
                    if transaction_cost_total is None
                    else round(max(0.0, float(transaction_cost_total)), 2)
                )
                detailed = round(sum(costs.values()), 2)
                if transaction_cost_total + 0.000001 < detailed:
                    raise ValueError("Transaction cost total cannot be below the detailed costs")
                residual = round(max(0.0, transaction_cost_total - detailed), 2)
                if residual:
                    costs["other"] = round(costs.get("other", 0.0) + residual, 2)

                created_at = int(time.time())
                date_value = (transaction_date or "").strip()[:10]
                if date_value:
                    datetime.strptime(date_value, "%Y-%m-%d")
                else:
                    date_value = datetime.fromtimestamp(created_at, UTC).date().isoformat()
                transaction_currency = canonical_currency(transaction_currency or holding.get("currency") or user.get("base_currency") or DEFAULT_BASE_CURRENCY)
                default_settlement = default_settlement_currency(transaction_currency)
                settlement_currency = canonical_currency(settlement_currency or fee_currency or default_settlement)
                portfolio_currency_at_transaction = canonical_currency(portfolio_currency_at_transaction or user.get("base_currency") or DEFAULT_BASE_CURRENCY)
                transaction = {
                    "id": uuid4().hex,
                    "type": "sell",
                    "date": date_value,
                    "created_at": created_at,
                    "quantity": quantity,
                    "sell_price": sell_price,
                    "gross_sale_total": gross_sale_total,
                    "proceeds_total": proceeds_total,
                    "costs": costs,
                    "cost_total": transaction_cost_total,
                    "fee_currency": settlement_currency,
                    "transaction_currency": transaction_currency,
                    "settlement_currency": settlement_currency,
                    "portfolio_currency_at_transaction": portfolio_currency_at_transaction,
                    "fx_rate": None if fx_rate is None else float(fx_rate),
                    "quote_fx_rate": None if quote_fx_rate is None else float(quote_fx_rate),
                    "trade_fx_rate": None if trade_fx_rate is None else float(trade_fx_rate),
                    "fx_date": fx_date or date_value,
                    "fx_source": fx_source,
                    "trade_fx_source": trade_fx_source,
                    "note": (transaction_note or "").strip()[:500] or None,
                }
                holding.setdefault("transactions", []).append(transaction)
                holding["quantity"] = max(0.0, current_quantity - quantity)
                holding["shared_quantity"] = round(sum(shared_quantity(tx) for tx in (holding.get("transactions") or [])), 12)
                holding["custody_quantity"] = round(float(holding["quantity"]) + float(holding["shared_quantity"]), 12)
                await self._store.async_save(self._data)
                return deepcopy(holding)
        return None

    @staticmethod
    def _recompute_holding_aggregate(holding: dict[str, Any]) -> None:
        """Rebuild current quantity and legacy average from transaction history.

        The transaction ledger is authoritative. This helper is intentionally
        pure with respect to stored transaction rows: it only updates aggregate
        compatibility fields on the holding after an edit.
        """
        records: list[dict[str, Any]] = []
        weighted_value = 0.0
        weighted_quantity = 0.0
        weighted_complete = True
        for tx in holding.get("transactions") or []:
            tx_type = str(tx.get("type") or "buy")
            if tx_type == "sell":
                records.append(
                    {
                        "id": tx.get("id"),
                        "type": "sell",
                        "sort_ts": transaction_timestamp(tx),
                        "quantity": float(tx.get("quantity") or 0),
                    }
                )
                continue
            net = float(tx.get("net_quantity", tx.get("quantity", 0)) or 0)
            owner_net = personal_quantity(tx)
            gross = float(tx.get("gross_quantity", net) or 0)
            records.append(
                {
                    "id": tx.get("id"),
                    "type": "buy",
                    "sort_ts": transaction_timestamp(tx),
                    "quantity": owner_net,
                }
            )
            price = tx.get("buy_price")
            owner_gross = gross * personal_ratio(tx)
            if owner_gross > 0:
                if price is None:
                    weighted_complete = False
                else:
                    weighted_value += owner_gross * float(price)
                    weighted_quantity += owner_gross

        summary = fifo_summary(records)
        holding["quantity"] = max(0.0, float(summary.quantity))
        holding["shared_quantity"] = round(sum(shared_quantity(tx) for tx in (holding.get("transactions") or [])), 12)
        holding["custody_quantity"] = round(holding["quantity"] + holding["shared_quantity"], 12)
        holding["average_buy_price"] = (
            round(weighted_value / weighted_quantity, 12)
            if weighted_complete and weighted_quantity > 0
            else None
        )

    async def async_replace_transaction(
        self,
        user_id: str,
        holding_id: str,
        transaction_id: str,
        replacement: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Replace one historical transaction atomically and rebuild aggregates.

        IDs, transaction type, and creation order are immutable. FIFO validation
        runs before persistent state is changed, so an edit that would make any
        historical SELL exceed the units owned on that date is rejected without
        partially modifying the portfolio.
        """
        async with self._lock:
            user = self._ensure_user(user_id)
            for holding in user["holdings"]:
                if holding.get("id") != holding_id:
                    continue
                transactions = holding.setdefault("transactions", [])
                index = next(
                    (i for i, tx in enumerate(transactions) if str(tx.get("id")) == str(transaction_id)),
                    None,
                )
                if index is None:
                    return None
                original = transactions[index]
                if str(replacement.get("type") or original.get("type") or "buy") != str(original.get("type") or "buy"):
                    raise ValueError("Transaction type cannot be changed")

                candidate = deepcopy(holding)
                candidate_tx = candidate.setdefault("transactions", [])
                clean = deepcopy(replacement)
                clean["id"] = original.get("id")
                clean["type"] = original.get("type") or "buy"
                clean["created_at"] = original.get("created_at") or int(time.time())
                candidate_tx[index] = clean
                self._recompute_holding_aggregate(candidate)

                holding["transactions"] = candidate_tx
                holding["quantity"] = candidate["quantity"]
                holding["shared_quantity"] = candidate.get("shared_quantity", 0.0)
                holding["custody_quantity"] = candidate.get("custody_quantity", candidate["quantity"])
                holding["average_buy_price"] = candidate["average_buy_price"]
                await self._store.async_save(self._data)
                return deepcopy(holding)
        return None

    async def async_update_holding(
        self, user_id: str, holding_id: str, changes: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Update non-ledger holding metadata only.

        Quantity and cost basis are intentionally excluded: transaction history
        is authoritative from v0.3.0 onward.
        """
        allowed = {"category"}
        async with self._lock:
            user = self._ensure_user(user_id)
            for holding in user["holdings"]:
                if holding["id"] != holding_id:
                    continue
                for key, value in changes.items():
                    if key in allowed:
                        holding[key] = value
                await self._store.async_save(self._data)
                return deepcopy(holding)
        return None

    async def async_remove_holding(self, user_id: str, holding_id: str) -> bool:
        async with self._lock:
            user = self._ensure_user(user_id)
            before = len(user["holdings"])
            user["holdings"] = [x for x in user["holdings"] if x["id"] != holding_id]
            changed = len(user["holdings"]) != before
            if changed:
                await self._store.async_save(self._data)
            return changed
