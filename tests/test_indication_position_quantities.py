"""Ownership evidence independent of display enrichment and historical FX."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
import json
import math
from pathlib import Path
import random
import sys

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "custom_components/investment/indication_positions.py"
spec = spec_from_file_location("indication_position_quantity_subject", SOURCE)
subject = module_from_spec(spec)
sys.modules[spec.name] = subject
spec.loader.exec_module(subject)
AS_OF = datetime(2026, 10, 6, 19, 0, tzinfo=timezone.utc)
START = datetime(2020, 1, 1, 10, 0, tzinfo=timezone.utc)


def buy(tx_id="buy", quantity=10., day="2020-01-01", *, created=None, shared=None, **extra):
    row = {"id": tx_id, "type": "buy", "date": day,
           "quantity": quantity, "net_quantity": quantity,
           "gross_quantity": quantity, "shared_allocations": [] if shared is None else shared,
           "created_at": int(START.timestamp()) if created is None else created}
    row.update(extra)
    return row


def sell(tx_id="sell", quantity=2., day="2020-01-02", *, created=None, **extra):
    row = {"id": tx_id, "type": "sell", "date": day, "quantity": quantity,
           "created_at": int((START + timedelta(days=1)).timestamp()) if created is None else created}
    row.update(extra)
    return row


def verify(transactions, *, aggregate=0, as_of=AS_OF, **extra):
    return subject.position_quantity_evidence({"quantity": aggregate, "transactions": transactions, **extra}, as_of=as_of)


def test_timed_out_zero_display_aggregate_cannot_hide_authoritative_positive_buy():
    result = verify([buy(quantity=100)], aggregate=0, status="error", error="Enrichment timed out")
    assert result["status"] == "verified"
    assert result["quantity"] == 100
    assert result["stored_aggregate_used"] is False


def test_display_personal_quantity_and_broker_custody_are_never_ownership_evidence():
    result = verify([buy(quantity=10)], aggregate=0, personal_quantity=0, custody_quantity=1000, value=None)
    assert result["quantity"] == 10


def test_shared_buy_sell_units_derive_owner_quantity_without_prices_costs_or_fx():
    transactions = [buy(quantity=10, shared=[{"participant": "Friend", "quantity": 3}]), sell(quantity=2)]
    result = verify(transactions, aggregate=500)
    assert result["status"] == "verified"
    assert result["quantity"] == 5
    assert result["personal_buy_units"] == 7
    assert result["shared_buy_units"] == 3
    assert result["personal_sell_units"] == 2


def test_fully_shared_buy_verifies_owner_zero_without_claiming_missing_ledger_zero():
    result = verify([buy(shared=[{"participant": "Friend", "quantity": 10}])], aggregate=10)
    assert result["status"] == "verified"
    assert result["quantity"] == 0
    assert verify([], aggregate=0)["quantity"] is None


def test_closing_sale_verifies_zero_without_mutating_source_records():
    transactions = [buy(), sell(quantity=10)]
    original = deepcopy(transactions)
    result = verify(transactions, aggregate=10)
    assert result["quantity"] == 0
    assert transactions == original


@pytest.mark.parametrize("raw", [None, [], {}, "", True, "not a ledger"])
def test_missing_or_invalid_ledger_never_uses_stored_positive_or_zero_aggregate(raw):
    for aggregate in (0, 10, None, math.nan):
        result = verify(raw, aggregate=aggregate)
        assert result["status"] == "unknown"
        assert result["quantity"] is None
        assert result["reasons"] == ["position_ledger_missing"]
        json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("raw", [None, [], True, "holding"])
def test_invalid_holding_shape_is_unknown(raw):
    result = subject.position_quantity_evidence(raw, as_of=AS_OF)
    assert result["status"] == "unknown"
    assert result["quantity"] is None


@pytest.mark.parametrize("bad", [None, True, False, math.nan, math.inf, -math.inf, -1, 0, "10", {}, []])
@pytest.mark.parametrize("kind", ["buy", "sell"])
def test_corrupt_units_are_not_clamped_coerced_or_dropped(bad, kind):
    transactions = [buy(quantity=bad)] if kind == "buy" else [buy(), sell(quantity=bad)]
    result = verify(transactions, aggregate=0)
    assert result["status"] == "unknown"
    assert result["quantity"] is None
    assert "quantity_invalid" in result["reasons"][0]
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("net", [None, True, math.nan, -1, 0, 9, 11])
def test_conflicting_or_invalid_explicit_net_units_cannot_borrow_legacy_quantity(net):
    result = verify([buy(net_quantity=net)])
    assert result["status"] == "unknown"
    assert result["quantity"] is None


def test_legacy_quantity_is_accepted_only_when_net_field_absent():
    legacy = buy()
    del legacy["net_quantity"]
    result = verify([legacy])
    assert result["quantity"] == 10


@pytest.mark.parametrize("gross", [None, True, math.nan, -1, 9])
def test_invalid_gross_or_gross_below_net_cannot_certify_position(gross):
    result = verify([buy(gross_quantity=gross)])
    assert result["reasons"] == ["position_ledger_gross_quantity_invalid"]


def test_withheld_units_use_net_quantity_without_subtracting_asset_fee_twice():
    result = verify([buy(quantity=9.9, gross_quantity=10, asset_fee_quantity=.1)])
    assert result["quantity"] == 9.9


@pytest.mark.parametrize("bad", [None, True, False, math.nan, math.inf, -math.inf, -1, 0, "2"])
def test_corrupt_shared_units_cannot_silently_become_personal_units(bad):
    result = verify([buy(shared=[{"participant": "Friend", "quantity": bad}])])
    assert result["status"] == "unknown"
    assert result["reasons"] == ["position_ledger_shared_quantity_invalid"]


@pytest.mark.parametrize("shared", [{}, "unknown", [None], [True], [{"quantity": 1}], [{"participant": True, "quantity": 1}], [{"participant": " ", "quantity": 1}]])
def test_unverifiable_shared_ownership_structure_is_unknown(shared):
    assert verify([buy(shared=shared)])["status"] == "unknown"


def test_shared_units_above_net_cannot_be_clamped_to_owner_zero():
    result = verify([buy(shared=[{"participant": "A", "quantity": 6}, {"participant": "B", "quantity": 5}])])
    assert result["reasons"] == ["position_ledger_shared_units_exceed_net"]
    assert result["quantity"] is None


def test_decimal_shared_arithmetic_preserves_full_share_and_tiny_personal_remainder():
    shared = [{"participant": "A", "quantity": .1}, {"participant": "B", "quantity": .2}]
    assert verify([buy(quantity=.3, shared=shared)])["quantity"] == 0
    result = verify([buy(quantity=.30000000000000004, shared=shared)])
    assert result["quantity"] == 4e-17


def test_shared_allocation_ids_must_be_unique_within_one_buy_but_names_can_repeat():
    shared = [{"id": "same", "participant": "Friend", "quantity": 2}, {"id": "same", "participant": "Friend", "quantity": 2}]
    assert verify([buy(shared=shared)])["reasons"] == ["position_ledger_duplicate_shared_allocation"]
    shared[1]["id"] = "different"
    assert verify([buy(shared=shared)])["quantity"] == 6


def test_shared_ids_are_transaction_local_not_global_person_identity():
    shared = [{"id": "same", "participant": "Friend", "quantity": 2}]
    result = verify([buy("one", shared=shared), buy("two", day="2020-01-02", shared=shared)])
    assert result["quantity"] == 16


def test_sell_cannot_misrepresent_shared_units_as_owner_sales():
    result = verify([buy(), sell(shared_allocations=[{"participant": "Friend", "quantity": 2}])])
    assert result["reasons"] == ["position_ledger_sell_shared_ownership_invalid"]


@pytest.mark.parametrize("tx_id", [None, "", " ", " padded", True, 123, "x" * 257])
def test_transaction_identity_is_required_and_valid(tx_id):
    assert verify([buy(tx_id)])["reasons"] == ["position_ledger_transaction_id_invalid"]


def test_repeated_transaction_identity_does_not_double_count_holdings():
    item = buy()
    result = verify([item, deepcopy(item)])
    assert result["reasons"] == ["position_ledger_duplicate_transaction"]
    assert result["quantity"] is None


@pytest.mark.parametrize("tx_type", [None, "", True, "BUY", "transfer", "split"])
def test_unsupported_transaction_kinds_do_not_default_to_buy(tx_type):
    assert verify([buy(type=tx_type)])["reasons"] == ["position_ledger_transaction_type_invalid"]


@pytest.mark.parametrize("day", [None, "", True, "2020-1-1", " 2020-01-01", "2020-01-01T00:00:00", "2020-02-30", "2026-10-07"])
def test_invalid_or_future_dates_cannot_use_created_at_or_today_as_fallback(day):
    result = verify([buy(day=day)])
    assert result["status"] == "unknown"
    assert result["quantity"] is None


@pytest.mark.parametrize("created", [True, False, math.nan, math.inf, -math.inf, -1, 0, .5, "12345", 1e300])
def test_invalid_creation_times_do_not_invent_order_or_date(created):
    result = verify([buy(created=created)])
    assert result["reasons"] == ["position_ledger_creation_time_invalid"]
    json.dumps(result, allow_nan=False)


def test_future_creation_time_rejected_even_for_backdated_transaction_date():
    result = verify([buy(created=int(AS_OF.timestamp()) + 1)])
    assert result["reasons"] == ["position_ledger_future_creation_time"]


def test_date_only_cutoff_does_not_claim_instant_precision():
    result = verify([buy(created=int(AS_OF.timestamp()) + 1)], as_of=AS_OF.date())
    assert result["status"] == "verified"
    assert result["as_of_resolution"] == "calendar_day"


@pytest.mark.parametrize("cutoff", [None, True, "2026-10-06", datetime(2026, 10, 6), math.nan])
def test_as_of_requires_explicit_date_or_timezone_aware_datetime(cutoff):
    result = verify([buy()], as_of=cutoff)
    assert result["status"] == "unknown"
    assert result["quantity"] is None


def test_user_local_calendar_date_is_preserved_at_utc_midnight_boundary():
    cutoff = datetime(2026, 10, 7, 0, 30, tzinfo=timezone(timedelta(hours=2)))
    transaction = buy(day="2026-10-07", created=int(cutoff.timestamp()) - 60)
    result = verify([transaction], as_of=cutoff)
    assert result["quantity"] == 10
    assert result["as_of_date"] == "2026-10-07"


def test_later_buy_cannot_rescue_historical_oversell():
    result = verify([sell(quantity=2, day="2020-01-01"), buy(day="2020-01-02")], aggregate=8)
    assert result["reasons"] == ["position_ledger_oversell"]


def test_oversell_owner_quantity_includes_exclusion_of_friends_units():
    result = verify([buy(shared=[{"participant": "Friend", "quantity": 9}]), sell(quantity=2)])
    assert result["reasons"] == ["position_ledger_oversell"]


def test_same_day_equal_creation_cannot_invent_buy_before_sell_from_random_ids():
    timestamp = int(START.timestamp())
    transactions = [buy("a", created=timestamp), sell("z", day="2020-01-01", created=timestamp)]
    assert verify(transactions)["reasons"] == ["position_ledger_transaction_order_ambiguous"]
    transactions[0]["id"], transactions[1]["id"] = "z", "a"
    assert verify(transactions)["reasons"] == ["position_ledger_transaction_order_ambiguous"]


def test_known_opening_position_covers_tied_sells_without_guessing_group_order():
    timestamp = int((START + timedelta(days=1)).timestamp())
    transactions = [buy("opening", quantity=20), buy("later", day="2020-01-02", created=timestamp), sell("sold", quantity=5, day="2020-01-02", created=timestamp)]
    assert verify(transactions)["quantity"] == 25


def test_one_missing_creation_time_makes_whole_day_ambiguous_when_order_matters():
    unknown_buy = buy()
    del unknown_buy["created_at"]
    timed_sell = sell(day="2020-01-01")
    assert verify([unknown_buy, timed_sell])["reasons"] == ["position_ledger_transaction_order_ambiguous"]


def test_missing_creation_time_on_different_days_does_not_prevent_valid_units():
    transactions = [buy(), sell()]
    for row in transactions:
        row.pop("created_at")
    assert verify(transactions)["quantity"] == 8


def test_recorded_full_creation_time_prevents_modulo_day_reversing_entry_order():
    # Both trades are backdated to one day. The later entry's UTC time of day
    # happens to be earlier; modulo86400 would incorrectly put its sale first.
    earlier = int(datetime(2022, 1, 1, 23, tzinfo=timezone.utc).timestamp())
    later = int(datetime(2022, 1, 2, 1, tzinfo=timezone.utc).timestamp())
    result = verify([buy(created=earlier), sell(day="2020-01-01", created=later)])
    assert result["quantity"] == 8


def test_decimal_accumulation_preserves_small_position_after_huge_round_trip():
    transactions = [buy("huge", quantity=1e308), buy("small", quantity=1, day="2020-01-02"), sell("close", quantity=1e308, day="2020-01-03")]
    result = verify(transactions)
    assert result["status"] == "verified"
    assert result["quantity"] == 1


def test_smallest_positive_float_units_are_not_dropped_by_epsilon():
    result = verify([buy(quantity=5e-324)])
    assert result["status"] == "verified"
    assert result["quantity"] == 5e-324


def test_unrepresentable_aggregate_is_unknown_and_json_stays_finite():
    result = verify([buy("one", quantity=1e308), buy("two", quantity=1e308)])
    assert result["status"] == "unknown"
    assert result["reasons"] == ["position_ledger_quantity_not_representable"]
    json.dumps(result, allow_nan=False)


def test_bounded_ledger_and_shared_row_work():
    assert verify([buy()] * (subject.MAX_TRANSACTIONS + 1))["reasons"] == ["position_ledger_too_large"]
    shared = [{"participant": str(index), "quantity": .01} for index in range(subject.MAX_SHARED_ALLOCATIONS + 1)]
    assert verify([buy(shared=shared)])["reasons"] == ["position_ledger_shared_ownership_invalid"]


@pytest.mark.parametrize("seed", range(20))
def test_reordered_unambiguous_ledger_matches_independent_owner_unit_algebra(seed):
    rng = random.Random(seed)
    transactions = []
    owned = 0
    for index in range(30):
        day = (date(2020, 1, 1) + timedelta(days=index)).isoformat()
        if owned > 0 and rng.random() < .45:
            quantity = rng.randint(1, owned)
            transactions.append(sell(str(index), quantity=quantity, day=day))
            owned -= quantity
        else:
            quantity = rng.randint(1, 50)
            shared = rng.randint(0, quantity)
            allocations = [{"participant": "Friend", "quantity": shared}] if shared else []
            transactions.append(buy(str(index), quantity=quantity, day=day, shared=allocations))
            owned += quantity - shared
    rng.shuffle(transactions)
    result = verify(transactions, aggregate=-999)
    assert result["status"] == "verified"
    assert result["quantity"] == owned


def fingerprint_holding(name="A", *, units=10):
    return {"id": name, "provider": "provider", "provider_id": name, "symbol": name,
            "category": "stock", "currency": "EUR", "exchange": "Xetra",
            "transactions": [buy(quantity=units)]}


def test_ledger_fingerprint_is_immutable_and_stable_across_membership_and_mapping_order():
    holdings = [fingerprint_holding("A"), fingerprint_holding("B")]
    original = deepcopy(holdings)
    expected = subject.portfolio_ledger_fingerprint(holdings)
    assert isinstance(expected, str) and len(expected) == 64
    reordered = [dict(reversed(list(holding.items()))) for holding in reversed(holdings)]
    assert subject.portfolio_ledger_fingerprint(reordered) == expected
    holdings[0]["transactions"][0]["quantity"] = 100
    assert subject.portfolio_ledger_fingerprint(holdings) != expected
    assert subject.portfolio_ledger_fingerprint(original) == expected


@pytest.mark.parametrize("field", ["price", "portfolio_price", "value", "quantity", "personal_quantity", "status", "error", "market_time", "ledger_rows", "pnl", "quote_currency"])
def test_ledger_fingerprint_ignores_derived_market_and_display_fields(field):
    holding = fingerprint_holding()
    expected = subject.portfolio_ledger_fingerprint([holding])
    holding[field] = math.nan
    assert subject.portfolio_ledger_fingerprint([holding]) == expected


@pytest.mark.parametrize("field", ["id", "provider", "provider_id", "symbol", "name", "category", "currency", "exchange", "isin", "share_class", "share_class_currency", "income_treatment", "leverage_factor", "leveraged", "inverse", "high_yield", "replication", "currency_hedged"])
def test_ledger_fingerprint_detects_identity_and_structure_edits(field):
    holding = fingerprint_holding()
    expected = subject.portfolio_ledger_fingerprint([holding])
    holding[field] = "changed"
    assert subject.portfolio_ledger_fingerprint([holding]) != expected


def test_ledger_fingerprint_detects_buys_sells_ownership_and_holding_membership_changes():
    original = [fingerprint_holding()]
    expected = subject.portfolio_ledger_fingerprint(original)
    changes = [[], [fingerprint_holding("B")], [*original, fingerprint_holding("B")]]
    for mutation in ("add_buy", "add_sell", "shared_units"):
        changed = deepcopy(original)
        if mutation == "add_buy":
            changed[0]["transactions"].append(buy("new-buy"))
        elif mutation == "add_sell":
            changed[0]["transactions"].append(sell())
        else:
            changed[0]["transactions"][0]["shared_allocations"] = [{"participant": "Friend", "quantity": 2}]
        changes.append(changed)
    for changed in changes:
        assert subject.portfolio_ledger_fingerprint(changed) != expected


@pytest.mark.parametrize("bad", [None, True, {}, "holdings", [None], [{}]])
def test_unverifiable_membership_has_no_fingerprint(bad):
    assert subject.portfolio_ledger_fingerprint(bad) is None


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, {"unsupported"}, object()])
def test_noncanonical_or_nonfinite_transaction_data_cannot_compare_equal(bad):
    holding = fingerprint_holding()
    holding["transactions"][0]["quantity"] = bad
    assert subject.portfolio_ledger_fingerprint([holding]) is None


def test_duplicate_stated_holding_ids_have_no_fingerprint():
    first, second = fingerprint_holding("A"), fingerprint_holding("B")
    second["id"] = "A"
    assert subject.portfolio_ledger_fingerprint([first, second]) is None


def test_empty_book_has_a_real_fingerprint_but_missing_book_does_not():
    assert isinstance(subject.portfolio_ledger_fingerprint([]), str)
    assert subject.portfolio_ledger_fingerprint(None) is None


def test_fingerprint_does_not_require_development_fixture_holding_ids():
    holding = fingerprint_holding()
    del holding["id"]
    assert isinstance(subject.portfolio_ledger_fingerprint([holding]), str)
