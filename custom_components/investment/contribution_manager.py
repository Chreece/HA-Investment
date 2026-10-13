"""Contribution-only Indications runtime.

Existing holdings are consulted only for exact owned-instrument filtering. They
never enter the candidate score, overlap penalty, allocation risk envelope, or
final purchase gate. The shared complete-portfolio machinery remains available
for a future explicitly named rebalancing feature, not the Indications flow.
"""
from __future__ import annotations

from typing import Any

from .indication_policy import resolve_indication_portfolio_policy
from .runtime_manager import InvestmentManager as PortfolioRuntimeManager


class InvestmentManager(PortfolioRuntimeManager):
    """Expose relatively safe new-buy suggestions independently of old holdings."""

    @staticmethod
    def _validated_indication_preferences(raw: dict[str, Any] | None) -> dict[str, Any]:
        values = PortfolioRuntimeManager._validated_indication_preferences(raw)
        source = raw if isinstance(raw, dict) else {}
        policy = resolve_indication_portfolio_policy(
            source.get("portfolio_context", values.get("portfolio_context")),
            source.get("existing_instruments", values.get("existing_instruments")),
            source.get("scope", values.get("scope")),
        )
        values["portfolio_context"] = policy.display_context
        values["existing_instruments"] = policy.display_existing_policy
        values["overlap_policy"] = policy.core_overlap_policy
        values["overlap_threshold_pct"] = 0.0
        values["existing_cash"] = policy.core_existing_cash
        return values

    async def async_indication(self, user_id: str, **kwargs: Any) -> dict[str, Any]:
        candidates = kwargs.get("candidates")
        resolved_scope = str(
            kwargs.get("scope") or ("search" if candidates else "discover")
        ).strip().lower()
        policy = resolve_indication_portfolio_policy(
            kwargs.get("portfolio_context", "use"),
            kwargs.get("existing_instruments", "allow"),
            resolved_scope,
        )

        core_scope = resolved_scope
        core_candidates = candidates
        if resolved_scope == "portfolio":
            # The portfolio source means "analyse these instruments as possible
            # future purchases", not "make the old portfolio satisfy the newly
            # selected risk profile". Feed the owned instruments as an explicit
            # candidate set while keeping the risk scope contribution-only.
            portfolio = await super().async_portfolio(user_id)
            holdings = list(portfolio.get("holdings") or [])
            core_scope = "search"
            core_candidates = [
                {
                    "provider": holding.get("provider"),
                    "provider_id": holding.get("provider_id"),
                    "symbol": holding.get("symbol"),
                    "name": holding.get("name"),
                    "category": holding.get("category"),
                    "currency": holding.get("currency") or holding.get("quote_currency"),
                    "exchange": holding.get("exchange"),
                    **({"isin": holding.get("isin")} if holding.get("isin") else {}),
                }
                for holding in holdings
                if holding.get("provider") and holding.get("provider_id")
            ]
            if not core_candidates:
                raise ValueError("The current portfolio has no indication candidates")

        core_kwargs = dict(kwargs)
        core_kwargs.update(
            {
                "candidates": core_candidates,
                "scope": core_scope,
                # These are the authoritative safety boundaries for Indications.
                "portfolio_context": policy.core_portfolio_context,
                "existing_instruments": policy.display_existing_policy,
                "overlap_policy": policy.core_overlap_policy,
                "overlap_threshold_pct": 0.0,
                "existing_cash": policy.core_existing_cash,
            }
        )
        result = await super().async_indication(user_id, **core_kwargs)

        if resolved_scope == "portfolio":
            result["scope"] = "portfolio"
            result["source"] = "portfolio"

        preferences = result.setdefault("preferences", {})
        preferences.update(
            {
                "portfolio_context": policy.display_context,
                "existing_instruments": policy.display_existing_policy,
                "overlap_policy": policy.core_overlap_policy,
                "overlap_threshold_pct": 0.0,
                "existing_cash": policy.core_existing_cash,
                "portfolio_filter_only": True,
                "holdings_use": policy.holdings_use,
                "risk_scope": policy.risk_scope,
                "complete_portfolio_enforcement": policy.complete_portfolio_enforcement,
            }
        )

        allocation = result.setdefault("allocation", {})
        allocation["portfolio_filter_only"] = True
        allocation["holdings_use"] = policy.holdings_use
        allocation["risk_scope"] = policy.risk_scope
        portfolio_risk = allocation.get("portfolio_risk")
        if isinstance(portfolio_risk, dict):
            portfolio_risk["scope"] = policy.risk_scope
            portfolio_risk["complete_portfolio_enforcement"] = False

        result["portfolio_context"] = policy.display_context
        result["existing_instruments"] = policy.display_existing_policy
        result["portfolio_filter_only"] = True
        result["holdings_use"] = policy.holdings_use
        result["risk_scope"] = policy.risk_scope
        result["complete_portfolio_enforcement"] = False

        # The wrapped runtime persisted its internal neutral arguments. Restore
        # the visible exact-owned filter and original source so reloads retain
        # the user's choices, while overlap and existing cash remain unable to
        # re-enable old-portfolio influence through stale preferences.
        current_user = await self.store.async_user(user_id)
        stored_preferences = dict(current_user.get("indication_preferences") or {})
        stored_preferences.update(
            {
                "scope": resolved_scope,
                "portfolio_context": policy.display_context,
                "existing_instruments": policy.display_existing_policy,
                "overlap_policy": policy.core_overlap_policy,
                "overlap_threshold_pct": 0.0,
                "existing_cash": policy.core_existing_cash,
            }
        )
        await self.store.async_set_preferences(
            user_id, indication_preferences=stored_preferences
        )
        return result
