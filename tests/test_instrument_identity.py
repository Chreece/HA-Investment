"""Exact-listing and fail-closed structure contracts, independent of HA."""
from copy import deepcopy
from datetime import date, timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

import pytest

SOURCE = Path(__file__).resolve().parents[1] / "custom_components/investment/instrument_identity.py"
spec = spec_from_file_location("investment_identity_subject", SOURCE)
identity = module_from_spec(spec)
sys.modules[spec.name] = identity
spec.loader.exec_module(identity)

TODAY = date(2026, 10, 6)


def fund(listing_id="EUNL.DE", **changes):
    row = {"provider": "yahoo", "provider_id": listing_id, "symbol": listing_id,
           "category": "etf", "currency": "EUR", "exchange": "Xetra",
           "name": "Custom display name"}
    row.update(changes)
    return row


def provider(listing_id="EUNL.DE", **changes):
    row = {"provider": "yahoo", "provider_id": listing_id, "symbol": listing_id,
           "category": "etf", "currency": "EUR", "exchange": "GER"}
    row.update(changes)
    return row


def resolve(row=None, metadata=None, *, as_of=TODAY):
    return identity.resolve_instrument_identity(
        fund() if row is None else row,
        provider_metadata=provider() if metadata is None else metadata,
        as_of=as_of,
    )


def assert_blocked(result, reason):
    assert result["instrument_identity_eligible"] is False
    assert result["allocatable"] is False
    assert result["economic_sleeve"] in {"unknown", "nontradable"}
    assert reason in result["instrument_identity_blockers"]


@pytest.mark.parametrize("symbol,isin,share_currency,sleeve,inception", [
    ("XEON.DE", "LU0290358497", "EUR", "cash_like", "2007-05-25"),
    ("CEMK.DE", "IE000WV38GP5", "EUR", "cash_like", "2025-08-22"),
    ("VAGF.DE", "IE00BG47KH54", "EUR", "aggregate_bond", "2019-06-18"),
    ("VGGF.DE", "IE000B1A2798", "EUR", "government_bond", "2025-03-25"),
    ("SXR8.DE", "IE00B5BMR087", "USD", "broad_equity", "2010-05-19"),
    ("EUNL.DE", "IE00B4L5Y983", "USD", "broad_equity", "2009-09-25"),
    ("VWCE.DE", "IE00BK5BQT80", "USD", "broad_equity", "2019-07-23"),
])
def test_exact_issuer_catalog_listings(symbol, isin, share_currency, sleeve, inception):
    result = resolve(fund(symbol, isin=isin), provider(symbol))
    assert result["instrument_identity_eligible"] is True
    assert result["allocatable"] is True
    assert result["instrument_identity_status"] == "issuer_catalog_and_provider_listing"
    assert result["economic_sleeve"] == sleeve
    assert result["isin"] == isin
    assert result["share_class_currency"] == share_currency
    assert result["listing_currency"] == "EUR"
    assert result["share_class_inception"] == inception
    assert result["issuer_source_url"].startswith("https://")
    assert result["issuer_reviewed_on"] == TODAY.isoformat()
    assert result["economic_classification_confidence"] is None
    assert result["instrument_identity_blockers"] == []


@pytest.mark.parametrize("metadata", [None, {}, [], "verified", True])
def test_verified_flags_and_nested_caller_metadata_cannot_create_evidence(metadata):
    row = fund(verified=True, issuer_verified=True, economic_sleeve="cash_like",
               instrument_identity_eligible=True, economic_classification_confidence=1.0,
               instrument_metadata=provider(), instrument_identity=resolve())
    result = identity.resolve_instrument_identity(row, provider_metadata=metadata, as_of=TODAY)
    assert_blocked(result, "provider_metadata_missing")


@pytest.mark.parametrize("name", [
    "iShares Core MSCI World UCITS ETF", "Money Market ETF", "EUR Overnight ETF",
    "Ultra Short Investment Grade Bond ETF", "Treasury Bill Fund",
    "0-1yr Government Bond ETF", "US Aggregate Bond ETF", "QQQ",
])
def test_unknown_fund_name_never_grants_a_defensive_or_equity_sleeve(name):
    row = fund("UNKNOWN.DE", name=name, isin="IE00B4L5Y983", verified=True)
    observed = provider("UNKNOWN.DE", isin="IE00B4L5Y983", verified=True)
    result = resolve(row, observed)
    assert_blocked(result, "issuer_fund_identity_unverified")
    assert result["isin"] is None
    assert result["issuer_source_url"] is None


