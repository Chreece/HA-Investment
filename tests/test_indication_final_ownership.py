"""Production-path ownership checks at the final account snapshot boundary."""
import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from test_indication_evidence_integration import (
    _indication,
    _portfolio_with,
    environment,
    modules,
)
from test_indication_local_ownership import contribution_manager, refuse_display_enrichment


MODES = ["deterministic", "deterministic_ai", "full_ai"]
AWAIT_CASES = [
    (mode, stage)
    for mode in MODES
    for stage in ("provider", "preferences", "ai")
    if mode != "deterministic" or stage != "ai"
]


def holding(asset, *, quantity=2., identifier="held"):
    return {
        **asset, "id": identifier,
        # The displayed quantity deliberately disagrees with the ledger.
        "quantity": 1000000., "value": 99999999.,
        "transactions": [{
            "id": f"{identifier}-buy", "type": "buy", "date": "2020-01-02",
            "quantity": quantity,
        }],
    }


def unrelated_asset(environment):
    return {
        **environment.candidate, "provider_id": "UNRELATED.DE", "symbol": "UNRELATED.DE",
        "name": "Unrelated synthetic holding",
    }


def review_payload():
    return {"structured": {"verdict": "approve", "summary": "Synthetic review", "ranking": []}}


def change_during(environment, stage, change):
    """Change real mocked I/O, without replacing score or purchase calculations."""
    manager = contribution_manager(environment)
    manager._ai_indication_review = AsyncMock(return_value=review_payload())
    changed = False

    def once():
        nonlocal changed
        if not changed:
            changed = True
            change()

    if stage == "provider":
        original = manager.yahoo._chart.side_effect

        async def chart(*args, **kwargs):
            payload = await original(*args, **kwargs)
            once()
            return payload

        manager.yahoo._chart.side_effect = chart
    elif stage == "ai":
        async def review(*args, **kwargs):
            once()
            return review_payload()

        manager._ai_indication_review.side_effect = review
    else:
        writes = 0

        async def persist_preferences(*args, **kwargs):
            nonlocal writes
            writes += 1
            if writes == 2:
                once()

        manager.store.async_set_preferences.side_effect = persist_preferences
    return manager


@pytest.mark.parametrize("mode,stage", AWAIT_CASES)
def test_candidate_bought_during_analysis_cannot_escape_owned_filter(environment, mode, stage):
    def buy():
        environment.manager.store.async_user.return_value["holdings"] = [holding(environment.candidate)]

    manager = change_during(environment, stage, buy)
    with pytest.raises(ValueError, match="ownership.*during analysis"):
        _indication(environment, mode=mode)
    assert manager._ai_indication_review.await_count == (0 if mode == "deterministic" else 1)


@pytest.mark.parametrize("mode,stage", AWAIT_CASES)
def test_unrelated_purchase_during_analysis_does_not_veto_contribution(environment, mode, stage):
    def buy():
        environment.manager.store.async_user.return_value["holdings"] = [holding(unrelated_asset(environment))]

    manager = change_during(environment, stage, buy)
    display = refuse_display_enrichment(environment)
    result = _indication(environment, mode=mode)
    assert result["allocation"]["deployed"] > 0
    assert result["allocation"]["portfolio_risk"]["existing_value"] == 0
    assert result["results"][0]["already_owned"] is False
    assert result["results"][0]["ownership_evidence"]["status"] == "not_owned"
    assert result["risk_scope"] == "new_contribution_only"
    display.assert_not_awaited()
    assert all(call.args[0] == "SAP.DE" for call in manager.yahoo._chart.await_args_list)


@pytest.mark.parametrize("mode,stage", AWAIT_CASES)
def test_allow_owned_policy_uses_fresh_metadata_without_blocking_purchase(environment, mode, stage):
    def buy():
        environment.manager.store.async_user.return_value["holdings"] = [holding(environment.candidate)]

    change_during(environment, stage, buy)
    result = _indication(environment, mode=mode, portfolio_context="ignore", existing_instruments="allow")
    assert result["allocation"]["deployed"] > 0
    assert result["results"][0]["already_owned"] is True
    assert result["results"][0]["ownership_evidence"]["status"] == "owned"
    assert result["allocation"]["portfolio_risk"]["existing_value"] == 0


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("transactions", [None, [None]])
def test_final_unverified_candidate_ownership_is_not_treated_as_absent(environment, mode, transactions):
    def corrupt():
        row = holding(environment.candidate)
        row.update(quantity=0., transactions=deepcopy(transactions))
        environment.manager.store.async_user.return_value["holdings"] = [row]

    change_during(environment, "preferences", corrupt)
    with pytest.raises(ValueError, match="ownership.*could not be verified"):
        _indication(environment, mode=mode)


