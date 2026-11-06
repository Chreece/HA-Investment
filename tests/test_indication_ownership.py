"""Owned filtering must distinguish verified absence from ledger uncertainty."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import importlib
import json
import math
from pathlib import Path
import sys
from types import ModuleType

import pytest


COMP = Path(__file__).resolve().parents[1] / "custom_components" / "investment"
PACKAGE = "investment_ownership_unit_test"
package = ModuleType(PACKAGE)
package.__path__ = [str(COMP)]
sys.modules[PACKAGE] = package
subject = importlib.import_module(f"{PACKAGE}.indication_ownership")
AS_OF = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def buy(quantity=3., **changes):
    return {"id": "buy", "type": "buy", "date": "2026-01-02", "quantity": quantity, **changes}


def sell(quantity=3., **changes):
    return {"id": "sell", "type": "sell", "date": "2026-02-02", "quantity": quantity, **changes}


def holding(**changes):
    return {
        "id": "held", "provider": "yahoo", "provider_id": "EUNL.DE",
        "symbol": "EUNL.DE", "category": "etf", "currency": "EUR",
        "exchange": "XETRA", "isin": "IE00B4L5Y983", "quantity": 999.,
        "transactions": [buy()], **changes,
    }


def candidate(**changes):
    return {
        "provider": "yahoo", "provider_id": "EUNL.DE", "symbol": "EUNL.DE",
        "category": "etf", "currency": "EUR", "exchange": "XETRA",
        "isin": "IE00B4L5Y983", **changes,
    }


def unrelated_candidate():
    return candidate(provider_id="OTHER.DE", symbol="OTHER.DE", isin="IE00BK5BQT80")


def snapshot(*holdings, as_of=AS_OF):
    return subject.build_ownership_snapshot(list(holdings), as_of=as_of)


def evidence(state, item=None):
    return subject.candidate_ownership_evidence(candidate() if item is None else item, state)


@pytest.mark.parametrize("aggregate", [0, None, True, -2, math.nan, math.inf, 10**500])
def test_valid_units_override_every_display_aggregate_without_leaking_invalid_numbers(aggregate):
    state = snapshot(holding(quantity=aggregate, personal_quantity=0, value=math.nan))
    result = evidence(state)
    assert result["status"] == "owned"
    assert result["quantity"] == 3
    assert result["holding_id"] == "held"
    assert state["holdings"][0]["personal_quantity"] == 3
    assert result["holding"]["position_quantity_evidence"]["stored_aggregate_used"] is False
    assert "transactions" not in result["holding"]
    assert "value" not in result["holding"]
    json.dumps(state, allow_nan=False)


@pytest.mark.parametrize("transactions,reason", [
    (None, "position_ledger_missing"),
    ([], "position_ledger_missing"),
    ([None], "position_ledger_transaction_invalid"),
    ([buy(), buy()], "position_ledger_duplicate_transaction"),
    ([buy(-1)], "position_ledger_buy_quantity_invalid"),
    ([buy(True)], "position_ledger_buy_quantity_invalid"),
    ([buy(math.nan)], "position_ledger_buy_quantity_invalid"),
    ([buy(math.inf)], "position_ledger_buy_quantity_invalid"),
    ([buy("3")], "position_ledger_buy_quantity_invalid"),
    ([buy(date="2026-11-07")], "position_ledger_future_date"),
    ([buy(created_at=int((AS_OF + timedelta(seconds=1)).timestamp()))], "position_ledger_future_creation_time"),
    ([sell(), buy(date="2026-03-02")], "position_ledger_oversell"),
    ([buy(), sell(4)], "position_ledger_oversell"),
    ([buy(), sell(1, date="2026-01-02")], "position_ledger_transaction_order_ambiguous"),
    ([buy(shared_allocations=[{"participant": "Friend", "quantity": 4}])], "position_ledger_shared_units_exceed_net"),
    ([buy(net_quantity=2)], "position_ledger_net_quantity_conflict"),
])
def test_invalid_ledger_is_unknown_for_matched_candidate_and_local_to_its_instrument(transactions, reason):
    state = snapshot(holding(quantity=0, transactions=transactions))
    assert state["holdings"] == []
    assert state["unidentified_unknown"] is False
    assert state["reasons"] == []
    result = evidence(state)
    assert result["status"] == "unknown"
    assert result["holding_id"] == "held"
    assert reason in result["reasons"]
    assert result["holding"]["quantity"] is None
    assert evidence(state, unrelated_candidate())["status"] == "not_owned"
    json.dumps(state, allow_nan=False)


def test_missing_ledger_is_not_certified_from_a_positive_aggregate_either():
    row = holding(quantity=8)
    del row["transactions"]
    assert evidence(snapshot(row))["status"] == "unknown"


@pytest.mark.parametrize("transactions", [
    [buy(), sell()],
    [buy(shared_allocations=[{"participant": "Friend", "quantity": 3}])],
])
def test_verified_zero_personal_units_are_not_owned_despite_positive_aggregate(transactions):
    state = snapshot(holding(quantity=999., transactions=transactions))
    assert state["holdings"] == []
    assert state["unknown_index"] == {}
    assert state["issues"] == []
    assert evidence(state)["status"] == "not_owned"


def test_personal_share_and_sale_are_counted_without_prices_or_fx():
    state = snapshot(holding(transactions=[
        buy(10, shared_allocations=[{"participant": "Friend", "quantity": 3}]),
        sell(2),
    ]))
    assert evidence(state)["quantity"] == 5


@pytest.mark.parametrize("quantity", [1e-13, 5e-324])
def test_any_representable_positive_personal_remainder_is_owned(quantity):
    state = snapshot(holding(quantity=0, transactions=[buy(quantity)]))
    assert evidence(state)["status"] == "owned"
    assert evidence(state)["quantity"] == quantity


@pytest.mark.parametrize("book", [None, {}, "", True, [None], ["broken"]])
def test_missing_or_malformed_book_cannot_certify_candidate_absence(book):
    state = subject.build_ownership_snapshot(book, as_of=AS_OF)
    assert state["unidentified_unknown"] is True
    assert evidence(state)["status"] == "unknown"
    assert evidence(state)["reasons"]


def test_empty_book_is_verified_absence():
    state = snapshot()
    assert state["unidentified_unknown"] is False
    assert evidence(state)["status"] == "not_owned"


@pytest.mark.parametrize("quantity,transactions", [(5, None), (0, [buy()])])
def test_unidentifiable_possible_position_does_not_disappear(quantity, transactions):
    state = snapshot({"id": "unknown", "quantity": quantity, "transactions": transactions})
    assert state["unidentified_unknown"] is True
    assert evidence(state)["status"] == "unknown"
    assert "ownership_identity_unavailable" in evidence(state)["reasons"]


def test_verified_zero_does_not_require_identity_to_establish_no_personal_exposure():
    state = snapshot({"transactions": [buy(), sell()], "symbol": {"invalid": "metadata"}})
    assert state["unidentified_unknown"] is False
    assert evidence(state)["status"] == "not_owned"


@pytest.mark.parametrize("field,value", [("isin", True), ("provider_id", {}), ("symbol", 123), ("exchange", "x" * 257)])
def test_corrupt_identity_metadata_is_not_stringified_into_a_false_match(field, value):
    state = snapshot(holding(**{field: value}))
    assert state["holdings"] == []
    assert evidence(state)["status"] == "unknown"
    assert evidence(state)["reasons"] == ["ownership_identity_unavailable"]


def test_known_owned_match_is_decisive_despite_a_separate_unidentified_record():
    state = snapshot(holding(), None)
    assert state["unidentified_unknown"] is True
    assert evidence(state)["status"] == "owned"
    assert evidence(state, unrelated_candidate())["status"] == "unknown"


def test_known_owned_match_wins_over_an_uncertain_record_for_the_same_instrument():
    state = snapshot(holding(id="known"), holding(id="uncertain", transactions=[]))
    assert evidence(state)["status"] == "owned"
    assert evidence(state)["holding_id"] == "known"


def test_same_global_identity_matches_another_venue_even_with_distinct_listing_figi():
    state = snapshot(holding(figi="BBG000BLNNH6"))
    result = evidence(state, candidate(provider="other", provider_id="SWDA.L", symbol="SWDA.L",
                                       exchange="LSE", currency="GBP", figi="BBG000B9XRY4"))
    assert result["status"] == "owned"


def test_identity_conflict_cannot_be_overridden_by_a_shared_provider_alias():
    state = snapshot(holding())
    assert evidence(state, candidate(isin="IE00BK5BQT80"))["status"] == "not_owned"


def test_unknown_ledger_does_not_override_contradictory_global_identity():
    state = snapshot(holding(transactions=[]))
    assert evidence(state, candidate(isin="IE00BK5BQT80"))["status"] == "not_owned"


@pytest.mark.parametrize("transactions", [[buy()], None, []])
@pytest.mark.parametrize("missing_side", ["held", "candidate", "both"])
def test_shared_bare_alias_with_missing_venue_is_scoped_unknown_instead_of_absence(transactions, missing_side):
    held = {"id": "bare", "symbol": "SAP", "category": "stock", "transactions": transactions}
    target = {
        "provider": "yahoo", "provider_id": "SAP", "symbol": "SAP",
        "category": "stock", "exchange": "XETRA", "currency": "EUR",
    }
    if missing_side == "candidate":
        held["exchange"] = target.pop("exchange")
    elif missing_side == "both":
        target.pop("exchange")
    state = snapshot(held)
    result = evidence(state, target)
    assert state["unidentified_unknown"] is False
    assert result["status"] == "unknown"
    assert result["holding_id"] == "bare"
    assert "ownership_identity_ambiguous" in result["reasons"]
    if transactions is None or transactions == []:
        assert "position_ledger_missing" in result["reasons"]
    different = {**target, "provider_id": "MSFT", "symbol": "MSFT"}
    assert evidence(state, different)["status"] == "not_owned"


@pytest.mark.parametrize("field,held_value,candidate_value", [
    ("isin", "IE00B4L5Y983", "IE00BK5BQT80"),
    ("share_class_figi", "BBG001S5XBN5", "BBG001S5N8V8"),
    ("figi", "BBG000BLNNH6", "BBG000B9XRY4"),
    ("exchange", "LSE", "XETRA"),
    ("currency", "USD", "EUR"),
    ("currency", "GBp", "GBP"),
])
def test_shared_weak_alias_with_explicit_conflict_is_not_ambiguous(field, held_value, candidate_value):
    held = {"id": "bare", "symbol": "SAP", "category": "stock", "transactions": [], field: held_value}
    target = {
        "provider": "yahoo", "provider_id": "SAP", "symbol": "SAP",
        "category": "stock", field: candidate_value,
    }
    assert evidence(snapshot(held), target)["status"] == "not_owned"


@pytest.mark.parametrize("identifier,value", [("isin", "IE00B4L5Y983"), ("share_class_figi", "BBG001S5XBN5")])
def test_shared_global_identity_remains_decisive_over_weak_alias_ambiguity(identifier, value):
    held = {"id": "bare", "symbol": "SAP", "category": "stock", "transactions": [buy()], identifier: value}
    target = {"provider": "yahoo", "provider_id": "SAP", "symbol": "SAP", "category": "stock", identifier: value}
    assert evidence(snapshot(held), target)["status"] == "owned"


def test_alias_collision_retains_later_exact_uncertain_record():
    state = snapshot(
        holding(id="other", isin="IE00BK5BQT80", transactions=[]),
        holding(id="exact", transactions=[]),
    )
    result = evidence(state)
    assert result["status"] == "unknown"
    assert result["holding_id"] == "exact"


@pytest.mark.parametrize("identifier", [False, 123, "", " invalid "])
def test_invalid_holding_identity_is_local_uncertainty(identifier):
    state = snapshot(holding(id=identifier))
    assert evidence(state)["status"] == "unknown"
    assert evidence(state)["reasons"] == ["ownership_holding_id_invalid"]
    assert evidence(state, unrelated_candidate())["status"] == "not_owned"


def test_duplicate_holding_records_cannot_be_presented_as_verified_positions():
    state = snapshot(holding(), holding(transactions=[]))
    assert state["holdings"] == []
    assert evidence(state)["status"] == "unknown"
    assert evidence(state)["reasons"] == ["ownership_holding_id_duplicate"]


@pytest.mark.parametrize("as_of", [None, "2026-11-06", datetime(2026, 11, 6)])
def test_invalid_reference_time_is_unknown_even_when_book_is_empty(as_of):
    assert evidence(snapshot(as_of=as_of))["status"] == "unknown"


def test_calendar_day_snapshot_preserves_supported_reference_time_contract():
    assert evidence(snapshot(holding(), as_of=date(2026, 11, 6)))["status"] == "owned"


def test_input_limits_cannot_silently_truncate_positions_or_transactions():
    large_book = [holding(id=str(i)) for i in range(subject.MAX_FINGERPRINT_HOLDINGS + 1)]
    assert evidence(subject.build_ownership_snapshot(large_book, as_of=AS_OF))["reasons"] == ["ownership_holdings_too_large"]
    records = [buy()] * (subject.MAX_TRANSACTIONS // 2 + 1)
    state = snapshot(holding(id="one", transactions=records), holding(id="two", transactions=records))
    assert evidence(state)["reasons"] == ["ownership_transactions_too_large"]


@pytest.mark.parametrize("item", [None, {}, True, {"name": "World Fund"}, {"isin": True}])
def test_unidentifiable_candidate_is_unknown_even_when_book_is_empty(item):
    result = subject.candidate_ownership_evidence(item, snapshot())
    assert result["status"] == "unknown"
    assert result["reasons"] == ["ownership_candidate_identity_unavailable"]


@pytest.mark.parametrize("state", [None, {}, {"contract": subject.CONTRACT_VERSION}])
def test_missing_snapshot_is_unknown_without_borrowing_any_aggregate(state):
    assert evidence(state)["reasons"] == ["ownership_snapshot_unavailable"]


def test_snapshot_does_not_mutate_source_and_remains_stable_after_source_edits():
    source = holding()
    original = deepcopy(source)
    state = snapshot(source)
    assert source == original
    source.update(provider_id="CHANGED", symbol="CHANGED", isin="IE00BK5BQT80", quantity=0)
    source["transactions"][0]["quantity"] = 0
    assert evidence(state)["status"] == "owned"
    assert evidence(state)["quantity"] == 3
