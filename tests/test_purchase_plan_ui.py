"""Run purchase inputs, real request wiring and risk rendering in both UI layers."""
from copy import deepcopy
import json
import shutil
import subprocess

import pytest

from test_indication_evidence_ui import NODE_RENDERER, PANEL, RUNTIME, candidate


RENDERER = NODE_RENDERER.replace(
    "globalThis.testDictionaries=I18N;", "globalThis.testDictionaries=I18N;\n  globalThis.testPlanStrings=PURCHASE_PLAN_I18N;"
).replace(
    "const output = cases.map(testCase => {", "const output = await Promise.all(cases.map(async testCase => {"
).replace(
    "  const original = JSON.stringify(panel._indicationResult);",
    """  if(testCase.stored)panel._indicationDraft=panel.indicationDraftFromStored(testCase.stored);
  if(testCase.draft)panel._indicationDraft={...panel._indicationDraft,...testCase.draft};
  const calls=[];
  panel._hass={connection:{sendMessagePromise(message){calls.push(message);return Promise.resolve({job_id:'test-purchase-job'});}}};
  const original = JSON.stringify(panel._indicationResult);""",
).replace(
    "  const text = (node, selector)",
    """  const form=root.querySelector('#indication-form');
  // LinkeDOM has no HTMLInputElement.checked reflection; initialize the browser
  // property from the parsed attribute before exercising the real capture code.
  form.querySelectorAll('input[type="checkbox"]').forEach(input=>{input.checked=input.hasAttribute('checked');});
  for(const [name,value] of Object.entries(testCase.fields||{})){
    const input=form.querySelector(`[name="${name}"]`);
    if(!input)throw new Error('Missing actual form control: '+name);
    if(input.type==='checkbox')input.checked=value;
    else input.value=String(value);
  }
  if(testCase.capture||testCase.run)panel.captureIndicationDraft(form);
  if(testCase.run){panel.render=()=>{};panel.waitForIndicationJob=async()=>({results:[],scope:'discover'});await panel.runIndication(form);}
  else if(testCase.send)await panel.call({type:'investment/indication_start',amount:100});
  const planning=panel.indicationPlanningPayload();
  const persisted=panel.indicationPreferencesPayload();
  const text = (node, selector)""",
).replace(
    "  const keys=['productIdentity','marketDataUnverified','productIdentityUnverified','noAllocationEvidence'];",
    "  const keys=Object.keys(context.testPlanStrings.en);",
).replace(
    "    html, cards, excluded,",
    """    html, cards, excluded, calls, planning, persisted,
    validInputs:panel.indicationPlanningInputsValid(planning),
    error:panel._indicationError,
    ownTranslations:keys.every(key=>typeof context.testPlanStrings[panel._lang]?.[key]==='string'&&context.testPlanStrings[panel._lang][key].length>0),
    inputNames:[...form.querySelectorAll('input[name]')].map(input=>input.name),
    checked:!!form.querySelector('[name="indication_costs_confirmed"]')?.checked,
    riskStatus:text(root,'.indication-risk-status'),
    riskScope:text(root,'.indication-risk-scope'),
    costStatus:root.querySelector('.indication-cost-summary')?.getAttribute('data-cost-status'),
    costText:text(root,'.indication-cost-summary'),
    missingPositions:text(root,'.indication-missing-positions'),
    portfolioChanged:text(root,'.indication-portfolio-changed'),
    riskRows:[...root.querySelectorAll('.indication-portfolio-risk > .indication-risk-table-scroll tbody tr')].map(row=>[...row.querySelectorAll('th,td')].map(cell=>cell.textContent)),
    stressRows:[...root.querySelectorAll('.indication-stress-scenarios tr')].map(row=>({status:row.getAttribute('data-scenario-status'),text:row.textContent})),
    bootstrapText:text(root,'.indication-bootstrap-ranges'),
    fit:text(root,'.portfolio-fit'),
    cardCost:text(root,'.indication-card .indication-purchase-cost'),
    concentration:text(root,'.indication-concentration-limit'),""",
).replace(
    "});\nprocess.stdout.write(JSON.stringify(output));",
    "}));\nprocess.stdout.write(JSON.stringify(output));",
)
RENDERER = "(async()=>{\n" + RENDERER + "\n})().catch(error=>{console.error(error);process.exit(1);});"