@pytest.mark.parametrize("name,flag", [
    ("Daily 2x Government Bond ETF", "leveraged_or_inverse_structure"),
    ("Treasury 1.5x UCITS ETF", "leveraged_or_inverse_structure"),
    ("MSCI World -1x ETF", "leveraged_or_inverse_structure"),
    ("MSCI World 3× ETF", "leveraged_or_inverse_structure"),
    ("Inverse Overnight Bond Fund", "leveraged_or_inverse_structure"),
    ("ProShares UltraShort Treasury ETF", "leveraged_or_inverse_structure"),
    ("Daily Short Government Bond ETF", "short_strategy_structure_unverified"),
    ("Short-Term High-Yield Bond ETF", "high_yield_credit_structure"),
    ("Ultrashort High Yield ETF", "high_yield_credit_structure"),
    ("Junk Bond ETF", "high_yield_credit_structure"),
    ("MSCI World Covered Call ETF", "other_complex_fund_structure"),
])
def test_structural_warning_names_can_only_reject(name, flag):
    result = resolve(fund("UNKNOWN.DE", name=name), provider("UNKNOWN.DE"))
    assert_blocked(result, f"unsupported_{flag}")
    assert flag in result["product_structure_flags"]
    assert result["economic_classification_basis"] == "unsupported_product_structure"


@pytest.mark.parametrize("field,value", [
    ("leveraged", True), ("inverse", True), ("leverage_factor", 2),
    ("leverage_factor", -1), ("leverage_factor", float("nan")),
    ("leverage_factor", float("inf")), ("leverage_factor", True),
    ("leverage_factor", "invalid"),
])
def test_structure_assertions_cannot_be_hidden_by_a_known_ticker(field, value):
    assert_blocked(resolve(fund(**{field: value})), "unsupported_leveraged_or_inverse_structure")


def test_short_maturity_words_do_not_prove_a_short_position_or_safe_structure():
    result = resolve(fund("UNKNOWN.DE", name="Ultra Short Duration Bond ETF"), provider("UNKNOWN.DE"))
    assert_blocked(result, "issuer_fund_identity_unverified")
    assert "short_strategy_structure_unverified" not in result["product_structure_flags"]
    result = resolve(fund("CEMK.DE", name="iShares Euro Govt Bond 0-1yr UCITS ETF"), provider("CEMK.DE"))
    assert result["allocatable"] is True


@pytest.mark.parametrize("changes,reason", [
    ({"provider": "twelve_data"}, "provider_source_mismatch"),
    ({"symbol": "SWDA.L"}, "provider_listing_mismatch"),
    ({"symbol": "EUNL.F"}, "provider_listing_mismatch"),
    ({"provider_id": "SXR8.DE"}, "provider_listing_mismatch"),
    ({"category": "stock"}, "provider_product_type_mismatch"),
    ({"currency": "USD"}, "quote_currency_mismatch"),
    ({"exchange": "Frankfurt"}, "exchange_mismatch"),
    ({"exchange": "LSE"}, "exchange_mismatch"),
    ({"isin": "IE00B5BMR087"}, "isin_mismatch"),
])
def test_provider_cannot_return_another_listing_even_with_valid_isin(changes, reason):
    row = fund(isin="IE00B4L5Y983")
    observed = provider(isin="IE00B4L5Y983")
    observed.update(changes)
    result = resolve(row, observed)
    assert_blocked(result, reason)
    assert result["instrument_identity_status"] == "conflicting"


@pytest.mark.parametrize("field,reason", [
    ("provider", "provider_source_missing"), ("symbol", "provider_symbol_missing"),
    ("category", "provider_product_type_missing"), ("currency", "provider_currency_missing"),
    ("exchange", "provider_exchange_missing"),
])
def test_returned_metadata_must_actually_contain_identity_fields(field, reason):
    observed = provider()
    observed.pop(field)
    assert_blocked(resolve(metadata=observed), reason)


@pytest.mark.parametrize("exchange", ["XETRA", "XETR", "GER", "Deutsche Börse AG", "Deutsche Boerse"])
def test_documented_exchange_aliases_are_explicit(exchange):
    assert resolve(metadata=provider(exchange=exchange))["allocatable"] is True


def test_missing_requested_exchange_can_be_resolved_but_provider_placeholder_cannot():
    assert resolve(fund(exchange="Yahoo Finance"))["allocatable"] is True
    assert resolve(fund(exchange=None))["allocatable"] is True
    assert_blocked(resolve(metadata=provider(exchange="Yahoo Finance")), "provider_exchange_missing")
    # Matching placeholders on both sides are still not a confirmed exchange.
    assert_blocked(resolve(fund(exchange="Yahoo Finance"), provider(exchange="Yahoo Finance")), "provider_exchange_missing")


