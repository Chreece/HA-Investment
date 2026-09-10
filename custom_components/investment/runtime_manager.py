"""Runtime manager extensions for portfolio-context aware ranking."""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from typing import Any, Callable

from .manager import InvestmentManager as CoreInvestmentManager
from .portfolio_context import (
    build_owned_identity_index,
    context_score_fields,
    exposure_profile,
    find_owned_match,
)

_PORTFOLIO_CONTEXT: ContextVar[str] = ContextVar("investment_portfolio_context", default="use")
_EXISTING_POLICY: ContextVar[str] = ContextVar("investment_existing_policy", default="allow")
_OWNED_INDEX: ContextVar[dict[str, dict[str, Any]] | None] = ContextVar("investment_owned_index", default=None)
_FILTER_STATS: ContextVar[dict[str, int] | None] = ContextVar("investment_filter_stats", default=None)

_EXTRA_DEFAULTS = {
    "portfolio_context": "use",
    "existing_instruments": "allow",
}

_EU_BALANCE_UNIVERSE: tuple[dict[str, Any], ...] = (
    {
        "provider": "yahoo",
        "provider_id": "XEON.DE",
        "symbol": "XEON.DE",
        "name": "Xtrackers II EUR Overnight Rate Swap UCITS ETF 1C",
        "category": "etf",
        "currency": "EUR",
        "exchange": "Xetra",
        "isin": "LU0290358497",
    },
    {
        "provider": "yahoo",
        "provider_id": "CEMK.DE",
        "symbol": "CEMK.DE",
        "name": "iShares € Govt Bond 0-1yr UCITS ETF",
        "category": "etf",
        "currency": "EUR",
        "exchange": "Xetra",
    },
    {
        "provider": "yahoo",
        "provider_id": "VAGF.DE",
        "symbol": "VAGF.DE",
        "name": "Vanguard Global Aggregate Bond UCITS ETF EUR Hedged Accumulating",
        "category": "etf",
        "currency": "EUR",
        "exchange": "Xetra",
        "isin": "IE00BG47KH54",
    },
    {
        "provider": "yahoo",
        "provider_id": "VGGF.DE",
        "symbol": "VGGF.DE",
        "name": "Vanguard Global Government Bond UCITS ETF EUR Hedged Accumulating",
        "category": "etf",
        "currency": "EUR",
        "exchange": "Xetra",
        "isin": "IE000B1A2798",
    },
)


