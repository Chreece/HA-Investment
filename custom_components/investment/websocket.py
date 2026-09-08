"""Authenticated WebSocket API for the Investment panel."""
from __future__ import annotations

from ipaddress import ip_address
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from .const import (
    DEFAULT_INCOGNITO_REVEAL_SECONDS,
    DEFAULT_UI_LANGUAGE,
    DOMAIN,
    EXPOSABLE_ENTITY_METRICS,
    INDICATION_DISCLAIMER_VERSION,
    INDICATION_LEGAL_REGIONS,
    MAX_INCOGNITO_REVEAL_SECONDS,
    SUPPORTED_PERIODS,
    SUPPORTED_UI_LANGUAGES,
)


SHARED_ALLOCATION_SCHEMA = vol.Schema(
    {
        vol.Optional("id"): vol.All(str, vol.Length(max=64)),
        vol.Required("participant"): vol.All(str, vol.Length(min=1, max=120)),
        vol.Required("quantity"): vol.Coerce(float),
        vol.Optional("input_mode"): vol.In(["units", "percent", "investment", "pool_percent"]),
        vol.Optional("input_value"): vol.Coerce(float),
        vol.Optional("pool_percent"): vol.Coerce(float),
    }
)

SHARED_OWNERSHIP_SCHEMA = vol.Schema(
    {
        vol.Optional("style", default="direct"): vol.In(["direct", "common"]),
        vol.Optional("common_mode"): vol.In(["units", "percent", "investment"]),
        vol.Optional("common_value"): vol.Coerce(float),
        vol.Optional("user_common_percent"): vol.Coerce(float),
        vol.Optional("user_input_mode"): vol.In(["units", "percent", "investment"]),
        vol.Optional("user_input_value"): vol.Coerce(float),
    }
)


def _manager(hass: HomeAssistant):
    data = hass.data.get(DOMAIN)
    if not data or "manager" not in data:
        raise RuntimeError("Investment integration is not ready")
    return data["manager"]


def _user_id(connection: websocket_api.ActiveConnection) -> str:
    if connection.user is None:
        raise RuntimeError("Authenticated user required")
    return connection.user.id


def _connection_is_local(connection: websocket_api.ActiveConnection) -> bool:
    """Return whether HA sees the WebSocket peer as local/non-global.

    Unknown peers deliberately return False so privacy defaults fail closed. HA's
    HTTP trusted-proxy handling is applied before ActiveConnection.remote is set,
    so deployments with a correctly configured reverse proxy still see the real
    client address here.
    """
    raw = str(getattr(connection, "remote", "") or "").strip()
    if not raw:
        return False
    host = raw.split("%", 1)[0]
    if host.startswith("[") and "]" in host:
        host = host[1:host.index("]")]
    elif host.count(":") == 1 and "." in host:
        host = host.rsplit(":", 1)[0]
    try:
        return not ip_address(host).is_global
    except ValueError:
        return False


