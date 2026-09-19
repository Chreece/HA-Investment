from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / "custom_components" / "investment"
MANAGER = (COMP / "manager.py").read_text(encoding="utf-8")
PANEL = (COMP / "www" / "investment-panel.js").read_text(encoding="utf-8")


def function_block(text: str, start: str, end: str) -> str:
    left = text.index(start)
    right = text.index(end, left)
    return text[left:right]


def test_portfolio_enrichment_has_an_outer_deadline_and_safe_fallback():
    assert "_PORTFOLIO_ENRICH_DEADLINE_SECONDS = 30.0" in MANAGER
    assert "done, pending = await asyncio.wait(" in MANAGER
    assert "timeout=_PORTFOLIO_ENRICH_DEADLINE_SECONDS" in MANAGER
    assert "def timed_out_holding(" in MANAGER
    assert '"status": "error"' in MANAGER
    assert '"ledger_rows": []' in MANAGER


def test_current_fx_uses_single_rate_endpoint_and_collapses_concurrent_cache_misses():
    block = function_block(MANAGER, "    async def _fx_rate(", "    @staticmethod\n    def _canonical_currency")
    assert "async with self._network_sem:" in block
    assert "self.frankfurter.async_rate(" in block
    assert "self.frankfurter.async_quote(" not in block
    assert "cached = self._cache.get(key, 300)" in block


def test_historical_fx_rechecks_cache_after_network_slot_is_acquired():
    block = function_block(MANAGER, "    async def _fx_rate_on_date(", "    async def _resolve_fx_leg(")
    assert "async with self._network_sem:" in block
    assert block.count("self._cache.get(key") >= 2


def test_panel_cannot_remain_on_loading_forever():
    assert "async withTimeout(promise,timeoutMs,message)" in PANEL
    assert "this._portfolio?45000:35000" in PANEL
    assert 'this._loadFailed=!this._portfolio;' in PANEL
    assert 'id="portfolio-retry"' in PANEL
    assert "safeRender()" in PANEL
    assert "renderEmergencyError(error)" in PANEL


def test_first_panel_instance_recovers_from_actual_websocket_events():
    assert "connectionReady(connection=this._hass?.connection)" in PANEL
    assert 'typeof connection.connected==="boolean"' in PANEL
    assert 'addEventListener("ready",this._handleHaReadyEvent)' in PANEL
    assert 'addEventListener("disconnected",this._handleHaDisconnectedEvent)' in PANEL
    assert 'removeEventListener("ready",this._handleHaReadyEvent)' in PANEL
    assert "handleHaReady(render=true)" in PANEL
    assert "handleHaDisconnected(render=true)" in PANEL
    assert "this.loadPortfolio(true);" in PANEL
    assert "scheduleBootstrapRetry()" in PANEL
    assert "portfolioWaitingConnection" in PANEL
    assert "const connected=value?.connected!==false;" not in PANEL