@pytest.mark.parametrize("aggregate", [0., 999.])
@pytest.mark.parametrize("transactions", [
    None,
    [None],
    [{"id": "oversell", "type": "sell", "date": "2020-01-02", "quantity": 1.}],
])
def test_initial_ownership_never_uses_a_corrupt_ledgers_display_fallback(environment, aggregate, transactions):
    manager = contribution_manager(environment)
    row = holding(environment.candidate)
    row.update(quantity=aggregate, transactions=deepcopy(transactions))
    _portfolio_with(environment, [row])
    with pytest.raises(ValueError, match="ownership could not be verified"):
        _indication(environment)
    manager.yahoo._chart.assert_not_awaited()


@pytest.mark.parametrize("transactions", [None, [], [None], [{"quantity": "bad"}]])
def test_corrupt_unrelated_record_does_not_reach_display_fifo_or_contribution_risk(environment, transactions):
    manager = contribution_manager(environment)
    row = holding(unrelated_asset(environment))
    row["transactions"] = deepcopy(transactions)
    _portfolio_with(environment, [row])
    display = refuse_display_enrichment(environment)

    result = _indication(environment)

    assert result["allocation"]["deployed"] > 0
    assert result["results"][0]["already_owned"] is False
    display.assert_not_awaited()
    assert all(call.args[0] == "SAP.DE" for call in manager.yahoo._chart.await_args_list)


@pytest.mark.parametrize("transactions", [None, [None]])
def test_disabled_owned_filter_preserves_unknown_status_and_contribution_independence(environment, transactions):
    contribution_manager(environment)
    row = holding(environment.candidate)
    row["transactions"] = deepcopy(transactions)
    _portfolio_with(environment, [row])

    result = _indication(environment, portfolio_context="ignore", existing_instruments="allow")

    assert result["allocation"]["deployed"] > 0
    assert result["results"][0]["already_owned"] is None
    assert result["results"][0]["ownership_evidence"]["status"] == "unknown"
    assert result["results"][0]["ownership_evidence"]["reasons"]


def test_mixed_candidates_show_unverified_exclusion_without_hiding_valid_candidate(environment):
    contribution_manager(environment)
    unknown = unrelated_asset(environment)
    row = holding(unknown)
    row["transactions"] = None
    _portfolio_with(environment, [row])

    result = _indication(environment, candidates=[unknown, environment.candidate])

    assert result["allocation"]["deployed"] > 0
    assert [item["symbol"] for item in result["results"]] == ["SAP.DE"]
    assert result["existing_filtered_count"] == 0
    assert result["ownership_unverified_filtered_count"] == 1
    excluded = result["excluded_candidates"]
    assert result["excluded_candidate_count"] == len(excluded) == 1
    assert excluded[0]["symbol"] == "UNRELATED.DE"
    assert excluded[0]["reason"] == "ownership_unverified"
    assert excluded[0]["ownership_evidence"]["status"] == "unknown"


def test_discovery_applies_strict_ownership_before_requesting_candidate_market_data(environment, monkeypatch):
    manager = contribution_manager(environment)
    unknown = unrelated_asset(environment)
    row = holding(unknown)
    row["transactions"] = [None]
    _portfolio_with(environment, [row])
    discover = AsyncMock(return_value=([unknown, environment.candidate], 2))
    monkeypatch.setattr(environment.modules.core.InvestmentManager, "_discover_indication_candidates", discover)

    result = _indication(environment, scope="discover", candidates=None, category="stock")

    assert [item["symbol"] for item in result["results"]] == ["SAP.DE"]
    assert result["ownership_unverified_filtered_count"] == 1
    assert result["allocation"]["deployed"] > 0
    assert all(call.args[0] == "SAP.DE" for call in manager.yahoo._chart.await_args_list)


def test_current_portfolio_source_uses_only_verified_positive_personal_holdings(environment):
    manager = contribution_manager(environment)
    positive = holding(environment.candidate)
    sold = holding(unrelated_asset(environment), identifier="sold")
    sold["transactions"].append({
        "id": "sold-sell", "type": "sell", "date": "2020-02-02", "quantity": 2.,
    })
    shared = holding(unrelated_asset(environment), identifier="shared")
    shared["transactions"][0]["shared_allocations"] = [{"participant": "Fixture guest", "quantity": 2.}]
    unknown = holding(unrelated_asset(environment), identifier="unknown")
    unknown["transactions"] = [None]
    unavailable = holding({
        **unrelated_asset(environment), "provider": None, "provider_id": None,
        "symbol": "NO_FEED", "isin": "DE0007164600",
    }, identifier="unavailable")
    _portfolio_with(environment, [positive, sold, shared, unknown, unavailable])
    display = refuse_display_enrichment(environment)

    result = _indication(environment, scope="portfolio")

    assert result["selected_candidate_count"] == 1
    assert [item["symbol"] for item in result["results"]] == ["SAP.DE"]
    assert result["allocation"]["deployed"] > 0
    assert result["results"][0]["already_owned"] is True
    exclusions = result["excluded_candidates"]
    assert result["excluded_candidate_count"] == 2
    assert result["portfolio_source_excluded_count"] == 2
    assert result["ownership_unverified_filtered_count"] == 1
    assert [(row["symbol"], row["reason"], row["ownership_evidence"]["status"]) for row in exclusions] == [
        ("UNRELATED.DE", "ownership_unverified", "unknown"),
        ("NO_FEED", "requested_provider_identity_unavailable", "owned"),
    ]
    assert all("transactions" not in row and "value" not in row for row in exclusions)
    display.assert_not_awaited()
    assert all(call.args[0] == "SAP.DE" for call in manager.yahoo._chart.await_args_list)


