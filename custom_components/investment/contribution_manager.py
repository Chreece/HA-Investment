"""Contribution-only Indications runtime.

Existing holdings are consulted only for exact owned-instrument filtering. They
never enter the candidate score, overlap penalty, allocation risk envelope, or
final purchase gate. The shared complete-portfolio machinery remains available
for a future explicitly named rebalancing feature, not the Indications flow.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from .affordable_lots import recover_affordable_minimum_lots
from .indication_ownership import candidate_ownership_evidence
from .indication_policy import resolve_indication_portfolio_policy
from .runtime_manager import InvestmentManager as PortfolioRuntimeManager


_VISIBLE_PREFERENCES: ContextVar[dict[str, Any] | None] = ContextVar(
    "investment_contribution_visible_preferences", default=None
)


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

    async def _async_indication_final_account_snapshot(self, user_id: str) -> dict[str, Any]:
        # Persist the visible filter/source before the final account read. The
        # core then checks this same account, source evidence and purchase plan
        # synchronously; no wrapper I/O may reopen that verified boundary.
        updates = _VISIBLE_PREFERENCES.get()
        if updates is not None:
            current_user = await self.store.async_user(user_id)
            stored_preferences = dict(current_user.get("indication_preferences") or {})
            stored_preferences.update(updates)
            await self.store.async_set_preferences(
                user_id, indication_preferences=stored_preferences
            )
        return await super()._async_indication_final_account_snapshot(user_id)

    def _finalize_indication_ownership(
        self, result: dict[str, Any], snapshot: dict[str, Any], *, existing_instruments: str
    ) -> None:
        super()._finalize_indication_ownership(
            result, snapshot, existing_instruments=existing_instruments
        )
        if (_VISIBLE_PREFERENCES.get() or {}).get("scope") != "portfolio":
            return
        if self._account_snapshot_vetoed(result):
            return
        for item in result.get("results") or []:
            if candidate_ownership_evidence(item, snapshot)["status"] != "owned":
                raise ValueError(
                    "The current-portfolio candidate source changed or could not be verified "
                    "during analysis. Check the transaction records and run the analysis again."
                )

    @staticmethod
    def _source_identity_fields(holding: dict[str, Any]) -> dict[str, str]:
        """Copy bounded display identifiers, never corrupt nested source values."""
        return {
            key: holding[key][:256]
            for key in ("provider", "provider_id", "symbol", "name", "category")
            if isinstance(holding.get(key), str) and holding[key].strip()
        }

    def _unverified_portfolio_sources(
        self, raw_holdings: Any, snapshot: dict[str, Any]
    ) -> list[dict[str, Any]]:
        exclusions = []
        raw_rows = raw_holdings if isinstance(raw_holdings, list) else []
        for issue in snapshot["issues"]:
            identifier = issue.get("holding_id")
            symbol = issue.get("symbol")
            raw = next((
                holding for holding in raw_rows
                if isinstance(holding, dict)
                and (
                    isinstance(identifier, str) and holding.get("id") == identifier
                    or identifier is None and isinstance(symbol, str) and holding.get("symbol") == symbol
                )
            ), {})
            display = self._source_identity_fields(raw)
            if not any(display.get(key) for key in ("symbol", "name", "provider_id")):
                display["symbol"] = identifier[:256] if isinstance(identifier, str) else "—"
            exclusions.append({
                **display, "reason": "ownership_unverified",
                "ownership_evidence": {
                    "status": "unknown", "holding_id": identifier,
                    "reasons": list(issue.get("reasons") or ["ownership_quantity_unverified"]),
                },
            })
        return exclusions

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
        source_exclusions: list[dict[str, Any]] = []
        source_unknown_count = 0
        if resolved_scope == "portfolio":
            # The portfolio source means "analyse these instruments as possible
            # future purchases", not "make the old portfolio satisfy the newly
            # selected risk profile". Feed the owned instruments as an explicit
            # candidate set while keeping the risk scope contribution-only.
            current_user = await self.store.async_user(user_id)
            source_snapshot = self._ownership_snapshot(current_user.get("holdings"))
            holdings = source_snapshot["holdings"]
            source_exclusions = self._unverified_portfolio_sources(
                current_user.get("holdings"), source_snapshot
            )
            source_unknown_count = len(source_exclusions)
            unavailable = [
                holding for holding in holdings
                if holding.get("provider") not in self.providers or not holding.get("provider_id")
            ]
            source_exclusions.extend({
                **self._source_identity_fields(holding),
                "reason": "requested_provider_identity_unavailable",
                "ownership_evidence": {
                    "status": "owned", "holding_id": holding.get("id"), "reasons": [],
                },
            } for holding in unavailable)
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
                if holding.get("provider") in self.providers and holding.get("provider_id")
            ]
            if not core_candidates:
                if source_snapshot["issues"] or source_snapshot["unidentified_unknown"]:
                    raise ValueError(
                        "Current portfolio ownership could not be verified from the transaction "
                        "records. Check those records and run the analysis again."
                    )
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
        visible_preferences = {
            "scope": resolved_scope,
            "portfolio_context": policy.display_context,
            "existing_instruments": policy.display_existing_policy,
            "overlap_policy": policy.core_overlap_policy,
            "overlap_threshold_pct": 0.0,
            "existing_cash": policy.core_existing_cash,
        }
        preferences_token = _VISIBLE_PREFERENCES.set(visible_preferences)
        try:
            result = await super().async_indication(user_id, **core_kwargs)
        finally:
            _VISIBLE_PREFERENCES.reset(preferences_token)

        # The final ownership, currency and source checks have completed.
        # Recovery and response decoration are pure work: there are no awaits
        # between the validated snapshot and the returned purchase plan.
        result = recover_affordable_minimum_lots(
            result,
            budget=kwargs.get("amount"),
            risk_tolerance=str(kwargs.get("risk_tolerance") or "medium"),
        )

        if resolved_scope == "portfolio":
            result["scope"] = "portfolio"
            result["source"] = "portfolio"
            # A valid source holding must not hide uncertain peers. Keep the
            # response bounded like the 20-candidate analysis, and expose the
            # total separately when more exclusions exist than can be shown.
            if source_exclusions:
                result.setdefault("excluded_candidates", []).extend(source_exclusions[:20])
                result["excluded_candidate_count"] = len(result["excluded_candidates"])
                result["portfolio_source_excluded_count"] = len(source_exclusions)
                result["portfolio_source_exclusions_truncated"] = len(source_exclusions) > 20
                result["ownership_unverified_filtered_count"] = (
                    result.get("ownership_unverified_filtered_count", 0) + source_unknown_count
                )

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

        return result