@websocket_api.websocket_command({vol.Required("type"): "investment/get_portfolio", vol.Optional("force", default=False): bool, vol.Optional("refresh_market", default=False): bool})
@websocket_api.async_response
async def ws_get_portfolio(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        result = await _manager(hass).async_portfolio(_user_id(connection), force=msg["force"], refresh_market=msg["refresh_market"])
        # Connection locality is session-specific and must never be cached inside
        # the portfolio object itself. It drives the frontend's privacy-safe remote default.
        payload = dict(result)
        payload["connection_local"] = _connection_is_local(connection)
        connection.send_result(msg["id"], payload)
    except Exception as err:
        connection.send_error(msg["id"], "portfolio_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/search",
        vol.Required("query"): vol.All(str, vol.Length(min=1, max=120)),
        vol.Optional("currency"): vol.All(str, vol.Length(min=3, max=3)),
    }
)
@websocket_api.async_response
async def ws_search(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        result = await _manager(hass).async_search(_user_id(connection), msg["query"], msg.get("currency"))
        connection.send_result(msg["id"], {"results": result})
    except Exception as err:
        connection.send_error(msg["id"], "search_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/fx_rate",
        vol.Required("from_currency"): vol.All(str, vol.Length(min=3, max=3)),
        vol.Optional("to_currency"): vol.All(str, vol.Length(min=3, max=3)),
        vol.Optional("date"): vol.All(str, vol.Length(min=10, max=10)),
    }
)
@websocket_api.async_response
async def ws_fx_rate(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        result = await _manager(hass).async_fx_rate(
            _user_id(connection), msg["from_currency"], to_currency=msg.get("to_currency"), on_date=msg.get("date")
        )
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "fx_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/quote",
        vol.Required("asset"): dict,
    }
)
@websocket_api.async_response
async def ws_quote(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        result = await _manager(hass).async_asset_quote(_user_id(connection), msg["asset"])
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "quote_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/add",
        vol.Required("asset"): dict,
        vol.Optional("quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("gross_quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("net_quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("asset_fee_quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("asset_fee_percent"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("shared_allocations", default=[]): [SHARED_ALLOCATION_SCHEMA],
        vol.Optional("shared_ownership"): SHARED_OWNERSHIP_SCHEMA,
        vol.Optional("principal_mode", default="unit"): vol.In(["unit", "total", "independent"]),
        vol.Optional("average_buy_price"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("gross_trade_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("investment_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("transaction_costs", default={}): {
            vol.Optional("platform"): vol.Coerce(float),
            vol.Optional("bank"): vol.Coerce(float),
            vol.Optional("exchange"): vol.Coerce(float),
            vol.Optional("tax"): vol.Coerce(float),
            vol.Optional("other"): vol.Coerce(float),
        },
        vol.Optional("transaction_cost_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("all_in_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("transaction_note"): vol.All(str, vol.Length(max=500)),
        vol.Optional("transaction_date"): vol.All(str, vol.Length(min=10, max=10)),
        vol.Optional("settlement_currency"): vol.All(str, vol.Length(min=3, max=3)),
        vol.Optional("fx_rate"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("trade_fx_rate"): vol.Any(None, vol.Coerce(float)),
    }
)
@websocket_api.async_response
async def ws_add(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        holding = await _manager(hass).async_add(
            _user_id(connection),
            msg["asset"],
            msg.get("quantity"),
            gross_quantity=msg.get("gross_quantity"),
            net_quantity=msg.get("net_quantity"),
            asset_fee_quantity=msg.get("asset_fee_quantity"),
            asset_fee_percent=msg.get("asset_fee_percent"),
            shared_allocations=msg.get("shared_allocations"),
            shared_ownership=msg.get("shared_ownership"),
            principal_mode=msg.get("principal_mode", "unit"),
            average_buy_price=msg.get("average_buy_price"),
            gross_trade_total=msg.get("gross_trade_total"),
            investment_total=msg.get("investment_total"),
            transaction_costs=msg.get("transaction_costs"),
            transaction_cost_total=msg.get("transaction_cost_total"),
            all_in_total=msg.get("all_in_total"),
            transaction_note=msg.get("transaction_note"),
            transaction_date=msg.get("transaction_date"),
            settlement_currency=msg.get("settlement_currency"),
            fx_rate=msg.get("fx_rate"),
            trade_fx_rate=msg.get("trade_fx_rate"),
        )
        connection.send_result(msg["id"], {"holding": holding})
    except Exception as err:
        connection.send_error(msg["id"], "add_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/sell",
        vol.Required("holding_id"): str,
        vol.Required("quantity"): vol.Coerce(float),
        vol.Optional("sell_price"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("gross_sale_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("proceeds_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("transaction_costs", default={}): {
            vol.Optional("platform"): vol.Coerce(float),
            vol.Optional("bank"): vol.Coerce(float),
            vol.Optional("exchange"): vol.Coerce(float),
            vol.Optional("tax"): vol.Coerce(float),
            vol.Optional("other"): vol.Coerce(float),
        },
        vol.Optional("transaction_cost_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("transaction_note"): vol.All(str, vol.Length(max=500)),
        vol.Optional("transaction_date"): vol.All(str, vol.Length(min=10, max=10)),
        vol.Optional("settlement_currency"): vol.All(str, vol.Length(min=3, max=3)),
        vol.Optional("fx_rate"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("trade_fx_rate"): vol.Any(None, vol.Coerce(float)),
    }
)
@websocket_api.async_response
async def ws_sell(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        holding = await _manager(hass).async_sell(
            _user_id(connection),
            msg["holding_id"],
            msg["quantity"],
            sell_price=msg.get("sell_price"),
            gross_sale_total=msg.get("gross_sale_total"),
            proceeds_total=msg.get("proceeds_total"),
            transaction_costs=msg.get("transaction_costs"),
            transaction_cost_total=msg.get("transaction_cost_total"),
            transaction_note=msg.get("transaction_note"),
            transaction_date=msg.get("transaction_date"),
            settlement_currency=msg.get("settlement_currency"),
            fx_rate=msg.get("fx_rate"),
            trade_fx_rate=msg.get("trade_fx_rate"),
        )
        connection.send_result(msg["id"], {"holding": holding})
    except Exception as err:
        connection.send_error(msg["id"], "sell_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/edit_transaction",
        vol.Required("holding_id"): str,
        vol.Required("transaction_id"): str,
        vol.Optional("quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("gross_quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("net_quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("asset_fee_quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("asset_fee_percent"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("shared_allocations"): [SHARED_ALLOCATION_SCHEMA],
        vol.Optional("shared_ownership"): SHARED_OWNERSHIP_SCHEMA,
        vol.Optional("average_buy_price"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("gross_trade_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("investment_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("all_in_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("sell_price"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("gross_sale_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("proceeds_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("transaction_costs", default={}): {
            vol.Optional("platform"): vol.Coerce(float),
            vol.Optional("bank"): vol.Coerce(float),
            vol.Optional("exchange"): vol.Coerce(float),
            vol.Optional("tax"): vol.Coerce(float),
            vol.Optional("other"): vol.Coerce(float),
        },
        vol.Optional("transaction_cost_total"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("transaction_note"): vol.All(str, vol.Length(max=500)),
        vol.Optional("transaction_date"): vol.All(str, vol.Length(min=10, max=10)),
        vol.Optional("settlement_currency"): vol.All(str, vol.Length(min=3, max=3)),
        vol.Optional("fx_rate"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("trade_fx_rate"): vol.Any(None, vol.Coerce(float)),
    }
)
@websocket_api.async_response
async def ws_edit_transaction(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        holding = await _manager(hass).async_edit_transaction(
            _user_id(connection),
            msg["holding_id"],
            msg["transaction_id"],
            quantity=msg.get("quantity"),
            gross_quantity=msg.get("gross_quantity"),
            net_quantity=msg.get("net_quantity"),
            asset_fee_quantity=msg.get("asset_fee_quantity"),
            asset_fee_percent=msg.get("asset_fee_percent"),
            shared_allocations=msg.get("shared_allocations"),
            shared_ownership=msg.get("shared_ownership"),
            average_buy_price=msg.get("average_buy_price"),
            gross_trade_total=msg.get("gross_trade_total"),
            investment_total=msg.get("investment_total"),
            all_in_total=msg.get("all_in_total"),
            sell_price=msg.get("sell_price"),
            gross_sale_total=msg.get("gross_sale_total"),
            proceeds_total=msg.get("proceeds_total"),
            transaction_costs=msg.get("transaction_costs"),
            transaction_cost_total=msg.get("transaction_cost_total"),
            transaction_note=msg.get("transaction_note"),
            transaction_date=msg.get("transaction_date"),
            settlement_currency=msg.get("settlement_currency"),
            fx_rate=msg.get("fx_rate"),
            trade_fx_rate=msg.get("trade_fx_rate"),
        )
        connection.send_result(msg["id"], {"holding": holding})
    except Exception as err:
        connection.send_error(msg["id"], "edit_transaction_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/update",
        vol.Required("holding_id"): str,
        vol.Optional("quantity"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("average_buy_price"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("category"): vol.In(["crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"]),
    }
)
@websocket_api.async_response
async def ws_update(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        changes = {k: msg[k] for k in ("quantity", "average_buy_price", "category") if k in msg}
        holding = await _manager(hass).async_update(_user_id(connection), msg["holding_id"], changes)
        connection.send_result(msg["id"], {"holding": holding})
    except Exception as err:
        connection.send_error(msg["id"], "update_error", str(err))


@websocket_api.websocket_command(
    {vol.Required("type"): "investment/remove", vol.Required("holding_id"): str}
)
@websocket_api.async_response
async def ws_remove(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        removed = await _manager(hass).async_remove(_user_id(connection), msg["holding_id"])
        connection.send_result(msg["id"], {"removed": removed})
    except Exception as err:
        connection.send_error(msg["id"], "remove_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/set_preferences",
        vol.Optional("base_currency"): vol.All(str, vol.Length(min=3, max=3)),
        vol.Optional("language"): vol.In((DEFAULT_UI_LANGUAGE, *SUPPORTED_UI_LANGUAGES)),
        vol.Optional("incognito"): bool,
        vol.Optional("incognito_reveal_seconds"): vol.All(vol.Coerce(int), vol.Range(min=0, max=MAX_INCOGNITO_REVEAL_SECONDS)),
        vol.Optional("developer_indicator_unlocked"): bool,
        vol.Optional("indication_preferences"): dict,
        vol.Optional("exposed_entities"): [vol.In(EXPOSABLE_ENTITY_METRICS)],
        vol.Optional("indication_disclaimer_version"): vol.All(vol.Coerce(int), vol.Range(min=0, max=INDICATION_DISCLAIMER_VERSION)),
        vol.Optional("indication_disclaimer_region"): vol.In(INDICATION_LEGAL_REGIONS),
        vol.Optional("indication_disclaimer_language"): vol.In((DEFAULT_UI_LANGUAGE, *SUPPORTED_UI_LANGUAGES)),
    }
)
@websocket_api.async_response
async def ws_preferences(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        preference_keys = {"base_currency", "language", "incognito", "incognito_reveal_seconds", "developer_indicator_unlocked", "indication_preferences", "exposed_entities", "indication_disclaimer_version", "indication_disclaimer_region", "indication_disclaimer_language"}
        if not any(key in msg for key in preference_keys):
            raise ValueError("At least one preference is required")
        user = await _manager(hass).async_set_preferences(
            _user_id(connection),
            base_currency=msg.get("base_currency"),
            language=msg.get("language"),
            incognito=msg.get("incognito"),
            incognito_reveal_seconds=msg.get("incognito_reveal_seconds"),
            developer_indicator_unlocked=msg.get("developer_indicator_unlocked"),
            indication_preferences=msg.get("indication_preferences"),
            exposed_entities=msg.get("exposed_entities"),
            indication_disclaimer_version=msg.get("indication_disclaimer_version"),
            indication_disclaimer_region=msg.get("indication_disclaimer_region"),
            indication_disclaimer_language=msg.get("indication_disclaimer_language"),
        )
        connection.send_result(
            msg["id"],
            {
                "base_currency": user["base_currency"],
                "language": user.get("language", DEFAULT_UI_LANGUAGE),
                "incognito": bool(user.get("incognito", False)),
                "incognito_reveal_seconds": int(user.get("incognito_reveal_seconds", DEFAULT_INCOGNITO_REVEAL_SECONDS)),
                "developer_indicator_unlocked": bool(user.get("developer_indicator_unlocked", False)),
                "indication_preferences": user.get("indication_preferences") or {},
                "exposed_entities": list(user.get("exposed_entities") or []),
                "indication_disclaimer_version": int(user.get("indication_disclaimer_version") or 0),
                "indication_disclaimer_accepted_at": user.get("indication_disclaimer_accepted_at"),
                "indication_disclaimer_region": user.get("indication_disclaimer_region"),
                "indication_disclaimer_language": user.get("indication_disclaimer_language"),
            },
        )
    except Exception as err:
        connection.send_error(msg["id"], "preferences_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/history",
        vol.Required("scope"): vol.In(["holding", "category", "portfolio"]),
        vol.Optional("scope_id"): vol.Any(None, str),
        vol.Required("period"): vol.In(SUPPORTED_PERIODS),
    }
)
@websocket_api.async_response
async def ws_history(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        result = await _manager(hass).async_scope_history(
            _user_id(connection), msg["scope"], msg.get("scope_id"), msg["period"]
        )
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "history_error", str(err))



@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/set_category_expense",
        vol.Required("category"): vol.In(["crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"]),
        vol.Optional("amount"): vol.Any(None, vol.Coerce(float)),
    }
)
@websocket_api.async_response
async def ws_category_expense(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        amount = msg.get("amount")
        if amount is not None and amount < 0:
            raise ValueError("Expense cannot be negative")
        user = await _manager(hass).async_set_category_expense(
            _user_id(connection), msg["category"], amount
        )
        connection.send_result(msg["id"], {"category_expenses": user.get("category_expenses", {})})
    except Exception as err:
        connection.send_error(msg["id"], "category_expense_error", str(err))

@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/indication",
        vol.Optional("candidates", default=[]): [dict],
        vol.Optional("amount"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("category"): vol.Any(None, vol.In(["crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"])),
        vol.Optional("scope"): vol.In(["discover", "portfolio", "search"]),
        vol.Optional("mode", default="deterministic"): vol.In(["deterministic", "deterministic_ai", "full_ai"]),
        vol.Optional("ai_task_entity_id"): vol.Any(None, str),
        # Compatibility with r10 clients; new clients send ai_task_entity_id.
        vol.Optional("ai_agent_id"): vol.Any(None, str),
        vol.Optional("risk_tolerance", default="medium"): vol.In(["very_low", "low", "medium", "high", "very_high"]),
        vol.Optional("horizon", default="medium"): vol.In(["very_short", "short", "medium", "long", "very_long"]),
        vol.Optional("strategy", default="adaptive"): vol.In(["adaptive", "balanced", "momentum", "trend", "risk_adjusted", "pullback"]),
        vol.Optional("overlap_policy", default="penalize"): vol.In(["allow", "penalize", "exclude"]),
        vol.Optional("overlap_threshold_pct", default=20.0): vol.Coerce(float),
        vol.Optional("diversification", default="medium"): vol.In(["low", "medium", "high"]),
        vol.Optional("max_candidate_pct"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("min_confidence_pct", default=45.0): vol.Coerce(float),
        vol.Optional("min_cash_reserve_pct", default=0.0): vol.Coerce(float),
        vol.Optional("whole_units_only", default=False): bool,
    }
)
@websocket_api.async_response
async def ws_indication(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        ai_task_entity_id = msg.get("ai_task_entity_id")
        # The old field is accepted only when it already names an ai_task entity;
        # Conversation agent IDs are intentionally not forwarded to AI Task.
        if not ai_task_entity_id and str(msg.get("ai_agent_id") or "").startswith("ai_task."):
            ai_task_entity_id = msg.get("ai_agent_id")
        result = await _manager(hass).async_indication(
            _user_id(connection),
            candidates=msg.get("candidates"),
            amount=msg.get("amount"),
            category=msg.get("category"),
            scope=msg.get("scope"),
            mode=msg.get("mode", "deterministic"),
            ai_task_entity_id=ai_task_entity_id,
            risk_tolerance=msg.get("risk_tolerance", "medium"),
            horizon=msg.get("horizon", "medium"),
            strategy=msg.get("strategy", "adaptive"),
            overlap_policy=msg.get("overlap_policy", "penalize"),
            overlap_threshold_pct=msg.get("overlap_threshold_pct", 20.0),
            diversification=msg.get("diversification", "medium"),
            max_candidate_pct=msg.get("max_candidate_pct"),
            min_confidence_pct=msg.get("min_confidence_pct", 45.0),
            min_cash_reserve_pct=msg.get("min_cash_reserve_pct", 0.0),
            whole_units_only=bool(msg.get("whole_units_only", False)),
        )
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "indication_error", str(err))


def async_register_commands(hass: HomeAssistant) -> None:
    """Register commands once for the integration domain."""
    websocket_api.async_register_command(hass, ws_get_portfolio)
    websocket_api.async_register_command(hass, ws_search)
    websocket_api.async_register_command(hass, ws_fx_rate)
    websocket_api.async_register_command(hass, ws_quote)
    websocket_api.async_register_command(hass, ws_add)
    websocket_api.async_register_command(hass, ws_sell)
    websocket_api.async_register_command(hass, ws_edit_transaction)
    websocket_api.async_register_command(hass, ws_update)
    websocket_api.async_register_command(hass, ws_remove)
    websocket_api.async_register_command(hass, ws_preferences)
    websocket_api.async_register_command(hass, ws_category_expense)
    websocket_api.async_register_command(hass, ws_history)
    websocket_api.async_register_command(hass, ws_indication)
