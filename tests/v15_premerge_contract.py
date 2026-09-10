from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_risk_contract_is_unchanged_on_baseline_before_candidate_patch():
    text = (ROOT / "custom_components/investment/validated_model.py").read_text(encoding="utf-8")
    assert "MIN_RISK_HISTORY_WEEKS = 52" in text


def test_current_runtime_has_explicit_score_help_marker():
    text = (ROOT / "custom_components/investment/www/investment-panel-runtime.js").read_text(encoding="utf-8")
    assert "const isScoreHelp=" in text


def test_current_signal_semantics_exist():
    text = (ROOT / "custom_components/investment/indication.py").read_text(encoding="utf-8")
    assert "medium_term_momentum_negative" in text
    assert "price_below_trend_averages" in text
