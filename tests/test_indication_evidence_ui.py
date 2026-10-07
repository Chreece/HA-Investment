"""Render both shipped indication UI layers against actual DOM nodes.

Requires Node and the test-only linkedom@0.18.12 package. Set
INVESTMENT_UI_NODE_MODULES to the directory containing that package when it is
installed outside Node's normal module search path. No network or HA session is
used by these tests. The module bootstrap and LinkeDOM's template serialization
gap are adapted for Node; both shipped renderers execute against real DOM nodes.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / "custom_components/investment/www/investment-panel.js"
RUNTIME = ROOT / "custom_components/investment/www/investment-panel-runtime.js"

NODE_RENDERER = r"""
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const dependency = process.env.INVESTMENT_UI_NODE_MODULES;
const { parseHTML } = require(dependency ? path.join(dependency, 'linkedom') : 'linkedom');
const { window } = parseHTML('<!doctype html><html><head></head><body></body></html>');
// LinkeDOM 0.18.12 clones template children into .content, but its inherited
// innerHTML getter serializes the original children. Browser templates serialize
// .content, including runtime edits. Keep that browser behavior in this harness.
const elementHtml = Object.getOwnPropertyDescriptor(window.Element.prototype, 'innerHTML');
Object.defineProperty(window.HTMLTemplateElement.prototype, 'innerHTML', {
  get() { return [...this.content.childNodes].map(node=>node.toString()).join(''); },
  set(value) {
    const content = this.content;
    elementHtml.set.call(this, value);
    content.replaceChildren(...[...this.childNodes].map(node=>node.cloneNode(true)));
  },
});
const context = vm.createContext({
  window, document: window.document, HTMLElement: window.HTMLElement,
  customElements: window.customElements, navigator: {language:'en'},
  console, URL, setTimeout, clearTimeout, Intl,
});
const base = fs.readFileSync(process.argv[1], 'utf8');
const runtime = fs.readFileSync(process.argv[2], 'utf8');
// The real browser bootstrap needs import.meta.url cache parameters. Register
// exactly the same class locally, then execute the complete runtime extension.
vm.runInContext(base.slice(0, base.indexOf('const PANEL_MODULE_REVISION =')) + `
  customElements.define('investment-panel-evidence-test', InvestmentPanel);
  globalThis.testPanelClass=InvestmentPanel;
  globalThis.testDictionaries=I18N;
  globalThis.testLanguages=UI_LANGUAGES.map(([language])=>language);
`, context, {filename: process.argv[1]});
vm.runInContext("const PANEL_ELEMENT_NAME='investment-panel-evidence-test';\n" +
  runtime.slice(runtime.indexOf('const Panel = customElements.get(')), context,
  {filename: process.argv[2]});
