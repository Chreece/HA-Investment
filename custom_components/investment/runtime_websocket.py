"""Runtime websocket registration for the alternate integration line."""
from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from . import websocket as base


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/indication",
        vol.Optional("candidates", default=[]): [dict],
        vol.Optional("amount"): vol.Any(None, vol.Coerce(float)),
        vol.Optional("category"): vol.Any(None, vol.In(["crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"])),
        vol.Optional("scope"): vol.In(["discover", "portfolio", "search"]),
        vol.Optional("mode", default="deterministic"): vol.In(["deterministic", "deterministic_ai", "full_ai"]),
        vol.Optional("ai_task_entity_id"): vol.Any(None, str),
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
        vol.Optional("portfolio_context", default="use"): vol.In(["use", "ignore"]),
        vol.Optional("existing_instruments", default="allow"): vol.In(["allow", "exclude"]),
    }
)
@websocket_api.async_response
async def ws_indication(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        ai_task_entity_id = msg.get("ai_task_entity_id")
        if not ai_task_entity_id and str(msg.get("ai_agent_id") or "").startswith("ai_task."):
            ai_task_entity_id = msg.get("ai_agent_id")
        result = await base._manager(hass).async_indication(
            base._user_id(connection),
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
            portfolio_context=msg.get("portfolio_context", "use"),
            existing_instruments=msg.get("existing_instruments", "allow"),
        )
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "indication_error", str(err))


def async_register_commands(hass: HomeAssistant) -> None:
    """Register base commands plus the extended analysis command."""
    websocket_api.async_register_command(hass, base.ws_get_portfolio)
    websocket_api.async_register_command(hass, base.ws_search)
    websocket_api.async_register_command(hass, base.ws_fx_rate)
    websocket_api.async_register_command(hass, base.ws_quote)
    websocket_api.async_register_command(hass, base.ws_add)
    websocket_api.async_register_command(hass, base.ws_sell)
    websocket_api.async_register_command(hass, base.ws_edit_transaction)
    websocket_api.async_register_command(hass, base.ws_update)
    websocket_api.async_register_command(hass, base.ws_remove)
    websocket_api.async_register_command(hass, base.ws_preferences)
    websocket_api.async_register_command(hass, base.ws_category_expense)
    websocket_api.async_register_command(hass, base.ws_history)
    websocket_api.async_register_command(hass, ws_indication)
