"""Owned filtering must identify instruments, not merely intersect aliases."""
from importlib.util import module_from_spec, spec_from_file_location
from itertools import permutations
from pathlib import Path

import pytest


COMP = Path(__file__).resolve().parents[1] / "custom_components" / "investment"
SPEC = spec_from_file_location("investment_owned_identity_test", COMP / "portfolio_context.py")
context = module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(context)


def holding(**changes):
    return {
        "provider": "yahoo", "provider_id": "EUNL.DE", "symbol": "EUNL.DE",
        "category": "etf", "currency": "EUR", "quantity": 3,
        **changes,
    }


def match(candidate, *owned):
    return context.find_owned_match(candidate, context.build_owned_identity_index(owned))


def test_same_bare_ticker_on_different_exchanges_is_not_owned():
    owned = holding(provider_id="ABC", symbol="ABC", category="stock", exchange="NYSE")
    candidate = holding(provider="other_feed", provider_id="ABC.L", symbol="ABC",
                        category="stock", exchange="LSE")
    assert match(candidate, owned) is None


@pytest.mark.parametrize("candidate_exchange", [None, "LSE"])
@pytest.mark.parametrize("owned_exchange", [None, "UNKNOWN", "Yahoo"])
def test_bare_ticker_across_providers_requires_identified_venue(candidate_exchange, owned_exchange):
    owned = holding(provider_id="ABC", symbol="ABC", category="stock", exchange=owned_exchange)
    candidate = holding(provider="other_feed", provider_id="ABC", symbol="ABC",
                        category="stock", exchange=candidate_exchange)
    assert match(candidate, owned) is None


def test_exact_bare_ticker_and_explicit_venue_can_match_across_providers():
    owned = holding(provider_id="ABC.DE", symbol="ABC", category="stock", exchange="GER")
    candidate = holding(provider="other_feed", provider_id="ABC", symbol="ABC",
                        category="stock", exchange="xetra")
    assert match(candidate, owned) is owned


def test_supported_qualified_listing_still_matches_without_venue_metadata():
    owned = holding()
    candidate = holding(provider="other_feed", symbol="eunl.de")
    assert match(candidate, owned) is owned


def test_share_class_dot_is_not_treated_as_an_exchange_suffix():
    owned = holding(provider_id="BRK.B", symbol="BRK.B", category="stock")
    candidate = holding(provider="other_feed", provider_id="BRK.B", symbol="BRK.B", category="stock")
    assert match(candidate, owned) is None


def test_crypto_ticker_alone_does_not_identify_a_token_across_providers():
    owned = holding(provider="coingecko", provider_id="first-token", symbol="SAME", category="crypto")
    candidate = holding(provider="other_feed", provider_id="second-token", symbol="SAME", category="crypto")
    assert match(candidate, owned) is None


@pytest.mark.parametrize("field,left,right", [
    ("isin", "IE00B4L5Y983", "IE00BK5BQT80"),
    ("figi", "BBG000BLNNH6", "BBG000B9XRY4"),
    ("share_class_figi", "BBG001S5XBN5", "BBG001S5N8V8"),
])
def test_provider_id_cannot_override_contradictory_strong_identifiers(field, left, right):
    owned = holding(**{field: left})
    candidate = holding(**{field: right})
    assert match(candidate, owned) is None


def test_agreement_on_one_strong_identifier_does_not_erase_another_conflict():
    owned = holding(isin="IE00B4L5Y983", share_class_figi="BBG001S5XBN5")
    candidate = holding(isin="IE00BK5BQT80", share_class_figi="BBG001S5XBN5")
    assert match(candidate, owned) is None


@pytest.mark.parametrize("changes", [
    {"exchange": "LSE"},
    {"currency": "USD"},
    {"quote_currency": "USD"},
    {"symbol": "OTHER.DE"},
    {"category": "stock"},
])
def test_provider_listing_fallback_rejects_explicit_conflicts(changes):
    owned = holding(exchange="XETRA")
    candidate = holding(exchange="XETRA")
    candidate.update(changes)
    assert match(candidate, owned) is None


def test_qualified_symbol_does_not_override_an_explicit_venue_conflict():
    owned = holding(exchange="XETRA")
    candidate = holding(provider="other_feed", exchange="LSE")
    assert match(candidate, owned) is None