@pytest.mark.parametrize("mode,stage", AWAIT_CASES)
def test_sale_during_analysis_invalidates_current_portfolio_source(environment, mode, stage):
    _portfolio_with(environment, [holding(environment.candidate)])

    def sell():
        environment.manager.store.async_user.return_value["holdings"][0]["transactions"].append({
            "id": "final-sell", "type": "sell", "date": "2020-02-02", "quantity": 2.,
        })

    change_during(environment, stage, sell)
    with pytest.raises(ValueError, match="current-portfolio candidate source changed"):
        _indication(environment, scope="portfolio", mode=mode)


@pytest.mark.parametrize("mode", MODES)
def test_partial_sale_preserves_current_portfolio_membership_without_old_quantity_risk(environment, mode):
    _portfolio_with(environment, [holding(environment.candidate, quantity=3.)])

    def sell():
        environment.manager.store.async_user.return_value["holdings"][0]["transactions"].append({
            "id": "partial-sell", "type": "sell", "date": "2020-02-02", "quantity": 2.,
        })

    change_during(environment, "preferences", sell)
    result = _indication(environment, scope="portfolio", mode=mode)
    assert result["results"][0]["already_owned"] is True
    assert result["allocation"]["deployed"] > 0
    assert result["allocation"]["portfolio_risk"]["existing_value"] == 0


def test_sale_during_unfiltered_search_updates_owned_badge(environment):
    _portfolio_with(environment, [holding(environment.candidate)])

    def sell():
        environment.manager.store.async_user.return_value["holdings"][0]["transactions"].append({
            "id": "final-sell", "type": "sell", "date": "2020-02-02", "quantity": 2.,
        })

    change_during(environment, "preferences", sell)
    result = _indication(environment, portfolio_context="ignore")
    assert result["results"][0]["already_owned"] is False
    assert result["allocation"]["deployed"] > 0


def test_failing_progress_callback_cannot_abort_analysis(environment):
    contribution_manager(environment)

    def broken_progress(*args, **kwargs):
        raise RuntimeError("Synthetic progress transport unavailable")

    result = _indication(environment, progress_callback=broken_progress)
    assert result["allocation"]["deployed"] > 0


@pytest.mark.parametrize("second_scope", ["search", "portfolio"])
def test_concurrent_profiles_keep_final_snapshots_and_preferences_request_local(environment, second_scope):
    manager = contribution_manager(environment)
    original = manager.store.async_user.return_value
    users = {"filter-user": deepcopy(original), "allow-user": deepcopy(original)}
    if second_scope == "portfolio":
        users["allow-user"]["holdings"] = [holding(environment.candidate)]
    writes = {key: 0 for key in users}

    async def exercise():
        final_writes = set()
        both_final_writes = asyncio.Event()

        async def user(user_id):
            return deepcopy(users[user_id])

        async def persist(user_id, **kwargs):
            writes[user_id] += 1
            users[user_id]["indication_preferences"] = deepcopy(kwargs["indication_preferences"])
            if writes[user_id] == 2:
                final_writes.add(user_id)
                if len(final_writes) == 2:
                    users["filter-user"]["holdings"] = [holding(environment.candidate)]
                    both_final_writes.set()
                await asyncio.wait_for(both_final_writes.wait(), timeout=5)

        manager.store.async_user.side_effect = user
        manager.store.async_set_preferences.side_effect = persist
        options = {
            "candidates": [deepcopy(environment.candidate)], "amount": 1000.,
            "risk_tolerance": "very_high", "scope": "search",
            "execution_costs": {"confirmed": True, "fixed_fee": 0., "commission_pct": 0.,
                                "spread_bps": 0., "fx_bps": 0.},
        }
        return await asyncio.gather(
            manager.async_indication("filter-user", **options, portfolio_context="use"),
            manager.async_indication("allow-user", **{**options, "scope": second_scope}, portfolio_context="ignore"),
            return_exceptions=True,
        )

    rejected, allowed = asyncio.run(exercise())
    assert isinstance(rejected, ValueError), rejected
    assert "ownership" in str(rejected)
    assert isinstance(allowed, dict), allowed
    assert allowed["allocation"]["deployed"] > 0
    assert allowed["scope"] == second_scope
    assert allowed["results"][0]["already_owned"] is (second_scope == "portfolio")
    assert users["filter-user"]["indication_preferences"]["existing_instruments"] == "exclude"
    assert users["allow-user"]["indication_preferences"]["existing_instruments"] == "allow"
    assert users["allow-user"]["indication_preferences"]["scope"] == second_scope
