from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANAGER = (ROOT / "custom_components" / "investment" / "manager.py").read_text(encoding="utf-8")


def _indication_body() -> str:
    start = MANAGER.index("    async def async_indication(")
    end = MANAGER.index("\n    async def _history(", start)
    return MANAGER[start:end]


def test_runtime_uses_fixed_risk_signal_scaffold_and_separate_five_year_risk_feed():
    body = _indication_body()
    assert "risk_tolerance=SIGNAL_SCAFFOLD_RISK" in body
    assert 'risk_history_period = "5y"' in body
    assert 'risk_fx_history_period = "5y_risk"' in body
    assert "self._convert_history(" in body
    assert "weekly_return_map_from_points(" in body
    assert 'metrics["risk_history_currency"] = base' in body


def test_runtime_uses_validated_construction_not_legacy_category_allocator():
    body = _indication_body()
    assert "validated_exact_weights(results, risk_tolerance)" in body
    assert "apply_downward_weight_constraints(" in body
    assert "risk=risk_tolerance" in body
    assert "production_projection(" in body
    assert "allocate_budget(" not in body


def test_full_ai_can_only_reduce_validated_deterministic_allocation():
    body = _indication_body()
    assert "clamp_ai_ranking_to_deterministic(" in body
    assert "sanitize_ai_ranking(" not in body


def test_nontradable_and_unknown_exposures_are_removed_before_market_history_fetch():
    body = _indication_body()
    classification = body.index("classification = classify_economic_exposure(asset)")
    overlap = body.index("overlap_rows = await asyncio.gather")
    history = body.index("async def evaluate(")
    assert classification < overlap < history


def test_risk_fx_feed_is_weekly_and_historical_fx_is_single_flight_cached():
    provider = (ROOT / "custom_components" / "investment" / "providers" / "frankfurter.py").read_text(encoding="utf-8")
    assert '"5y_risk": 5 * 370' in provider
    assert 'period in {"1y", "5y_risk"}' in provider
    assert 'self._fx_history_locks' in MANAGER
    assert '("fx_history", norm_currency, norm_base, period)' in MANAGER


def test_user_facing_label_and_ai_prompt_cannot_override_validated_suitability():
    body = _indication_body()
    assert 'item["market_label"] = item.get("label")' in body
    assert 'item["label"] = "caution"' in body
    assert 'risk_after_projection' in body
    assert 'deterministic_suggested_amount' in MANAGER
    assert 'never exceed it' in MANAGER


def test_portfolio_context_concentration_uses_economic_sleeves_not_wrapper_categories():
    body = _indication_body()
    assert "economic_sleeve_values" in body
    assert 'asset.get("economic_sleeve")' in body
    assert 'portfolio_economic_sleeve_weight' in body


def test_indication_form_changes_invalidate_stale_results_before_new_analysis():
    panel = (ROOT / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(
        encoding="utf-8"
    )
    assert "invalidateIndicationResult()" in panel
    assert 'addEventListener("change",e=>{this.invalidateIndicationResult();' in panel
    assert 'addEventListener("input",()=>{this.invalidateIndicationResult();' in panel


def test_discovery_hides_zero_allocation_rows_but_explicit_sources_keep_them_visible():
    panel = (ROOT / "custom_components" / "investment" / "www" / "investment-panel.js").read_text(
        encoding="utf-8"
    )
    assert "indicationVisibleResults(result)" in panel
    assert 'if(result?.scope==="search"||result?.scope==="portfolio")return rows;' in panel
    assert "return Number(suggestion.amount||0)>0;" in panel
    assert "const visibleResults=this.indicationVisibleResults(result);" in panel


def test_ai_text_only_review_falls_back_to_conservative_caution_without_touching_allocation():
    assert 'if mode == "deterministic_ai" and not isinstance(parsed, dict):' in MANAGER
    assert '"verdict": "caution"' in MANAGER
    body = _indication_body()
    assert "ai_review = await self._ai_indication_review(" in body
    assert "clamp_ai_ranking_to_deterministic(" in body