def test_exact_catalog_ticker_alias_does_not_become_generic_suffix_stripping():
    assert resolve(fund(symbol="EUNL"))["allocatable"] is True
    assert_blocked(resolve(fund(symbol="SXR8.DE")), "requested_symbol_mismatch")
    assert_blocked(resolve(fund("EUNL.L", symbol="EUNL"), provider("EUNL.L")), "requested_symbol_mismatch")


@pytest.mark.parametrize("changes,reason", [
    ({"isin": "IE00B5BMR087"}, "isin_mismatch"),
    ({"isin": "IE00B4L5Y984"}, "invalid_isin"),
    ({"isin": "not-an-isin"}, "invalid_isin"),
    ({"isin": True}, "invalid_isin"),
    ({"share_class": "USD Distributing"}, "share_class_mismatch"),
    ({"share_class": "EUR Hedged Accumulating"}, "share_class_mismatch"),
    ({"share_class_currency": "EUR"}, "share_class_currency_mismatch"),
    ({"currency_hedged": True}, "currency_hedging_mismatch"),
    ({"name": "iShares Core MSCI World EUR Hedged Acc"}, "currency_hedging_mismatch"),
    ({"name": "iShares Core MSCI World USD Dist"}, "income_treatment_mismatch"),
    ({"name": "iShares Core MSCI World Swap UCITS ETF"}, "replication_structure_mismatch"),
    ({"income_treatment": "distributing"}, "income_treatment_mismatch"),
    ({"replication": "synthetic_swap"}, "replication_structure_mismatch"),
    ({"replication": False}, "replication_structure_mismatch"),
])
def test_contradictory_share_class_assertions_block_from_either_source(changes, reason):
    assert_blocked(resolve(fund(**changes)), reason)
    assert_blocked(resolve(metadata=provider(**changes)), reason)


def test_hedged_funds_cannot_be_relabelled_unhedged_or_into_another_currency():
    assert_blocked(resolve(fund("VAGF.DE", name="Vanguard Global Aggregate Bond Unhedged"), provider("VAGF.DE")), "currency_hedging_mismatch")
    assert_blocked(resolve(fund("VGGF.DE", name="Global Government Bond USD Hedged"), provider("VGGF.DE")), "currency_hedging_mismatch")
    result = resolve(fund("VGGF.DE", name="Global Government Bond EUR Hedged Accumulating"), provider("VGGF.DE"))
    assert result["allocatable"] is True
    assert result["product_structure_flags"] == ["currency_hedged_structure"]


def test_xeon_swap_is_recorded_but_not_misrepresented_as_physical_cash():
    result = resolve(fund("XEON.DE", name="Xtrackers EUR Overnight Rate Swap UCITS ETF 1C"), provider("XEON.DE"))
    assert result["allocatable"] is True
    assert result["share_class"] == "1C"
    assert result["product_structure_flags"] == ["swap_based_structure"]
    assert_blocked(resolve(fund("XEON.DE", name="Physical Overnight ETF"), provider("XEON.DE")), "replication_structure_mismatch")
    assert_blocked(resolve(fund("XEON.DE", replication="physical"), provider("XEON.DE")), "replication_structure_mismatch")
    assert resolve(fund("XEON.DE", replication="synthetic_swap"), provider("XEON.DE"))["allocatable"] is True
    assert resolve(fund(replication="physical"))["allocatable"] is True


def test_review_expiry_requires_fresh_issuer_verification_not_a_verified_flag():
    due = TODAY + timedelta(days=identity.ISSUER_REVIEW_INTERVAL_DAYS)
    assert resolve(as_of=due)["allocatable"] is True
    result = resolve(fund(issuer_reviewed_on=(due + timedelta(days=1)).isoformat(), verified=True), as_of=due + timedelta(days=1))
    assert_blocked(result, "issuer_catalog_review_expired")
    assert result["instrument_identity_status"] == "expired_catalog"
    assert result["issuer_reviewed_on"] == TODAY.isoformat()


@pytest.mark.parametrize("as_of,reason", [
    (None, "catalog_review_date_missing"), ("invalid", "catalog_review_date_invalid"),
    (True, "catalog_review_date_invalid"),
    (TODAY - timedelta(days=1), "catalog_not_reviewed_as_of_date"),
])
def test_replay_date_cannot_silently_use_future_or_unreviewed_facts(as_of, reason):
    assert_blocked(resolve(as_of=as_of), reason)


def test_share_class_inception_does_not_borrow_a_parent_funds_older_launch():
    result = resolve(fund("CEMK.DE", inception="2009-03-06"), provider("CEMK.DE"))
    assert result["share_class_inception"] == "2025-08-22"
    assert result["listing_inception"] == "2025-08-27"
    result = resolve(fund("VGGF.DE", inception="2000-01-01"), provider("VGGF.DE"))
    assert result["share_class_inception"] == "2025-03-25"
    assert result["listing_inception"] == "2025-03-27"
    result = resolve(fund("VAGF.DE"), provider("VAGF.DE"))
    assert result["share_class_inception"] == "2019-06-18"
    assert result["listing_inception"] == "2019-06-20"