const cases = JSON.parse(fs.readFileSync(0, 'utf8'));
const output = cases.map(testCase => {
  const panel = new context.testPanelClass();
  panel._lang = testCase.language || 'en';
  panel._preferredLang = panel._lang;
  panel._haLang = panel._lang;
  panel._dict = context.testDictionaries[panel._lang];
  panel._portfolio = {base_currency:'EUR', holdings:[]};
  panel._indicationResult = testCase.result || null;
  panel._indicationDraft.scope = testCase.result?.scope || 'discover';
  const original = JSON.stringify(panel._indicationResult);
  const html = panel.indicationModalHtml();
  const template = window.document.createElement('template');
  template.innerHTML = html;
  const root = template.content;
  const text = (node, selector) => node.querySelector(selector)?.textContent || '';
  const labelText = node => {
    if (!node) return '';
    const label = node.cloneNode(true);
    label.querySelectorAll('.field-help').forEach(help=>help.remove());
    return label.textContent;
  };
  const cards = [...root.querySelectorAll('.indication-card')].map(card => ({
    name:text(card,'.indication-title strong'),
    identity:text(card,'.indication-identity'),
    evidence:text(card,'.indication-evidence-warning'),
    warning:text(card,'.indication-warnings'),
    reliability:labelText(card.querySelectorAll('.indication-meta > span')[1]),
    metricValues:[...card.querySelectorAll('.indication-metrics > span > b')].map(node=>node.textContent),
    classificationPercent:text(card,'.classification-confidence'),
  }));
  const excluded = [...root.querySelectorAll('.excluded-candidates > div')].map(row => ({
    symbol:text(row,'b'), reason:text(row,':scope > span'), identity:text(row,'.indication-identity'),
  }));
  const keys=['productIdentity','marketDataUnverified','productIdentityUnverified','noAllocationEvidence'];
  return {
    html, cards, excluded,
    runtimeApplied:!!root.querySelector('style[data-investment-help-runtime]'),
    excludedSummary:text(root,'.excluded-candidates > summary'),
    excludedOpen:root.querySelector('.excluded-candidates')?.hasAttribute('open') || false,
    sourceSummary:root.querySelectorAll('.indication-method small')[1]?.textContent || '',
    empty:text(root,'.indication-results .empty-mini'),
    visible:panel.indicationVisibleResults(panel._indicationResult).map(row=>row.symbol),
    messages:(testCase.rows || []).map(row=>panel.indicationEvidenceMessages(row)),
    signals:(testCase.codes || []).map(code=>panel.indicationSignalText(code)),
    translations:Object.fromEntries(keys.map(key=>[key,panel.t(key)])),
    supportedLanguages:context.testLanguages,
    inputUnchanged:original===JSON.stringify(panel._indicationResult),
    injectedNodes:root.querySelectorAll('script, img[data-injected]').length,
  };
});
process.stdout.write(JSON.stringify(output));
"""


def render(*cases):
    node = shutil.which("node")
    assert node, "Node is required to test the shipped frontend"
    completed = subprocess.run(
        [node, "-e", NODE_RENDERER, str(PANEL), str(RUNTIME)],
        input=json.dumps(cases), text=True, capture_output=True, check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    pages = json.loads(completed.stdout)
    assert all(page["runtimeApplied"] for page in pages), "Runtime DOM changes must survive serialization"
    return pages


def candidate(symbol="EUNL.DE", **changes):
    row = {
        "provider":"yahoo", "provider_id":symbol, "symbol":symbol,
        "name":"Example fund", "category":"etf", "confidence":0.9,
        "economic_classification_confidence":None,
        "score":80, "label":"candidate", "allocation_eligible":True,
        "suggested_amount":20, "suggested_units":0.2,
        "warnings":[], "reasons":[], "metrics":{},
        "instrument_identity_eligible":True, "data_quality_eligible":True,
        "instrument_identity":{
            "instrument_identity_eligible":True,
            "isin":"IE00B4L5Y983", "share_class":"USD Accumulating",
            "listing_exchange":"XETRA", "listing_currency":"EUR",
        },
    }
    row.update(changes)
    return row


def blocked(symbol="BLOCKED.DE", *, identity=False, code=None, **changes):
    code = code or ("issuer_fund_identity_unverified" if identity else "quote_stale")
    row = candidate(
        symbol, score=None, label="caution", confidence=0,
        suggested_amount=0, suggested_units=0, allocation_eligible=False,
        instrument_identity_eligible=not identity, data_quality_eligible=False,
        input_evidence_blockers=[code], warnings=[code],
    )
    if identity:
        row["instrument_identity"] = {
            "instrument_identity_eligible":False,
            "instrument_identity_blockers":[code],
        }
    row.update(changes)
    return row


def analysis(rows, *, scope="discover", excluded=None, **changes):
    result = {
        "scope":scope, "source":scope, "mode":"deterministic", "amount":100,
        "portfolio_currency":"EUR", "results":rows, "preferences":{},
        "excluded_candidates":excluded or [], "excluded_candidate_count":len(excluded or []),
        "evaluated_candidate_count":len(rows), "selected_candidate_count":len(rows),
    }
    result.update(changes)
    return result


@pytest.mark.parametrize("legacy_confidence", [None,0.9])
def test_exact_identity_survives_runtime_overlay_without_classification_percentage(legacy_confidence):
    page, = render({"result":analysis([candidate(economic_classification_confidence=legacy_confidence)])})
    card = page["cards"][0]
    assert card["identity"] == "Product identity:IE00B4L5Y983 · USD Accumulating · XETRA · EUR"
    assert "%" not in card["identity"]
    assert card["classificationPercent"] == ""
    assert "Data reliability" in card["reliability"]
    assert "90.00%" in card["reliability"]  # Existing historical-data measure remains.
    assert page["inputUnchanged"]


def test_discovery_keeps_evidence_failures_accessible_without_zero_unit_cards():
    page, = render({"result":analysis([
        candidate(), blocked("STALE.DE"), blocked("UNKNOWN.DE", identity=True),
    ])})
    assert page["visible"] == ["EUNL.DE"]
    assert len(page["cards"]) == 1
    assert [(row["symbol"], row["reason"]) for row in page["excluded"]] == [
        ("STALE.DE", "Market data could not be verified"),
        ("UNKNOWN.DE", "Product identity could not be verified"),
    ]
    assert page["excludedSummary"] == "Excluded candidates (2)"
    assert "Excluded candidates: 2" in page["sourceSummary"]
    assert "IE00B4L5Y983" in page["excluded"][0]["identity"]
    assert page["excluded"][1]["identity"] == ""
    assert page["inputUnchanged"]


@pytest.mark.parametrize("scope", ["search", "portfolio"])
def test_explicit_sources_keep_blocked_cards_and_explain_them(scope):
    page, = render({"result":analysis([blocked()], scope=scope)})
    assert page["visible"] == ["BLOCKED.DE"]
    assert page["excluded"] == []
    card = page["cards"][0]
    assert "No allocation: evidence unavailable" in card["evidence"]
    assert "Market data could not be verified" in card["evidence"]
    assert "quote_stale" not in card["evidence"]
    assert "%" not in card["reliability"]
    assert card["reliability"].startswith("Data reliability: —")
    assert card["metricValues"][:2] == ["—", "—"]


def test_all_evidence_blocked_opens_explanations_and_does_not_blame_whole_units():
    result = analysis([blocked(identity=True)], preferences={"whole_units_only":True})
    page, = render({"result":result})
    assert page["cards"] == []
    assert page["excludedOpen"]
    assert page["empty"] == "No allocation: evidence unavailable"
    assert page["excluded"][0]["reason"] == "Product identity could not be verified"


def test_unknown_exclusion_reasons_are_not_presented_as_portfolio_overlap():
    exclusions = [
        {"symbol":"REFERENCE", "reason":"nontradable_reference"},
        {"symbol":"IDENTITY", "reason":"identity_unverified"},
        {"symbol":"QUALITY", "reason":"data_quality"},
        {"symbol":"OVERLAP", "reason":"portfolio_fund_overlap"},
        {"symbol":"UNRECOGNISED", "reason":"another_explicit_rule"},
    ]
    page, = render({"result":analysis([], excluded=exclusions)})
    reasons = {row["symbol"]:row["reason"] for row in page["excluded"]}
    assert reasons["REFERENCE"] == "Nontradable reference"
    assert reasons["IDENTITY"] == "Product identity could not be verified"
    assert reasons["QUALITY"] == "Market data could not be verified"
    assert reasons["OVERLAP"] == "Excluded because of portfolio overlap"
    assert reasons["UNRECOGNISED"] == "Another explicit rule"


def test_existing_backend_exclusions_and_hidden_evidence_rows_are_deduplicated():
    result = analysis(
        [blocked()], excluded=[{"provider":"yahoo", "provider_id":"BLOCKED.DE", "symbol":"BLOCKED.DE", "reason":"data_quality"}],
    )
    page, = render({"result":result})
    assert len(page["excluded"]) == 1
    assert page["excluded"][0]["reason"] == "Market data could not be verified"
    assert page["excludedSummary"] == "Excluded candidates (1)"
    assert page["inputUnchanged"]


def test_different_providers_with_same_symbol_are_not_collapsed():
    page, = render({"result":analysis([blocked(), blocked(provider="other")])})
    assert len(page["excluded"]) == 2


def test_normal_unallocated_discovery_rows_keep_the_existing_visibility_policy():
    row = candidate(allocation_eligible=False, suggested_amount=0, suggested_units=0,
                    warnings=["market_activation_insufficient"])
    page, = render({"result":analysis([row])})
    assert page["cards"] == []
    assert page["excluded"] == []
    assert page["empty"] != "No allocation: evidence unavailable"


def test_discovery_without_budget_also_keeps_hidden_evidence_failures_accessible():
    page, = render({"result":analysis([candidate(), blocked(identity=True)], amount=None)})
    assert page["visible"] == ["EUNL.DE"]
    assert len(page["excluded"]) == 1


def test_nested_unverified_identity_overrides_stale_flat_identity_display_fields():
    row = blocked(identity=True, isin="IE00B4L5Y983", share_class="USD Accumulating",
                  listing_exchange="XETRA", listing_currency="EUR")
    page, = render({"result":analysis([row], scope="search")})
    assert page["cards"][0]["identity"] == ""


def test_verified_identity_values_and_exclusion_labels_are_html_escaped():
    row = candidate()
    row["instrument_identity"]["share_class"] = '<img data-injected="1" src=x onerror=alert(1)>'
    page, = render({"result":analysis([row], excluded=[{
        "symbol":"<script>alert(1)</script>", "reason":"nontradable_reference",
    }])})
    assert page["injectedNodes"] == 0
    assert "&lt;img" in page["html"]
    assert "&lt;script&gt;" in page["html"]


def test_missing_confidence_and_missing_returns_never_render_as_zero_measurements():
    page, = render({"result":analysis([candidate(confidence=None)])})
    assert page["cards"][0]["reliability"].startswith("Data reliability: —")
    assert page["cards"][0]["metricValues"][:2] == ["—", "—"]


def test_real_zero_return_still_displays_as_zero():
    page, = render({"result":analysis([candidate(metrics={"return_63d":0,"return_126d":0})])})
    assert page["cards"][0]["metricValues"][:2] == ["0.00%", "0.00%"]


def test_quality_objects_and_warning_only_rejections_are_explained():
    cases = [
        {"data_quality":{"quote":{"eligible":False,"reasons":["quote_timestamp_missing"]}}},
        {"data_quality":{"eligible":False,"reasons":["fx_history_no_asof_coverage"]}},
        {"warnings":["unsupported_high_yield_credit_structure"]},
        {"input_evidence_blockers":["future_unknown_evidence_blocker"]},
    ]
    page, = render({"rows":cases})
    assert page["messages"] == [
        ["Market data could not be verified"],
        ["Market data could not be verified"],
        ["Product identity could not be verified"],
        ["No allocation: evidence unavailable"],
    ]


def test_repeated_failure_codes_collapse_to_one_readable_message_per_kind():
    row = blocked(input_evidence_blockers=["quote_stale","risk_history_stale","invalid_isin"],
                  warnings=["quote_stale","risk_history_stale","invalid_isin"])
    page, = render({"rows":[row]})
    assert page["messages"] == [["Market data could not be verified", "Product identity could not be verified"]]


def test_runtime_signal_translation_uses_new_evidence_messages():
    codes = ["quote_stale","signal_history_tail_gap","fx_history_no_asof_coverage",
             "issuer_catalog_review_expired","isin_mismatch","quote_currency_mismatch"]
    page, = render({"codes":codes})
    assert page["signals"] == ["Market data could not be verified"] * 3 + ["Product identity could not be verified"] * 3


def test_runtime_warning_rewrite_does_not_restore_raw_evidence_codes():
    row = blocked(warnings=["quote_stale","near_term_trend_negative"])
    page, = render({"language":"el", "result":analysis([row], scope="search")})
    card = page["cards"][0]
    assert page["translations"]["marketDataUnverified"] in card["evidence"]
    assert page["translations"]["marketDataUnverified"] not in card["warning"]
    assert "Near term trend negative" in card["warning"]
    assert "quote_stale" not in page["html"]


def test_every_supported_locale_renders_its_evidence_and_identity_messages():
    baseline, = render({})
    languages = baseline["supportedLanguages"]
    assert len(languages) == 28
    cases = [{"language":language, "result":analysis([candidate(),blocked(identity=True)], scope="search"),
              "codes":["quote_stale","issuer_fund_identity_unverified"]} for language in languages]
    pages = render(*cases)
    for language, page in zip(languages, pages, strict=True):
        translations = page["translations"]
        assert all(value and value != key for key,value in translations.items())
        assert page["signals"] == [translations["marketDataUnverified"],translations["productIdentityUnverified"]]
        assert page["cards"][0]["identity"].startswith(translations["productIdentity"]+":")
        assert translations["noAllocationEvidence"] in page["cards"][1]["evidence"]
        assert translations["productIdentityUnverified"] in page["cards"][1]["evidence"]
        if language != "en":
            assert translations["noAllocationEvidence"] != baseline["translations"]["noAllocationEvidence"]


def test_rendering_does_not_modify_requested_result_objects():
    result = analysis([candidate(),blocked(identity=True)])
    original = deepcopy(result)
    page, = render({"result":result})
    assert page["inputUnchanged"]
    assert result == original
