"""Held records must prove their own identity even when market data are reused."""
from copy import deepcopy
import json

import pytest

from test_indication_evidence_integration import (
    DAY,
    NOW,
    _indication,
    _portfolio_with,
    environment as environment,
    modules as modules,
)


@pytest.mark.parametrize("assertions, reason", [
    ({"isin": "US0378331005"}, "provider_isin_missing"),
    ({"symbol": "MSFT"}, "requested_symbol_mismatch"),
    ({"category": "etf"}, "provider_product_type_mismatch"),
    ({"exchange": "NASDAQ"}, "exchange_mismatch"),
    ({"currency": "USD"}, "quote_currency_mismatch"),
    ({"share_class": "Class B"}, "share_class_mismatch"),
    ({"share_class_currency": "USD"}, "share_class_currency_mismatch"),
])
def test_held_assertion_conflict_cannot_borrow_candidate_identity(environment, assertions, reason):
    held = {**environment.candidate, "id": "held-record", "quantity": 1.,
            "value": 100., **assertions}
    _portfolio_with(environment, [held])
    result = _indication(environment)
    portfolio = result["allocation"]["portfolio_risk"]
    assert result["allocation"]["deployed"] == 0.
    assert portfolio["status"] == "unknown"
    assert portfolio["existing_value"] is None
    assert any(reason in issue["reasons"] for issue in portfolio["existing_position_issues"])
    # The same observed quote and histories suffice to reject the incompatible
    # ledger record; there is no alternate provider or speculative lookup.
    requests = [call.args for call in environment.manager.yahoo._chart.await_args_list]
    assert requests.count(("SAP.DE", "5d", "1d")) == 1
    assert requests.count(("SAP.DE", "1y", "1d")) == 1
    assert requests.count(("SAP.DE", "5y", "1d")) == 1
    json.dumps(result, allow_nan=False)


def test_known_good_holding_reuses_sources_and_counts_its_own_units(environment):
    _portfolio_with(environment, [{**environment.candidate, "id": "held-record",
                                  "quantity": 2., "value": 9999.}])
    result = _indication(environment, existing_cash=100.)
    portfolio = result["allocation"]["portfolio_risk"]
    assert result["allocation"]["deployed"] > 0.
    assert portfolio["status"] == "within_limits"
    assert portfolio["existing_position_issues"] == []
    assert portfolio["existing_value"] == pytest.approx(2 * result["results"][0]["portfolio_price"])
    assert len(environment.manager.yahoo._chart.await_args_list) == 3


@pytest.mark.parametrize("corrupt_first", [True, False])
def test_each_duplicate_candidate_key_validates_its_own_held_assertions(environment, corrupt_first):
    valid = {**environment.candidate, "id": "correct", "quantity": 1., "value": 100.}
    conflict = {**environment.candidate, "id": "conflicting", "quantity": 2.,
                "value": 200., "isin": "US0378331005"}
    _portfolio_with(environment, [conflict, valid] if corrupt_first else [valid, conflict])
    result = _indication(environment)
    portfolio = result["allocation"]["portfolio_risk"]
    assert result["allocation"]["deployed"] == 0.
    assert portfolio["status"] == "unknown"
    assert len(portfolio["existing_position_issues"]) == 1
    assert "provider_isin_missing" in portfolio["existing_position_issues"][0]["reasons"]
    assert len(environment.manager.yahoo._chart.await_args_list) == 3


def _adapt_held_fund_source(environment):
    original = environment.manager.yahoo._chart.side_effect

    async def chart(provider_id, range_, interval, **kwargs):
        payload = await original(provider_id, range_, interval, **kwargs)
        payload["meta"].update(symbol=provider_id,
                               instrumentType="ETF" if provider_id == "SXR8.DE" else "EQUITY")
        return payload

    environment.manager.yahoo._chart.side_effect = chart
    return {**environment.candidate, "provider_id": "SXR8.DE", "symbol": "SXR8.DE",
            "name": "iShares Core S&P 500 UCITS ETF USD Accumulating", "category": "etf",
            "quantity": 1., "value": 100.}


@pytest.mark.parametrize("corrupt_first", [True, False])
def test_deduplicated_held_source_never_erases_a_conflicting_ledger_record(environment, corrupt_first):
    base = _adapt_held_fund_source(environment)
    valid = {**base, "id": "correct"}
    conflict = {**base, "id": "conflicting", "isin": "IE00B4L5Y983"}
    _portfolio_with(environment, [conflict, valid] if corrupt_first else [valid, conflict])
    result = _indication(environment)
    portfolio = result["allocation"]["portfolio_risk"]
    assert result["allocation"]["deployed"] == 0.
    assert portfolio["status"] == "unknown"
    assert portfolio["existing_value"] is None
    assert any("isin_mismatch" in issue["reasons"] for issue in portfolio["existing_position_issues"])
    quote_requests = [call.args for call in environment.manager.yahoo._chart.await_args_list
                      if call.args[1:] == ("5d", "1d")]
    assert quote_requests.count(("SXR8.DE", "5d", "1d")) == 1


def test_compatible_held_duplicates_reuse_one_fund_feed_and_count_both_quantities(environment):
    base = _adapt_held_fund_source(environment)
    _portfolio_with(environment, [{**base, "id": "one"},
                                  {**base, "id": "two", "quantity": 2., "value": 200.}])
    result = _indication(environment, existing_cash=1000.)
    portfolio = result["allocation"]["portfolio_risk"]
    assert result["allocation"]["deployed"] > 0.
    assert portfolio["existing_position_issues"] == []
    assert portfolio["existing_value"] == pytest.approx(3 * environment.quote["meta"]["regularMarketPrice"])
    held_requests = [call.args for call in environment.manager.yahoo._chart.await_args_list
                     if call.args[0] == "SXR8.DE"]
    assert len(held_requests) == 3


def test_rechecking_held_identity_cannot_promote_stale_shared_source(environment):
    _portfolio_with(environment, [{**environment.candidate, "quantity": 1., "value": 100.}])
    environment.quote["meta"]["regularMarketTime"] = NOW - 10 * DAY
    result = _indication(environment)
    portfolio = result["allocation"]["portfolio_risk"]
    assert result["allocation"]["deployed"] == 0.
    assert portfolio["status"] == "unknown"
    assert any("quote_stale" in issue["reasons"] for issue in portfolio["existing_position_issues"])


def test_confirmed_zero_owner_units_do_not_require_identity_or_market_history(environment):
    held = {**environment.candidate, "provider_id": "UNVERIFIED.DE", "symbol": "WRONG",
            "isin": "US0378331005", "quantity": 100., "value": 10000.,
            "transactions": [{"id": "fully-shared", "type": "buy", "date": "2020-01-02",
                              "quantity": 100., "shared_allocations": [
                                  {"participant": "Other owner", "quantity": 100.}]}]}
    original = deepcopy(held)
    _portfolio_with(environment, [held])
    result = _indication(environment)
    portfolio = result["allocation"]["portfolio_risk"]
    assert result["allocation"]["deployed"] > 0.
    assert portfolio["existing_value"] == 0.
    assert portfolio["existing_position_issues"] == []
    assert all(call.args[0] != "UNVERIFIED.DE" for call in environment.manager.yahoo._chart.await_args_list)
    assert held == original