def render(*cases):
    done = subprocess.run(
        [shutil.which("node"), "-e", RENDERER, str(PANEL), str(RUNTIME)],
        input=json.dumps(cases), capture_output=True, text=True, timeout=35, check=False,
    )
    assert done.returncode == 0, done.stderr
    output = json.loads(done.stdout)
    assert all(item["runtimeApplied"] for item in output)
    return output


def profile(**changes):
    return {"confirmed": True, "fixed_fee": 1, "commission_pct": 0.25,
            "spread_bps": 10, "fx_bps": 0, **changes}


def signature(vol=0.20, es=0.045, drawdown=0.25, weeks=159, weight=0.4):
    return {"annualized_volatility_3y": vol, "expected_shortfall_95_weekly_3y": es,
            "max_drawdown_3y": drawdown, "weekly_observations_3y": weeks,
            "instrument_weights": {"example": weight}}


def result(*, mode="deterministic", status="within_limits", scope="complete_portfolio"):
    risk = {
        "scope": scope, "status": status,
        "pre": signature(vol=0.9),  # This excludes purchase cash and is not the Before column.
        "before_purchases": signature(), "post": signature(vol=0.18, weight=0.38),
        "limits": {"instrument_fraction": 0.25},
        "evidence": {
            "history": {"first_week":"2023-W41", "last_week":"2026-W41", "observations":159},
            "tail": {"observation_mass":7.95,"complete_observations":7,"boundary_observation_fraction":0.95},
            "windows": {"full":{"observations":159,"annualized_volatility":0.18,"expected_shortfall_95_weekly":0.04,"path":{"max_drawdown_loss":0.25}}},
            "bootstrap": {"status":"ok", "resampling_quantiles": {
                "annualized_volatility":{"lower_05":0.15,"median_50":0.18,"upper_95":0.24},
                "expected_shortfall_95_weekly":{"lower_05":0.03,"median_50":0.04,"upper_95":0.08},
                "max_drawdown_loss":{"lower_05":0.10,"median_50":0.25,"upper_95":0.40},
            }},
            "stress": {"scenarios":[
                {"id":"sustained_decline","status":"modelled","portfolio_loss":0.25},
                {"id":"decline_then_full_rebound","status":"modelled","portfolio_loss":0,"max_drawdown_loss":0.2},
                {"id":"rates_up_200bps","status":"unknown","portfolio_loss":None},
            ]},
            "horizon": {"status":"observed","horizon_weeks":52,"min_horizon_return":-0.15,"max_horizon_return":0.30},
        },
    }
    cost = {"confirmed":True,"status":"confirmed","estimated_transaction_cost":2,
            "estimated_cash_debit":42,"cash_remaining":58,"principal":40,"profile":profile()}
    allocation = {"budget":100,"deployed":40,"cash_reserve":58,"execution_costs":cost,"portfolio_risk":risk}
    rows = [candidate(suggested_amount=40,suggested_units=0.4,estimated_transaction_cost=2,estimated_cash_debit=42)]
    return {"scope":"discover","mode":mode,"portfolio_currency":"EUR","amount":100,
            "preferences":{"portfolio_context":"use","risk_tolerance":"medium","horizon":"long","strategy":"adaptive"},
            "results":rows,"allocation":allocation,"evaluated_candidate_count":1,"selected_candidate_count":1,"errors":[]}


def test_first_open_has_unconfirmed_zero_estimates_and_no_invented_limits():
    page, = render({"capture": True})
    assert not page["checked"]
    assert page["planning"]["execution_costs"] == profile(confirmed=False,fixed_fee=0,commission_pct=0,spread_bps=0)
    assert page["planning"]["max_drawdown_pct"] is None
    assert page["planning"]["analysis_horizon_weeks"] is None
    assert page["planning"]["existing_cash"] == 0
    assert page["validInputs"]
    assert len([name for name in page["inputNames"] if name.startswith("indication_")]) >= 14


