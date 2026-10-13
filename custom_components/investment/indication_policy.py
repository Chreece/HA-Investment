"""Portfolio-use policy for contribution-only investment indications.

The Indications feature answers which purchases fit the new purchase budget and
selected risk profile. Existing holdings may be consulted only to identify and
remove instruments the user already owns. They must not change candidate market
scores, portfolio-risk limits, suggested amounts, or turn a valid new purchase
into an all-cash result.

The complete-portfolio finalizer remains available in the shared core for future
explicit rebalancing features, but it is never enabled implicitly by this policy.
"""
from __future__ import annotations

from dataclasses import dataclass


_VALID_CONTEXTS = {"use", "ignore"}
_VALID_EXISTING_POLICIES = {"allow", "exclude"}
_VALID_SCOPES = {"discover", "search", "portfolio"}


@dataclass(frozen=True, slots=True)
class IndicationPortfolioPolicy:
    """Resolved runtime policy for one Indications request."""

    requested_context: str
    requested_existing_policy: str
    scope: str
    filter_owned: bool
    display_context: str
    display_existing_policy: str
    core_portfolio_context: str = "ignore"
    core_existing_policy: str = "allow"
    core_overlap_policy: str = "allow"
    core_existing_cash: float = 0.0
    risk_scope: str = "new_contribution_only"
    holdings_use: str = "exact_identity_filter_only"
    complete_portfolio_enforcement: bool = False


def resolve_indication_portfolio_policy(
    portfolio_context: str | None,
    existing_instruments: str | None,
    scope: str | None,
) -> IndicationPortfolioPolicy:
    """Resolve legacy controls to the contribution-only Indications contract.

    ``portfolio_context=use`` is now the single visible owned-investment filter.
    The legacy ``existing_instruments=exclude`` value remains an alias so saved
    preferences and API callers continue to work. The current-portfolio source
    deliberately analyses owned instruments, so filtering is disabled for that
    source while risk evaluation still remains contribution-only.
    """

    context = str(portfolio_context or "use").strip().lower()
    existing = str(existing_instruments or "allow").strip().lower()
    resolved_scope = str(scope or "discover").strip().lower()
    if context not in _VALID_CONTEXTS:
        raise ValueError("Unsupported portfolio context mode")
    if existing not in _VALID_EXISTING_POLICIES:
        raise ValueError("Unsupported existing-instrument policy")
    if resolved_scope not in _VALID_SCOPES:
        raise ValueError("Unsupported indication candidate scope")

    filter_owned = (
        resolved_scope != "portfolio"
        and (context == "use" or existing == "exclude")
    )
    display_context = "use" if filter_owned or resolved_scope == "portfolio" else "ignore"
    display_existing = "exclude" if filter_owned else "allow"
    return IndicationPortfolioPolicy(
        requested_context=context,
        requested_existing_policy=existing,
        scope=resolved_scope,
        filter_owned=filter_owned,
        display_context=display_context,
        display_existing_policy=display_existing,
    )