def test_penny_quotes_are_not_confused_with_pound_quotes():
    owned = holding(provider_id="VOD.L", symbol="VOD.L", currency="GBp")
    assert match(holding(provider_id="VOD.L", symbol="VOD.L", currency="GBP"), owned) is None
    assert match(holding(provider_id="VOD.L", symbol="VOD.L", currency="GBX"), owned) is owned


def test_strong_identity_can_identify_same_share_class_on_another_listing():
    owned = holding(isin="IE00B4L5Y983", exchange="XETRA")
    candidate = holding(provider="other_feed", provider_id="SWDA.L", symbol="SWDA.L",
                        currency="GBP", exchange="LSE", isin="IE00B4L5Y983")
    assert match(candidate, owned) is owned


@pytest.mark.parametrize("instrument_id,value", [
    ("isin", "IE00B4L5Y983"),
    ("share_class_figi", "BBG001S5XBN5"),
])
def test_shared_instrument_id_takes_precedence_over_distinct_venue_figis(instrument_id, value):
    owned = holding(exchange="XETRA", figi="BBG000BLNNH6", **{instrument_id: value})
    candidate = holding(provider="other_feed", provider_id="SWDA.L", symbol="SWDA.L",
                        currency="GBP", exchange="LSE", figi="BBG000B9XRY4",
                        **{instrument_id: value})
    assert match(candidate, owned) is owned


def test_shared_isin_does_not_override_contradictory_share_class_identifiers():
    owned = holding(isin="IE00B4L5Y983", share_class_figi="BBG001S5XBN5")
    candidate = holding(isin="IE00B4L5Y983", share_class_figi="BBG001S5N8V8")
    assert match(candidate, owned) is None


def test_shared_venue_figi_does_not_override_contradictory_isins():
    owned = holding(isin="IE00B4L5Y983", figi="BBG000BLNNH6")
    candidate = holding(isin="IE00BK5BQT80", figi="BBG000BLNNH6")
    assert match(candidate, owned) is None


def test_instrument_identity_takes_precedence_over_a_venue_only_alias():
    venue_only = holding(figi="BBG000BLNNH6")
    instrument = holding(provider="other_feed", provider_id="IWDA.AS", symbol="IWDA.AS",
                         isin="IE00B4L5Y983", figi="BBG000B9XRY4")
    candidate = holding(isin="IE00B4L5Y983", figi="BBG000BLNNH6")
    for ordered in permutations([venue_only, instrument]):
        assert match(candidate, *ordered) is instrument


def test_same_provider_id_remains_available_without_other_identity_metadata():
    owned = holding(provider_id="ABC", symbol="ABC", category="stock")
    candidate = holding(provider_id="ABC", symbol="ABC", category="stock")
    assert match(candidate, owned) is owned


def test_alias_collision_does_not_discard_a_later_uncontradicted_record():
    conflicting = holding(isin="IE00BK5BQT80")
    compatible = holding()
    candidate = holding(isin="IE00B4L5Y983")
    for ordered in permutations([conflicting, compatible]):
        assert match(candidate, *ordered) is compatible


def test_strong_identity_wins_over_provider_alias_regardless_of_insertion_order():
    provider_only = holding(id="provider-only")
    exact = holding(id="exact", provider="other_feed", provider_id="IWDA.AS",
                    symbol="IWDA.AS", isin="IE00B4L5Y983")
    candidate = holding(isin="IE00B4L5Y983")
    for ordered in permutations([provider_only, exact]):
        assert match(candidate, *ordered) is exact


def test_equally_identified_records_have_deterministic_selection():
    first = holding(id="a", isin="IE00B4L5Y983")
    second = holding(id="z", isin="IE00B4L5Y983")
    candidate = holding(isin="IE00B4L5Y983")
    for ordered in permutations([first, second]):
        assert match(candidate, *ordered) is first


@pytest.mark.parametrize("quantity", [0, -1, None, True, float("nan"), float("inf"), 10**500])
def test_nonpositive_or_invalid_quantity_never_establishes_current_ownership(quantity):
    assert match(holding(), holding(quantity=quantity)) is None


def test_similar_display_names_are_never_identity_evidence():
    owned = holding(name="World ETF Accumulating")
    candidate = holding(provider="other_feed", provider_id="OTHER.DE", symbol="OTHER.DE",
                        name="World ETF Accumulating")
    assert match(candidate, owned) is None