def stock(**changes):
    row = fund("MSFT", category="stock", currency="USD", exchange="NASDAQ", name="Microsoft")
    row.update(changes)
    return row


def stock_provider(**changes):
    row = provider("MSFT", category="stock", currency="USD", exchange="NMS")
    row.update(changes)
    return row


def test_direct_stock_requires_provider_evidence_but_no_fund_catalog():
    result = resolve(stock(), stock_provider(), as_of=None)
    assert result["allocatable"] is True
    assert result["economic_sleeve"] == "single_equity"
    assert result["instrument_identity_status"] == "provider_listing"
    assert result["economic_classification_confidence"] is None
    assert result["issuer_source_url"] is None
    assert_blocked(resolve(stock(exchange="NYSE"), stock_provider()), "exchange_mismatch")


def test_direct_stock_cannot_use_uncorroborated_share_class_identifier():
    assert_blocked(resolve(stock(isin="US5949181045"), stock_provider()), "provider_isin_missing")
    result = resolve(stock(isin="US5949181045"), stock_provider(isin="US5949181045"))
    assert result["allocatable"] is True


@pytest.mark.parametrize("requested,observed,eligible", [
    ("GBp", "GBP", False), ("GBP", "GBp", False), ("GBX", "GBP", False),
    ("GBp", "GBX", True), ("GBX", "GBp", True), ("GBP", "GBP", True),
])
def test_native_quote_units_are_preserved_in_listing_identity(requested, observed, eligible):
    row = stock(provider_id="TEST.L", symbol="TEST.L", currency=requested, exchange="LSE")
    metadata = stock_provider(provider_id="TEST.L", symbol="TEST.L", currency=observed, exchange="LONDON")
    result = resolve(row, metadata)
    assert result["allocatable"] is eligible
    if not eligible:
        assert_blocked(result, "quote_currency_mismatch")


def test_crypto_display_separator_alias_does_not_change_quote_pair():
    row = fund("BTC-EUR", category="crypto", symbol="BTC/EUR", exchange="CCC")
    metadata = provider("BTC-EUR", category="crypto", exchange="CCC")
    result = resolve(row, metadata)
    assert result["economic_sleeve"] == "crypto"
    assert result["allocatable"] is True
    assert_blocked(resolve(row, {**metadata, "symbol": "BTC-USD"}), "provider_listing_mismatch")
    assert_blocked(resolve(row, {**metadata, "currency": "USD"}), "crypto_quote_pair_mismatch")


def test_kraken_pair_id_and_display_symbol_must_both_match_provider_response():
    row = fund("XXBTZEUR", provider="kraken", category="crypto", symbol="BTC/EUR", exchange="Kraken")
    metadata = provider("XXBTZEUR", provider="kraken", category="crypto", symbol="XBT/EUR", exchange="Kraken")
    assert resolve(row, metadata)["allocatable"] is True
    assert_blocked(resolve(row, {**metadata, "provider_id": "BCHZEUR"}), "provider_listing_mismatch")
    assert_blocked(resolve(row, {**metadata, "symbol": "BCH/EUR"}), "provider_symbol_mismatch")
    del metadata["provider_id"]
    assert_blocked(resolve(row, metadata), "provider_listing_id_missing")


@pytest.mark.parametrize("category,symbol", [("index", "^GSPC"), ("commodity", "GC=F"), ("fx", "EURUSD=X"), ("future", "FUT")])
def test_reference_series_never_become_allocatable(category, symbol):
    assert_blocked(resolve(fund(symbol, category=category), provider(symbol, category=category)), "nontradable_reference")


def test_generic_commodity_type_does_not_prove_spot_rather_than_derivative():
    assert_blocked(resolve(fund("GOLD", category="commodity"), provider("GOLD", category="commodity")), "product_structure_unverified")


def test_resolver_does_not_mutate_inputs_or_allow_output_to_rewrite_catalog():
    row, metadata = fund(), provider()
    original = deepcopy((row, metadata))
    first = resolve(row, metadata)
    first["isin"] = "US5949181045"
    first["product_structure_flags"].append("made_up")
    second = resolve(row, metadata)
    assert (row, metadata) == original
    assert second["isin"] == "IE00B4L5Y983"
    assert second["product_structure_flags"] == []


@pytest.mark.parametrize("row", [None, [], True, "EUNL.DE", 1])
def test_malformed_candidate_abstains(row):
    result = identity.resolve_instrument_identity(row, provider_metadata=provider(), as_of=TODAY)
    assert_blocked(result, "invalid_candidate_identity")
