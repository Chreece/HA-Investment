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
    # Never gate recovery only on the forwarded hass.connected snapshot.
    assert "const connected=value?.connected!==false;" not in PANEL


def test_initial_panel_uses_network_free_bootstrap_before_market_refresh():
    websocket = (COMP / "websocket.py").read_text(encoding="utf-8")
    assert 'vol.Optional("bootstrap", default=False): bool' in websocket
    assert "await manager.async_portfolio_bootstrap(user_id)" in websocket
    assert "async def async_portfolio_bootstrap" in MANAGER
    bootstrap = function_block(
        MANAGER,
        "    async def async_portfolio_bootstrap(",
        "    async def async_set_base_currency(",
    )
    assert "await self.store.async_user(user_id)" in bootstrap
    assert "fifo_summary(records, current_price=None)" in bootstrap
    assert '"bootstrap_local": True' in bootstrap
    assert "await self._quote(" not in bootstrap
    assert "await self._fx_rate(" not in bootstrap
    assert "async_history(" not in bootstrap
    assert "async_rate(" not in bootstrap

    assert "if(connected)this.bootstrapPortfolio();" in PANEL
    assert 'this.call({type:"investment/get_portfolio",bootstrap:true})' in PANEL
    assert "5000," in PANEL
    assert 'this._portfolio?.bootstrap_local===true' in PANEL
    assert "this.loadPortfolio(true,false)" in PANEL
    assert "this.applyPortfolioPayload(portfolio);" in PANEL


def test_bootstrap_unknown_values_are_not_misrepresented_as_zero_history():
    assert "value!==null&&value!==undefined&&Number.isFinite(Number(value))" in PANEL


def test_pre_upgrade_hass_property_is_replayed_through_setter():
    # HA can assign .hass to an unknown element before the custom element class
    # is registered. The own property would then shadow the prototype setter.
    assert 'this.upgradePredefinedProperty("hass");' in PANEL
    assert "upgradePredefinedProperty(name){" in PANEL
    assert "Object.prototype.hasOwnProperty.call(this,name)" in PANEL
    assert "const value=this[name];" in PANEL
    assert "delete this[name];" in PANEL
    assert "this[name]=value;" in PANEL

    constructor_start = PANEL.index("  constructor(){")
    replay = PANEL.index('this.upgradePredefinedProperty("hass");', constructor_start)
    setter = PANEL.index("  set hass(value){", replay)
    assert constructor_start < replay < setter