class InvestmentManager(CoreInvestmentManager):
    """Extend the base manager without changing the normal integration line."""

    @staticmethod
    def _validated_indication_preferences(raw: dict[str, Any] | None) -> dict[str, Any]:
        values = CoreInvestmentManager._validated_indication_preferences(raw)
        source = raw if isinstance(raw, dict) else {}
        portfolio_context = str(
            source.get("portfolio_context", _PORTFOLIO_CONTEXT.get() or _EXTRA_DEFAULTS["portfolio_context"])
        ).strip().lower()
        existing = str(
            source.get("existing_instruments", _EXISTING_POLICY.get() or _EXTRA_DEFAULTS["existing_instruments"])
        ).strip().lower()
        if portfolio_context not in {"use", "ignore"}:
            raise ValueError("Unsupported portfolio context mode")
        if existing not in {"allow", "exclude"}:
            raise ValueError("Unsupported existing-instrument policy")
        values["portfolio_context"] = portfolio_context
        values["existing_instruments"] = existing
        return values

    async def async_portfolio(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        portfolio = await super().async_portfolio(*args, **kwargs)
        if _PORTFOLIO_CONTEXT.get() != "ignore":
            return portfolio
        neutral = deepcopy(portfolio)
        neutral["holdings"] = []
        neutral["categories"] = []
        neutral["total"] = 0.0
        neutral["today_change"] = 0.0
        neutral["today_pct"] = None
        return neutral

    async def _candidate_portfolio_overlap(
        self,
        asset: dict[str, Any],
        owned_funds: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if _PORTFOLIO_CONTEXT.get() == "use":
            match = find_owned_match(asset, _OWNED_INDEX.get())
            if match is not None:
                return {
                    "known": True,
                    "matched": True,
                    "overlap": 1.0,
                    "sources": [
                        {
                            "symbol": match.get("symbol"),
                            "name": match.get("name"),
                            "overlap": 1.0,
                        }
                    ],
                    "kind": "same_instrument",
                    "method": "portfolio_identity",
                }
        return await super()._candidate_portfolio_overlap(asset, owned_funds)

    async def _discover_indication_candidates(
        self,
        base_currency: str,
        category: str | None,
        legal_region: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        selected, pool_size = await super()._discover_indication_candidates(
            base_currency, category, legal_region
        )

        rows: list[dict[str, Any]] = [dict(row) for row in selected]
        if str(legal_region or "").lower() in {"germany", "eu_eea"} and category in {None, "etf"}:
            rows = [dict(row) for row in _EU_BALANCE_UNIVERSE] + rows
            pool_size += len(_EU_BALANCE_UNIVERSE)

        unique: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = self._candidate_key(row)
            if not all(key) or key in seen:
                continue
            seen.add(key)
            # Broad discovery should surface tradable wrappers rather than raw
            # benchmarks or futures contracts. Explicit category requests remain
            # available for analysis-only inspection.
            if category is None and not bool(exposure_profile(row)["allocatable_default"]):
                continue
            if _EXISTING_POLICY.get() == "exclude" and find_owned_match(row, _OWNED_INDEX.get()) is not None:
                stats = _FILTER_STATS.get()
                if stats is not None:
                    stats["existing"] = int(stats.get("existing", 0)) + 1
                continue
            unique.append(row)
            if len(unique) >= 20:
                break
        return unique, pool_size

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
        whole_unit_categories: list[str] | None = None,
        portfolio_context: str = "use",
        existing_instruments: str = "allow",
        response_language: str | None = None,
        progress_callback: Callable[[int, str, dict[str, Any] | None], None] | None = None,
    ) -> dict[str, Any]:
        portfolio_context = str(portfolio_context or "use").strip().lower()
        existing_instruments = str(existing_instruments or "allow").strip().lower()
        if portfolio_context not in {"use", "ignore"}:
            raise ValueError("Unsupported portfolio context mode")
        if existing_instruments not in {"allow", "exclude"}:
            raise ValueError("Unsupported existing-instrument policy")

        resolved_scope = scope or ("search" if candidates else "discover")
        if resolved_scope == "portfolio" and portfolio_context == "ignore":
            raise ValueError("Portfolio source cannot be used while portfolio context is ignored")
        if resolved_scope == "portfolio" and existing_instruments == "exclude":
            raise ValueError("Portfolio source contains only existing instruments")

        if progress_callback is not None:
            try:
                progress_callback(3, "starting", None)
            except Exception as err:
                _LOGGER.debug("Investment indication runtime progress callback failed: %s", err)
        actual_portfolio = await super().async_portfolio(user_id)
        actual_holdings = list(actual_portfolio.get("holdings") or [])
        owned_index = build_owned_identity_index(actual_holdings)
        stats = {"existing": 0}

        forwarded_candidates = list(candidates or [])
        if resolved_scope == "search" and existing_instruments == "exclude":
            filtered: list[dict[str, Any]] = []
            for row in forwarded_candidates:
                if find_owned_match(row, owned_index) is not None:
                    stats["existing"] += 1
                    continue
                filtered.append(row)
            forwarded_candidates = filtered
            if not forwarded_candidates:
                raise ValueError("No new instruments remain in the current candidate set")

        context_token = _PORTFOLIO_CONTEXT.set(portfolio_context)
        existing_token = _EXISTING_POLICY.set(existing_instruments)
        owned_token = _OWNED_INDEX.set(owned_index)
        stats_token = _FILTER_STATS.set(stats)
        try:
            result = await super().async_indication(
                user_id,
                candidates=forwarded_candidates if resolved_scope == "search" else candidates,
                amount=amount,
                category=category,
                scope=resolved_scope,
                mode=mode,
                ai_task_entity_id=ai_task_entity_id,
                risk_tolerance=risk_tolerance,
                horizon=horizon,
                strategy=strategy,
                overlap_policy=overlap_policy,
                overlap_threshold_pct=overlap_threshold_pct,
                diversification=diversification,
                max_candidate_pct=max_candidate_pct,
                min_confidence_pct=min_confidence_pct,
                min_cash_reserve_pct=min_cash_reserve_pct,
                whole_units_only=whole_units_only,
                whole_unit_categories=whole_unit_categories,
                portfolio_context=portfolio_context,
                existing_instruments=existing_instruments,
                response_language=response_language,
                progress_callback=progress_callback,
            )
        finally:
            _FILTER_STATS.reset(stats_token)
            _OWNED_INDEX.reset(owned_token)
            _EXISTING_POLICY.reset(existing_token)
            _PORTFOLIO_CONTEXT.reset(context_token)

        for item in result.get("results") or []:
            profile = exposure_profile(item)
            if item.get("economic_sleeve"):
                profile["exposure_class"] = item["economic_sleeve"]
                profile["allocatable_default"] = bool(
                    item.get("allocatable_economic_exposure", True)
                )
            item.update(profile)
            item.update(context_score_fields(item))
            item["already_owned"] = find_owned_match(item, owned_index) is not None

        preferences = result.setdefault("preferences", {})
        preferences["portfolio_context"] = portfolio_context
        preferences["existing_instruments"] = existing_instruments
        result["portfolio_context"] = portfolio_context
        result["existing_instruments"] = existing_instruments
        result["existing_filtered_count"] = int(stats.get("existing", 0))
        return result