def test_actual_form_capture_submit_and_persistence_send_identical_cost_authority():
    fields = {
        "indication_amount":"100", "indication_fixed_fee":"1.2", "indication_commission_pct":"0.25",
        "indication_spread_bps":"10", "indication_fx_bps":"5", "indication_costs_confirmed":True,
        "indication_existing_cash":"500", "indication_max_drawdown_pct":"20", "indication_analysis_weeks":"104",
    }
    page, = render({"fields":fields,"run":True})
    assert not page["error"]
    assert len(page["calls"]) == 1
    call = page["calls"][0]
    assert call["type"] == "investment/indication_start"
    assert call["amount"] == 100  # The other 500 is never added to the purchase budget.
    assert call["execution_costs"] == profile(fixed_fee=1.2,fx_bps=5)
    assert call["existing_cash"] == 500
    assert call["max_drawdown_pct"] == 20
    assert call["analysis_horizon_weeks"] == 104
    for key in page["planning"]:
        assert page["persisted"][key] == call[key]


def test_roundtrip_preferences_restore_confirmed_costs_and_optional_horizon():
    stored = {"execution_costs":profile(),"existing_cash":230,"max_drawdown_pct":12.5,"analysis_horizon_weeks":52}
    page, = render({"stored":stored,"send":True})
    for key, value in stored.items():
        assert page["planning"][key] == value
        assert page["persisted"][key] == value
        assert page["calls"][0][key] == value


@pytest.mark.parametrize("field,value", [
    ("indication_fixed_fee",""), ("indication_fixed_fee","-1"),
    ("indication_commission_pct","101"), ("indication_spread_bps","10001"),
    ("indication_fx_bps","no"), ("indication_existing_cash","-1"),
    ("indication_max_drawdown_pct","0"), ("indication_max_drawdown_pct","101"),
    ("indication_max_drawdown_pct","bad"), ("indication_analysis_weeks","0"),
    ("indication_analysis_weeks","1.2"), ("indication_analysis_weeks","5201"),
])
def test_actual_submit_rejects_invalid_inputs_before_backend_request(field, value):
    page, = render({"fields":{field:value,"indication_amount":"100","indication_costs_confirmed":True},"run":True})
    assert not page["validInputs"]
    assert page["error"]
    assert not page["calls"]


def test_partial_cost_edits_persist_unknown_instead_of_silently_zeroing():
    page, = render({"fields":{"indication_fixed_fee":"","indication_costs_confirmed":False},"capture":True})
    assert page["planning"]["execution_costs"]["fixed_fee"] is None
    assert page["persisted"]["execution_costs"] is None
    assert not page["validInputs"]


def test_complete_portfolio_table_uses_before_purchase_cash_and_reports_all_diagnostics():
    data = result()
    before = deepcopy(data)
    page, = render({"result":data})
    assert page["inputUnchanged"] and data == before
    assert page["riskScope"] == "Holdings, purchases and cash"
    assert page["riskStatus"] == "Within the selected historical limits"
    assert page["riskRows"][0][1:] == ["20.00%", "18.00%"]
    assert page["riskRows"][-1][1:] == ["40.00%", "38.00%"]
    assert "25.00%" in page["concentration"]
    assert "combined holdings after purchases" in page["concentration"]
    assert "7.95" in page["html"]
    assert "2023-W41" in page["html"] and "2026-W41" in page["html"]
    assert "15.00% – 24.00%" in page["bootstrapText"]
    assert "not a forecast interval" in page["html"]
    assert "€2.00" in page["cardCost"] and "€42.00" in page["cardCost"]
    assert "−15.00%" in page["html"] or "-15.00%" in page["html"]


def test_unknown_stress_exposures_do_not_render_as_zero_risk_and_rebound_shows_drawdown():
    page, = render({"result":result()})
    assert page["stressRows"][-1] == {"status":"unknown","text":"Interest rates: +2 percentage pointsAssessment incomplete"}
    assert "20.00%" in page["stressRows"][1]["text"]
    assert "0%" not in page["stressRows"][-1]["text"]


def test_incomplete_existing_holding_evidence_names_the_holding_without_raw_errors():
    data = result(status="unknown")
    risk=data["allocation"]["portfolio_risk"]
    risk.update(before_purchases=None,baseline_with_contribution=None,post=None,evidence={},
                existing_position_issues=[{"symbol":"MISSING.DE","reasons":["internal_trace_secret"]}])
    page, = render({"result":data})
    assert page["riskStatus"] == "Assessment incomplete"
    assert all(row[1:]==["—","—"] for row in page["riskRows"])
    assert "MISSING.DE" in page["missingPositions"]
    assert "internal_trace_secret" not in page["html"]


