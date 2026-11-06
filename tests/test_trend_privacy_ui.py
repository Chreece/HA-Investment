"""Exercise real history rendering, pointer updates and reveal lifetimes in a DOM.

Requires the same test-only LinkeDOM dependency as the other frontend tests.
All financial fixtures below are synthetic; no user portfolio data is used.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


PANEL = Path(__file__).resolve().parents[1] / "custom_components/investment/www/investment-panel.js"
MASK = "••••"

NODE_TEST = r"""
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const dependency = process.env.INVESTMENT_UI_NODE_MODULES;
const { parseHTML } = require(dependency ? path.join(dependency, 'linkedom') : 'linkedom');
const { window } = parseHTML('<!doctype html><html><head></head><body></body></html>');
window.innerWidth=1440;window.innerHeight=1000;
window.matchMedia=()=>({matches:true});
const timers=new Map();let timerId=0;
const context=vm.createContext({
  window,document:window.document,HTMLElement:window.HTMLElement,Element:window.Element,
  customElements:window.customElements,navigator:{language:'en'},console,URL,Intl,
  CSS:{escape:value=>String(value)},
  Date:class extends Date {static now(){return Date.parse('2026-11-06T15:00:00Z');}},
  setTimeout:(callback,delay)=>{const id=++timerId;timers.set(id,{callback,delay});return id;},
  clearTimeout:id=>timers.delete(id),
});
const source=fs.readFileSync(process.argv[1],'utf8');
// The privacy fix is shared with master, whose browser element registration is
// unversioned. Run the identical class under one isolated test registration.
const versioned=source.indexOf('const PANEL_MODULE_REVISION =');
const bootstrap=versioned>=0?versioned:source.indexOf('if(!customElements.get("investment-panel"))');
if(bootstrap<0)throw new Error('Unrecognized panel registration');
vm.runInContext(source.slice(0,bootstrap)+`
  customElements.define('investment-panel-trend-privacy-test',InvestmentPanel);
  globalThis.Panel=InvestmentPanel;
`,context,{filename:process.argv[1]});
const timestamp=date=>Date.parse(date+'T12:00:00Z')/1000;
const points=[
  ['2026-10-10',2098.75],['2026-10-15',2233.12],['2026-10-20',2177.95],
  ['2026-10-25',2301.34],['2026-10-30',2198.09],['2026-11-06',2267.41],
].map(([date,value])=>({ts:timestamp(date),value}));
const holding={
  id:'example',name:'Synthetic fixture',symbol:'TEST',category:'stock',currency:'EUR',
  quantity:4.25,value:2267.41,ledger_rows:[
    {type:'buy',sort_ts:timestamp('2026-10-01'),quantity:3,cash_principal:1530,
      all_in_cost:1532.43,explicit_costs:2.43,asset_fee_value:1.17,embedded_asset_fee_cost:1.17},
    {type:'buy',sort_ts:timestamp('2026-10-20'),quantity:1.75,cash_principal:822.50,
      all_in_cost:825.62,explicit_costs:3.12,asset_fee_value:0.48,embedded_asset_fee_cost:0.48},
    {type:'sell',sort_ts:timestamp('2026-10-29'),quantity:0.5,allocated_cost_basis:255.4,
      realized_pnl:61.42,explicit_costs:1.04},
  ],
};
const rectangle=()=>({left:20,top:40,right:540,bottom:268,width:520,height:228});
const event=(type,properties={})=>{
  const result=new window.Event(type,{bubbles:true,cancelable:true});
  Object.assign(result,{clientX:280,clientY:140,pointerId:1,pointerType:'mouse',button:0},properties);
  return result;
};
const snapshot=panel=>{
  const pop=panel.shadowRoot.querySelector('[data-trend-pop]');
  if(!pop)return {closed:true};
  const fields=[...pop.querySelectorAll('.history-primary strong,.history-change-pill,.trend-y-label,.history-stats b,[data-trend-tooltip-value],[data-trend-tooltip-change]')];
  return {
    fields:fields.map(node=>({key:node.dataset.trendPrivateValue||null,text:node.textContent,
      revealed:node.hasAttribute('data-incognito-revealed'),role:node.getAttribute('role'),
      pressed:node.getAttribute('aria-pressed'),tabindex:node.getAttribute('tabindex')})),
    text:pop.textContent,
    accessibility:[...pop.querySelectorAll('[aria-label],[aria-valuetext],[title]')].flatMap(node=>
      ['aria-label','aria-valuetext','title'].map(name=>node.getAttribute(name)||'')),
    tooltipVisible:pop.querySelector('[data-trend-tooltip]')?.hidden===false,
    slotPrivate:panel.shadowRoot.getElementById('trend-slot').classList.contains('incognito'),
    revealDelay:timers.get(panel._incognitoRevealTimer)?.delay??null,
    pinned:panel._trendPinned,
    metric:panel._trend.metric,
    zoom:[panel._trend.zoomStart,panel._trend.zoomEnd],
  };
};
(async()=>{
  const cases=JSON.parse(fs.readFileSync(0,'utf8')),results=[];
  for(const testCase of cases){
    const panel=new context.Panel();
    panel._lang=testCase.language||'en';panel._haLang=panel._lang;panel._preferredLang=panel._lang;
    panel.applyLanguage(panel._lang);
    panel._portfolio={base_currency:'EUR',language:panel._lang,total:2267.41,categories:[],holdings:[holding]};
    panel._loading=false;panel._privacyControlsEnabled=true;
    panel._incognito=testCase.private!==false;panel._storedIncognito=panel._incognito;
    panel._connectionLocal=true;panel._incognitoRevealSeconds=testCase.seconds??5;
    panel._trendPinned=testCase.pinned!==false;
    const scope=testCase.scope||'portfolio';
    panel._trend={scope,id:scope==='holding'?'example':scope==='category'?'stock':null,
      period:'1m',metric:testCase.metric||'value',points,currency:'EUR',left:20,top:40,
      loading:!!testCase.loading,zoomStart:0,zoomEnd:null,hoverIndex:null};
    panel.call=async message=>message.type==='investment/history'?{points,currency:'EUR'}:{};
    panel.shadowRoot.innerHTML='<div id="trend-slot"></div><button id="elsewhere">Elsewhere</button>';
    panel.updateTrendUi();panel.bind();
    const snapshots=[snapshot(panel)];
    for(const action of testCase.actions||[]){
      const chart=panel.shadowRoot.querySelector('[data-trend-chart]');
      if(chart)chart.getBoundingClientRect=rectangle;
      if(action.type==='hover'){
        chart.dispatchEvent(event('pointermove',{clientX:action.x??280}));
      }else if(action.type==='pointer'||action.type==='click'){
        const node=panel.shadowRoot.querySelector(action.selector);
        if(!node)throw new Error('Missing interaction target '+action.selector);
        node.dispatchEvent(event('pointerdown'));
        if(action.type==='click')node.dispatchEvent(event('click'));
      }else if(action.type==='key'){
        panel.shadowRoot.querySelector(action.selector).dispatchEvent(event('keydown',{key:action.key}));
      }else if(action.type==='expire'){
        const id=panel._incognitoRevealTimer,timer=timers.get(id);
        if(!timer)throw new Error('Missing reveal timeout');
        timers.delete(id);timer.callback();
      }else if(action.type==='update')panel.updateTrendUi();
      else if(action.type==='render')panel.render();
      else if(action.type==='zoom')panel.setTrendZoom(action.direction??1);
      else if(action.type==='toggle')await panel.toggleIncognito();
      else if(action.type==='payload')panel.applyPortfolioPayload({...panel._portfolio,
        privacy_controls_enabled:true,connection_local:true,incognito:action.private});
      else if(action.type==='close')panel.closeTrend();
      else if(action.type==='load'){
        const anchor=window.document.createElement('div');anchor.getBoundingClientRect=rectangle;
        await panel.loadTrend(scope,panel._trend?.id||null,anchor,action.period||'7d',null,testCase.metric||'value');
      }
      // Flush the immediate synthetic history response when a real period
      // button triggered loadTrend without awaiting it in its event listener.
      await Promise.resolve();
      snapshots.push(snapshot(panel));
    }
    panel.clearIncognitoReveal();
    results.push(snapshots);
  }
  process.stdout.write(JSON.stringify(results));
})().catch(error=>{console.error(error);process.exit(1);});
"""


def run_ui(*cases):
    node = shutil.which("node")
    assert node, "Node is required for the shipped history UI regression tests"
    completed = subprocess.run(
        [node, "-e", NODE_TEST, str(PANEL)],
        input=json.dumps(cases), text=True, capture_output=True, timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def assert_masked(snapshot, *, complete=True):
    assert snapshot["slotPrivate"]
    if complete:
        assert len(snapshot["fields"]) == 13
    assert all(field["text"] == MASK for field in snapshot["fields"])
    assert all(field["pressed"] == "false" for field in snapshot["fields"])
    assert all(field["role"] == "button" for field in snapshot["fields"])
    assert all(field["tabindex"] == "0" for field in snapshot["fields"])
    assert "€" not in snapshot["text"]
    assert not any("€" in value for value in snapshot["accessibility"])


def field(snapshot, key):
    return next(item for item in snapshot["fields"] if item["key"] == key)


@pytest.mark.parametrize("scope", ["portfolio", "category", "holding"])
@pytest.mark.parametrize("metric", ["value", "costBasis", "invested", "costs", "assetFees", "pnl"])
def test_every_scope_and_metric_masks_summary_axes_stats_and_pointer_values(scope, metric):
    snapshots, = run_ui({"scope": scope, "metric": metric, "actions": [{"type": "hover"}]})
    for snapshot in snapshots:
        assert_masked(snapshot)
    assert snapshots[-1]["tooltipVisible"]


@pytest.mark.parametrize("metric", ["price", "quantity", "realized", "unrealized"])
def test_holding_only_metrics_never_disclose_prices_units_or_returns_on_hover(metric):
    snapshots, = run_ui({"scope": "holding", "metric": metric, "actions": [{"type": "hover"}]})
    for snapshot in snapshots:
        assert_masked(snapshot)


@pytest.mark.parametrize("language", ["en", "de", "el"])
def test_private_history_redacts_accessibility_labels_in_localized_views(language):
    snapshots, = run_ui({"language": language, "actions": [{"type": "hover"}]})
    for snapshot in snapshots:
        assert_masked(snapshot)


def test_deliberate_reveal_exposes_one_value_and_expiry_removes_its_text_and_accessible_name():
    snapshots, = run_ui({"actions": [
        {"type": "pointer", "selector": ".history-primary > div:first-child strong"},
        {"type": "expire"}, {"type": "hover"},
    ]})
    assert_masked(snapshots[0])
    revealed = snapshots[1]
    assert field(revealed, "current")["text"] == "€2,267.41"
    assert field(revealed, "current")["pressed"] == "true"
    assert revealed["revealDelay"] == 5000
    assert all(item["text"] == MASK for item in revealed["fields"] if item["key"] != "current")
    assert_masked(snapshots[2])
    assert_masked(snapshots[3])


def test_zero_duration_reveal_closes_on_next_pointer_and_does_not_create_a_timeout():
    snapshots, = run_ui({"seconds": 0, "actions": [
        {"type": "pointer", "selector": ".history-primary > div:first-child strong"},
        {"type": "pointer", "selector": ".history-primary > div:first-child strong"},
        {"type": "pointer", "selector": ".history-stats b"},
        {"type": "pointer", "selector": ".history-title strong"},
    ]})
    assert field(snapshots[1], "current")["text"] == "€2,267.41"
    assert snapshots[1]["revealDelay"] is None
    assert_masked(snapshots[2])
    assert field(snapshots[3], "start")["text"] == "€2,098.75"
    assert_masked(snapshots[4])


def test_tooltip_reveal_is_explicit_and_remasks_dynamically_updated_text():
    snapshots, = run_ui({"actions": [
        {"type": "hover"},
        {"type": "pointer", "selector": "[data-trend-tooltip-value]"},
        {"type": "hover", "x": 500},
        {"type": "expire"},
        {"type": "hover"},
    ]})
    assert_masked(snapshots[1])
    assert "€" in field(snapshots[2], "tooltip-value")["text"]
    assert field(snapshots[2], "tooltip-change")["text"] == MASK
    assert "€" in field(snapshots[3], "tooltip-value")["text"]
    assert_masked(snapshots[4])
    assert_masked(snapshots[5])


@pytest.mark.parametrize("action", [{"type": "update"}, {"type": "render"}, {"type": "zoom"}])
def test_replacing_history_content_does_not_carry_reveal_permission_to_new_values(action):
    snapshots, = run_ui({"actions": [
        {"type": "pointer", "selector": ".history-primary > div:first-child strong"}, action,
    ]})
    assert "€" in field(snapshots[1], "current")["text"]
    assert_masked(snapshots[2])
    assert snapshots[2]["revealDelay"] is None


def test_metric_and_period_changes_remain_private_through_actual_controls():
    snapshots, = run_ui({"actions": [
        {"type": "click", "selector": '[data-trend-metric="costs"]'},
        {"type": "click", "selector": '[data-period="7d"]'},
        {"type": "hover"},
    ]})
    for snapshot in snapshots:
        assert_masked(snapshot)
    assert snapshots[1]["metric"] == "costs"
    assert snapshots[-1]["tooltipVisible"]


def test_reopening_a_preview_cannot_reuse_a_previous_dialog_reveal():
    snapshots, = run_ui({"pinned": False, "actions": [
        {"type": "click", "selector": ".history-primary > div:first-child strong"},
        {"type": "close"}, {"type": "load"},
    ]})
    assert not snapshots[1]["pinned"]
    assert field(snapshots[1], "current")["text"] == "€2,267.41"
    assert snapshots[2] == {"closed": True}
    assert_masked(snapshots[3])


def test_keyboard_reveal_uses_the_same_field_and_expiry_contract():
    snapshots, = run_ui({"seconds": 0, "actions": [
        {"type": "key", "selector": ".history-primary > div:first-child strong", "key": "Enter"},
        {"type": "key", "selector": ".history-primary > div:first-child strong", "key": " "},
    ]})
    assert field(snapshots[1], "current")["text"] == "€2,267.41"
    assert_masked(snapshots[2])


def test_enabling_privacy_in_an_open_history_removes_live_hover_and_summary_amounts():
    snapshots, = run_ui({"private": False, "actions": [
        {"type": "hover"}, {"type": "toggle"}, {"type": "hover"}, {"type": "toggle"},
    ]})
    assert field(snapshots[0], "current")["text"] == "€2,267.41"
    assert "€" in field(snapshots[1], "tooltip-value")["text"]
    assert_masked(snapshots[2])
    assert_masked(snapshots[3])
    assert not snapshots[4]["slotPrivate"]
    assert field(snapshots[4], "current")["text"] == "€2,267.41"
    assert all(item["role"] is None for item in snapshots[4]["fields"])


def test_remote_preference_change_hides_history_even_before_a_deferred_full_render():
    snapshots, = run_ui({"private": False, "actions": [
        {"type": "hover"}, {"type": "payload", "private": True},
    ]})
    assert_masked(snapshots[-1])


def test_loading_history_has_no_unmasked_amount_placeholder():
    snapshots, = run_ui({"loading": True})
    assert_masked(snapshots[0], complete=False)
    assert len(snapshots[0]["fields"]) == 3
