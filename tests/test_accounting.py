from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "investment"

def load_module(name, filename):
    spec = spec_from_file_location(name, ROOT / filename)
    module = module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module

accounting = load_module("investment_accounting_test", "accounting.py")
search = load_module("investment_search_test", "search.py")
derive_asset_quantities = accounting.derive_asset_quantities
derive_purchase_principal = accounting.derive_purchase_principal


def test_bitcoin_de_bch_withheld_fee():
    result = derive_asset_quantities(gross_quantity=0.04554, net_quantity=0.0450846)
    assert abs(result.fee - 0.0004554) < 1e-12
    assert abs(result.fee_percent - 1.0) < 1e-9
    assert result.net == 0.0450846


def test_asset_fee_can_be_derived_from_percent():
    result = derive_asset_quantities(gross_quantity=10, asset_fee_percent=1)
    assert result.gross == 10
    assert result.net == 9.9
    assert abs(result.fee - 0.1) < 1e-12


def test_legacy_quantity_means_no_withheld_fee():
    result = derive_asset_quantities(quantity=2.5)
    assert result.gross == result.net == 2.5
    assert result.fee == 0


def test_common_crypto_name_aliases_cover_name_search():
    assert search.crypto_name("BCH") == "Bitcoin Cash"
    assert "bitcoin cash" in search.crypto_aliases("BCH")
    assert "bitcoin" in search.crypto_aliases("BTC")


def test_crypto_name_result_can_be_retargeted_to_portfolio_currency():
    assert search.target_crypto_symbol("BCH-USD", "EUR") == "BCH-EUR"
    assert search.target_crypto_symbol("BTC-GBP", "EUR") == "BTC-EUR"


def test_bitcoin_cash_name_search_scores_exactly():
    assert search.crypto_query_score("Bitcoin Cash", "BCH", "BCHEUR", "BCH/EUR", "EUR") == 0
    assert search.crypto_query_score("BCH", "BCH", "BCHEUR", "BCH/EUR", "EUR") == 0
    assert search.crypto_query_score("Bitcoin", "BCH", "BCHEUR", "BCH/EUR", "EUR") == 1


def test_receipt_all_quantity_fields_reconcile():
    result = derive_asset_quantities(
        quantity=0.0450846,
        gross_quantity=0.04554,
        net_quantity=0.0450846,
        asset_fee_quantity=0.0004554,
        asset_fee_percent=1.0,
    )
    assert result.net == 0.0450846
    assert abs(result.fee - 0.0004554) < 1e-12


def test_bitcoin_de_keeps_quoted_price_and_actual_cash_principal_separate():
    unit, principal = derive_purchase_principal(
        gross_quantity=0.04554,
        average_buy_price=2030.00,
        investment_total=91.98,
    )
    assert unit == 2030.00
    assert principal == 91.98


def test_purchase_principal_still_links_when_only_one_side_is_entered():
    unit, principal = derive_purchase_principal(gross_quantity=0.04554, average_buy_price=2030.00)
    assert unit == 2030.00
    assert principal == 92.45

    unit, principal = derive_purchase_principal(gross_quantity=0.04554, investment_total=91.98)
    assert unit == 2019.76
    assert principal == 91.98
