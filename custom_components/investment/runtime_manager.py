"""Runtime manager extensions for portfolio-context aware ranking."""
from __future__ import annotations

from contextvars import ContextVar
from copy import deepcopy
from datetime import UTC, datetime
import logging
from typing import Any, Callable

from . import manager as core_manager
from .indication_ownership import build_ownership_snapshot, candidate_ownership_evidence
from .manager import InvestmentManager as CoreInvestmentManager
from .portfolio_context import (
    OwnedIdentityIndex,
    context_score_fields,
    exposure_profile,
    find_owned_match,
)

_PORTFOLIO_CONTEXT: ContextVar[str] = ContextVar("investment_portfolio_context", default="use")
_EXISTING_POLICY: ContextVar[str] = ContextVar("investment_existing_policy", default="allow")
_OWNED_INDEX: ContextVar[OwnedIdentityIndex | None] = ContextVar("investment_owned_index", default=None)
_OWNERSHIP_SNAPSHOT: ContextVar[dict[str, Any] | None] = ContextVar("investment_ownership_snapshot", default=None)
_FINAL_ACCOUNT: ContextVar[dict[str, Any] | None] = ContextVar("investment_final_account", default=None)
_FILTER_STATS: ContextVar[dict[str, Any] | None] = ContextVar("investment_filter_stats", default=None)
_LOGGER = logging.getLogger(__name__)

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

    async def async_portfolio(
        self, user_id: str, *, force: bool = False, refresh_market: bool = False
    ) -> dict[str, Any]:
        if _PORTFOLIO_CONTEXT.get() != "ignore":
            return await super().async_portfolio(
                user_id, force=force, refresh_market=refresh_market
            )
        # The core reads account preferences separately. Contribution-only
        # scoring needs neither old-holdings prices/FX nor display FIFO rows;
        # malformed unrelated ledgers must not prevent a neutral risk scope.
        return {
            "holdings": [], "categories": [], "total": 0.0,
            "today_change": 0.0, "today_pct": None,
        }

    @staticmethod
    def _ownership_snapshot(holdings: Any) -> dict[str, Any]:
        """Use the same clock and HA calendar as the final quantity gate."""
        return build_ownership_snapshot(
            holdings,
            as_of=datetime.fromtimestamp(
                core_manager.time.time(), core_manager.dt_util.now().tzinfo or UTC
            ),
        )

    async def _async_indication_final_account_snapshot(self, user_id: str) -> dict[str, Any]:
        current_user = await super()._async_indication_final_account_snapshot(user_id)
        # Request-local capture lets the outer wrappers enforce exact ownership
        # without another await after the core's final evidence/risk checks.
        _FINAL_ACCOUNT.set(deepcopy(current_user))
        return current_user

    @staticmethod
    def _account_snapshot_vetoed(result: dict[str, Any]) -> bool:
        risk = (result.get("allocation") or {}).get("portfolio_risk") or {}
        return bool(risk.get("portfolio_snapshot_issue"))

    def _finalize_indication_ownership(
        self, result: dict[str, Any], snapshot: dict[str, Any], *, existing_instruments: str
    ) -> None:
        """Check only the selected candidates against the final local ledger."""
        account_vetoed = self._account_snapshot_vetoed(result)
        for item in result.get("results") or []:
            evidence = candidate_ownership_evidence(item, snapshot)
            status = evidence["status"]
            item["already_owned"] = status == "owned" if status != "unknown" else None
            item["ownership_evidence"] = {
                key: evidence.get(key) for key in ("status", "reasons", "holding_id")
            }
            # Preserve the core's explicit, closed account-snapshot result.
            # Otherwise exclusion cannot certify a candidate from an obsolete
            # snapshot, even when its current allocation happens to be zero.
            if existing_instruments == "exclude" and status != "not_owned" and not account_vetoed:
                raise ValueError(
                    "Candidate ownership changed or could not be verified during analysis. "
                    "Check the transaction records and run the analysis again."
                )

    @staticmethod
    def _exclude_owned_candidate(
        asset: dict[str, Any], snapshot: dict[str, Any] | None,
        *, stats: dict[str, Any] | None = None,
    ) -> bool:
        evidence = candidate_ownership_evidence(asset, snapshot)
        if evidence["status"] == "not_owned":
            return False
        if stats is None:
            stats = _FILTER_STATS.get()
        if stats is not None:
            if evidence["status"] == "owned":
                stats["existing"] = int(stats.get("existing", 0)) + 1
            else:
                stats["unknown"] = int(stats.get("unknown", 0)) + 1
                stats.setdefault("unknown_candidates", []).append({
                    key: asset.get(key) for key in
                    ("provider", "provider_id", "symbol", "name", "category")
                } | {
                    "reason": "ownership_unverified",
                    "ownership_evidence": {
                        key: evidence.get(key) for key in ("status", "reasons", "holding_id")
                    },
                })
        return True

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
            if _EXISTING_POLICY.get() == "exclude" and self._exclude_owned_candidate(
                row, _OWNERSHIP_SNAPSHOT.get()
            ):
                continue
            unique.append(row)
            if len(unique) >= 20:
                break
        if not unique and (_FILTER_STATS.get() or {}).get("unknown"):
            raise ValueError(
                "Candidate ownership could not be verified from the transaction records. "
                "Check those records and run the analysis again."
            )
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
        execution_costs: dict[str, Any] | None = None,
        existing_cash: float = 0.0,
        max_drawdown_pct: float | None = None,
        analysis_horizon_weeks: int | None = None,
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
        if existing_instruments == "exclude" or portfolio_context == "use":
            current_user = await self.store.async_user(user_id)
            ownership_snapshot = self._ownership_snapshot(current_user.get("holdings"))
        else:
            # No initial old-holdings work is needed when ownership cannot
            # filter or influence this run. The final badge still uses the
            # verified final ledger rather than an inferred empty portfolio.
            ownership_snapshot = self._ownership_snapshot([])
        owned_index = ownership_snapshot["owned_index"]
        stats = {"existing": 0, "unknown": 0, "unknown_candidates": []}

        forwarded_candidates = list(candidates or [])
        if resolved_scope == "search" and existing_instruments == "exclude":
            filtered: list[dict[str, Any]] = []
            for row in forwarded_candidates:
                if not isinstance(row, dict):
                    continue
                if self._exclude_owned_candidate(row, ownership_snapshot, stats=stats):
                    continue
                filtered.append(row)
            forwarded_candidates = filtered
            if not forwarded_candidates:
                if stats["unknown"]:
                    raise ValueError(
                        "Candidate ownership could not be verified from the transaction records. "
                        "Check those records and run the analysis again."
                    )
                raise ValueError("No new instruments remain in the current candidate set")

        context_token = _PORTFOLIO_CONTEXT.set(portfolio_context)
        existing_token = _EXISTING_POLICY.set(existing_instruments)
        owned_token = _OWNED_INDEX.set(owned_index)
        ownership_token = _OWNERSHIP_SNAPSHOT.set(ownership_snapshot)
        final_account_token = _FINAL_ACCOUNT.set(None)
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
                execution_costs=execution_costs,
                existing_cash=existing_cash,
                max_drawdown_pct=max_drawdown_pct,
                analysis_horizon_weeks=analysis_horizon_weeks,
                response_language=response_language,
                progress_callback=progress_callback,
            )
            final_user = _FINAL_ACCOUNT.get()
        finally:
            _FILTER_STATS.reset(stats_token)
            _FINAL_ACCOUNT.reset(final_account_token)
            _OWNERSHIP_SNAPSHOT.reset(ownership_token)
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

        final_snapshot = self._ownership_snapshot(
            final_user.get("holdings") if isinstance(final_user, dict) else None
        )
        self._finalize_indication_ownership(
            result, final_snapshot, existing_instruments=existing_instruments
        )
        unknown_candidates = stats["unknown_candidates"]
        if unknown_candidates:
            result.setdefault("excluded_candidates", []).extend(unknown_candidates)
            result["excluded_candidate_count"] = len(result["excluded_candidates"])

        preferences = result.setdefault("preferences", {})
        preferences["portfolio_context"] = portfolio_context
        preferences["existing_instruments"] = existing_instruments
        result["portfolio_context"] = portfolio_context
        result["existing_instruments"] = existing_instruments
        result["existing_filtered_count"] = int(stats.get("existing", 0))
        result["ownership_unverified_filtered_count"] = int(stats.get("unknown", 0))
        return result
