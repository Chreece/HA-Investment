from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "investment"

def text(path: str) -> str:
    return (COMP / path).read_text(encoding="utf-8")

def indication_body() -> str:
    manager = text("manager.py")
    start = manager.index("    async def async_indication(")
    end = manager.index("\n    async def _history(", start)
    return manager[start:end]

def validated_history_body() -> str:
    manager = text("manager.py")
    start = manager.index("    async def _validated_indication_history(")
    end = manager.index("\n    async def _fallback_paid_history(", start)
    return manager[start:end]

def test_frozen_validated_model_remains_frozen():
    model = text("validated_model.py")
    assert "MIN_RISK_HISTORY_WEEKS = 52" in model
    assert "ACTIVATION_FLOOR = 48.0" in model
    assert "ACTIVATION_FULL = 75.0" in model
    assert 'SIGNAL_SCAFFOLD_RISK = "very_high"' in model

def test_separate_fail_closed_adjusted_contract_exists():
    source = text("providers/base.py")
    assert "async def async_adjusted_history(" in source
    assert "does not provide validated adjusted history" in source
    assert "async def async_history(" in source

def test_yahoo_path_uses_v13_adjusted_semantics():
    source = text("providers/yahoo.py")
    assert 'params["includeAdjustedClose"] = "true"' in source
    assert 'indicators.get("adjclose")' in source
    assert '.get("adjclose")' in source
    assert "require_exact_symbol" in source
    assert "expected_currency" in source
    assert "provider_id, period, adjusted=False" in source

def test_alpha_vantage_uses_explicit_adjusted_endpoints():
    source = text("providers/alpha_vantage.py")
    assert '"TIME_SERIES_DAILY_ADJUSTED"' in source
    assert '"TIME_SERIES_WEEKLY_ADJUSTED"' in source
    assert 'row.get("5. adjusted close")' in source

def test_twelve_data_and_stooq_are_not_promoted():
    assert "async_adjusted_history" not in text("providers/twelve_data.py")
    assert "async_adjusted_history" not in text("providers/stooq.py")

def test_indication_signal_and_risk_use_validated_history():
    body = indication_body()
    assert body.count("self._validated_indication_history(") >= 3
    assert "self._history(asset, history_period)" not in body
    assert "self._history(asset, risk_history_period)" not in body
    assert 'metrics["validated_model_history_source"]' in body
    assert 'metrics["validated_risk_history_source"]' in body
    assert '"validated_history_contract": "corporate_action_aware"' in body

def test_fallback_is_exact_symbol_currency_and_never_stooq():
    body = validated_history_body()
    assert "require_exact_symbol=True" in body
    assert "expected_currency=expected_currency" in body
    assert "self.stooq" not in body
    assert "split_dividend_adjusted_exact_symbol_fallback" in body
    assert "No exchange suffix/listing is guessed" in body

def test_crypto_remains_corporate_action_neutral():
    body = validated_history_body()
    assert 'if category == "crypto":' in body
    assert "points = await self._history(holding, period)" in body
    assert "corporate_action_neutral_crypto" in body

def test_generic_charts_keep_original_history_path():
    manager = text("manager.py")
    scope_start = manager.index("    async def async_scope_history(")
    assert "points = await self._history(holding, period)" in manager[scope_start:]
    generic_start = manager.index("    async def _history(")
    generic_end = manager.index("\n    async def _validated_indication_history(", generic_start)
    generic = manager[generic_start:generic_end]
    assert "provider.async_history(" in generic
    assert "self.stooq.async_history(" in generic

def test_quote_and_current_price_are_unchanged():
    body = indication_body()
    assert "quote = await self._quote(asset)" in body
    assert "current_price=quote.price" in body
    assert '"source": quote.source' in body
