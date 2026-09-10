"""Runtime websocket registration for the alternate integration line."""
from __future__ import annotations

import time
from typing import Any, Callable
from uuid import uuid4

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant

from . import websocket as base
from .const import DOMAIN


_INDICATION_FIELDS = {
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
    vol.Optional("whole_unit_categories", default=[]): [vol.In(["crypto", "etf", "stock", "fund", "index", "commodity", "fx", "other"])],
    vol.Optional("portfolio_context", default="use"): vol.In(["use", "ignore"]),
    vol.Optional("existing_instruments", default="allow"): vol.In(["allow", "exclude"]),
    vol.Optional("response_language"): vol.Any(None, str),
}


def _indication_schema(command_type: str) -> dict[Any, Any]:
    return {vol.Required("type"): command_type, **_INDICATION_FIELDS}


def _indication_jobs(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    data = hass.data.get(DOMAIN)
    if not data or "manager" not in data:
        raise RuntimeError("Investment integration is not ready")
    return data.setdefault("indication_progress_jobs", {})


def _prune_jobs(hass: HomeAssistant) -> None:
    jobs = _indication_jobs(hass)
    now = time.monotonic()
    for job_id, job in list(jobs.items()):
        age = now - float(job.get("updated_at") or job.get("created_at") or now)
        if (bool(job.get("done")) and age > 300.0) or age > 1800.0:
            jobs.pop(job_id, None)


def _indication_kwargs(msg: dict[str, Any]) -> dict[str, Any]:
    ai_task_entity_id = msg.get("ai_task_entity_id")
    if not ai_task_entity_id and str(msg.get("ai_agent_id") or "").startswith("ai_task."):
        ai_task_entity_id = msg.get("ai_agent_id")
    return {
        "candidates": msg.get("candidates"),
        "amount": msg.get("amount"),
        "category": msg.get("category"),
        "scope": msg.get("scope"),
        "mode": msg.get("mode", "deterministic"),
        "ai_task_entity_id": ai_task_entity_id,
        "risk_tolerance": msg.get("risk_tolerance", "medium"),
        "horizon": msg.get("horizon", "medium"),
        "strategy": msg.get("strategy", "adaptive"),
        "overlap_policy": msg.get("overlap_policy", "penalize"),
        "overlap_threshold_pct": msg.get("overlap_threshold_pct", 20.0),
        "diversification": msg.get("diversification", "medium"),
        "max_candidate_pct": msg.get("max_candidate_pct"),
        "min_confidence_pct": msg.get("min_confidence_pct", 45.0),
        "min_cash_reserve_pct": msg.get("min_cash_reserve_pct", 0.0),
        "whole_units_only": bool(msg.get("whole_units_only", False)),
        "whole_unit_categories": list(msg.get("whole_unit_categories") or []),
        "portfolio_context": msg.get("portfolio_context", "use"),
        "existing_instruments": msg.get("existing_instruments", "allow"),
        "response_language": msg.get("response_language"),
    }


async def _run_indication(
    hass: HomeAssistant,
    user_id: str,
    msg: dict[str, Any],
    *,
    progress_callback: Callable[[int, str, dict[str, Any] | None], None] | None = None,
) -> dict[str, Any]:
    return await base._manager(hass).async_indication(
        user_id,
        **_indication_kwargs(msg),
        progress_callback=progress_callback,
    )


@websocket_api.websocket_command(_indication_schema("investment/indication"))
@websocket_api.async_response
async def ws_indication(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    try:
        user_id = base._user_id(connection)
        result = await _run_indication(hass, user_id, msg)
        connection.send_result(msg["id"], result)
    except Exception as err:
        connection.send_error(msg["id"], "indication_error", str(err))


@websocket_api.websocket_command(_indication_schema("investment/indication_start"))
@websocket_api.async_response
async def ws_indication_start(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """Start one analysis job and return immediately so the panel can show real progress."""
    try:
        user_id = base._user_id(connection)
        _prune_jobs(hass)
        jobs = _indication_jobs(hass)
        job_id = uuid4().hex
        now = time.monotonic()
        job: dict[str, Any] = {
            "user_id": user_id,
            "percent": 1,
            "stage": "starting",
            "detail": None,
            "done": False,
            "result": None,
            "error": None,
            "created_at": now,
            "updated_at": now,
        }
        jobs[job_id] = job

        def progress(percent: int, stage: str, detail: dict[str, Any] | None) -> None:
            current = jobs.get(job_id)
            if current is None or current.get("done"):
                return
            current["percent"] = max(int(current.get("percent") or 0), min(99, int(percent)))
            current["stage"] = str(stage or "starting")
            current["detail"] = dict(detail) if isinstance(detail, dict) else None
            current["updated_at"] = time.monotonic()

        async def run_job() -> None:
            try:
                result = await _run_indication(
                    hass,
                    user_id,
                    msg,
                    progress_callback=progress,
                )
                current = jobs.get(job_id)
                if current is not None:
                    current.update(
                        percent=100,
                        stage="complete",
                        detail=None,
                        done=True,
                        result=result,
                        updated_at=time.monotonic(),
                    )
            except Exception as err:
                current = jobs.get(job_id)
                if current is not None:
                    current.update(
                        stage="error",
                        done=True,
                        error=str(err),
                        updated_at=time.monotonic(),
                    )

        hass.async_create_task(run_job())
        connection.send_result(msg["id"], {"job_id": job_id})
    except Exception as err:
        connection.send_error(msg["id"], "indication_start_error", str(err))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "investment/indication_status",
        vol.Required("job_id"): vol.All(str, vol.Length(min=8, max=80)),
    }
)
@websocket_api.async_response
async def ws_indication_status(hass: HomeAssistant, connection, msg: dict[str, Any]) -> None:
    """Return progress for the caller's own analysis job."""
    try:
        _prune_jobs(hass)
        jobs = _indication_jobs(hass)
        job_id = str(msg["job_id"])
        job = jobs.get(job_id)
        if job is None or str(job.get("user_id") or "") != base._user_id(connection):
            raise ValueError("Investment indication progress job was not found")
        payload = {
            "job_id": job_id,
            "percent": int(job.get("percent") or 0),
            "stage": str(job.get("stage") or "starting"),
            "detail": job.get("detail") if isinstance(job.get("detail"), dict) else None,
            "done": bool(job.get("done")),
        }
        if payload["done"]:
            payload["result"] = job.get("result")
            payload["error"] = job.get("error")
        connection.send_result(msg["id"], payload)
        if payload["done"]:
            jobs.pop(job_id, None)
    except Exception as err:
        connection.send_error(msg["id"], "indication_status_error", str(err))


def async_register_commands(hass: HomeAssistant) -> None:
    """Register base commands plus the extended analysis commands."""
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
    websocket_api.async_register_command(hass, ws_indication_start)
    websocket_api.async_register_command(hass, ws_indication_status)