def test_existing_breach_is_not_labelled_as_fitting_portfolio_limits():
    page, = render({"result":result(status="existing_breach_not_worsened")})
    assert "Existing breach remains" in page["riskStatus"]
    assert "Existing breach remains" in page["fit"]
    assert "Fits the selected portfolio limits" not in page["fit"]


def test_unknown_costs_block_copy_does_not_destroy_verified_history_reliability():
    data = result(status="blocked")
    data["scope"]="search"
    data["allocation"]["execution_costs"].update(confirmed=False,status="unknown")
    data["results"][0].update(suggested_amount=0,suggested_units=0,allocation_eligible=False,
                              execution_cost_blockers=["execution_costs_unknown"])
    page, = render({"result":data})
    assert "confirm the cost estimates first" in page["costText"]
    assert "90.00%" in page["cards"][0]["reliability"]
    assert page["cardCost"] == ""


def test_ai_mode_uses_final_ai_quantities_costs_and_risk_scope():
    data=result(mode="full_ai")
    data["ai_allocation"]=deepcopy(data["allocation"])
    data["ai_allocation"]["execution_costs"].update(estimated_transaction_cost=1,estimated_cash_debit=21)
    data["ai_allocation"]["portfolio_risk"].update(scope="new_contribution_only")
    data["results"][0].update(ai_suggested_amount=20,ai_suggested_units=0.2,
                              ai_estimated_transaction_cost=1,ai_estimated_cash_debit=21)
    page, = render({"result":data})
    assert page["riskScope"] == "Purchase budget only"
    assert "€21.00" in page["costText"] and "€42.00" not in page["costText"]
    assert "€21.00" in page["cardCost"] and "€42.00" not in page["cardCost"]


def test_all_28_languages_have_complete_translations_for_the_new_real_controls():
    first,=render({"result":result()})
    pages=render(*({"language":language,"result":result()} for language in first["supportedLanguages"]))
    assert len(pages)==28
    for page in pages:
        assert page["ownTranslations"]
        assert len(page["translations"])==54
        assert page["riskStatus"]==page["translations"]["riskStatusWithin"]
        assert page["riskScope"]==page["translations"]["riskScopeComplete"]
        assert page["translations"]["costsConfirmed"] in page["html"]
        assert page["translations"]["existingCashHint"] in page["html"]


def test_provider_or_holding_text_cannot_inject_html_into_new_risk_details():
    data=result(status="unknown")
    risk=data["allocation"]["portfolio_risk"]
    risk["existing_position_issues"]=[{"symbol":"<img data-injected src=x onerror=alert(1)>"}]
    risk["evidence"]["history"]["first_week"]="<script>alert(1)</script>"
    page,=render({"result":data})
    assert page["injectedNodes"]==0
    assert "&lt;script&gt;" in page["html"]


def test_concurrent_portfolio_change_shows_translated_retry_in_all_languages_and_ai():
    data=result(status="unknown")
    data["allocation"]["portfolio_risk"].update(
        ledger_changed_during_analysis=True,
        existing_position_issues=[{"symbol":"__portfolio_changed__","provider_id":"__portfolio__","reasons":["portfolio_changed_during_analysis"]}],
        before_purchases=None,baseline_with_contribution=None,post=None,evidence={},
    )
    first,=render({"result":data})
    assert first["portfolioChanged"]=="The portfolio changed during analysis. Run the analysis again."
    pages=render(*({"language":language,"result":data} for language in first["supportedLanguages"]))
    assert len(pages)==28
    for page in pages:
        assert page["portfolioChanged"]==page["translations"]["portfolioChangedDuringAnalysis"]
        assert page["riskStatus"]==page["translations"]["riskStatusUnknown"]
        assert page["missingPositions"]==""
        assert "__portfolio_changed__" not in page["html"]
        assert page["inputUnchanged"]
    ai=deepcopy(data)
    ai["mode"]="full_ai"
    ai["ai_allocation"]=ai.pop("allocation")
    ai["results"][0].update(ai_suggested_amount=0,ai_suggested_units=0)
    page,=render({"result":ai,"language":"el"})
    assert page["portfolioChanged"]==page["translations"]["portfolioChangedDuringAnalysis"]
    assert page["missingPositions"]==""
