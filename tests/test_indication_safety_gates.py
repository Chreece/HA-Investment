"""Behavioral safety contracts, independent of any particular budget or ticker.

Realistic-history and adversarial inputs both matter. Random cases are seeded
and all must preserve the approved set; they need not return a purchase. This
module requires only pytest and the Python standard library.
"""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
import math
import os
from pathlib import Path
import random
import sys

import pytest


SOURCE = Path(os.environ.get(
    "INVESTMENT_SAFETY_MODEL",
    str(Path(__file__).resolve().parents[1] / "custom_components/investment/validated_model.py"),
))
spec = spec_from_file_location("indication_safety_subject", SOURCE)
v = module_from_spec(spec)
sys.modules[spec.name] = v
spec.loader.exec_module(v)


def history(count=156, offset=0, magnitude=0.001):
    out = {}
    for index in range(count):
        day = date(2020, 1, 6) + timedelta(weeks=index + offset)
        iso = day.isocalendar()
        out[f"{iso.year}-W{iso.week:02d}"] = magnitude * (-1 if index % 2 else 1)
    return out


def asset(symbol, *, price=7.0, score=80.0, confidence=1.0, context=1.0,
          returns=None, category="etf", name="Synthetic Total Stock Market ETF"):
    return {"provider": "test", "provider_id": symbol, "symbol": symbol,
            "category": category, "name": name, "economic_sleeve": "broad_equity",
            "portfolio_price": price, "market_score": score, "confidence": confidence,
            "allocation_context_scale": context,
            "risk_weekly_returns": history() if returns is None else returns}


def constrained(items, risk="medium", floor=0.45, cap=None, reserve=0):
    weighted, _ = v.validated_exact_weights(items, risk)
    return v.apply_downward_weight_constraints(
        weighted, risk=risk, min_confidence=floor,
        max_candidate_fraction=cap, minimum_cash_reserve_fraction=reserve,
    )[0]


@pytest.mark.parametrize("risk", v.RISK_ORDER)
@pytest.mark.parametrize("reason", ["signal", "confidence", "context", "history"])
@pytest.mark.parametrize("budget", [29.73, 937.41, 12345.67])
def test_rejected_candidates_never_reenter_lot_search(risk, reason, budget):
    good = asset("approved", price=budget * 2)
    bad = asset("rejected", price=round(budget * 0.01, 2))
    if reason == "signal":
        bad["market_score"] = 40.0
    elif reason == "confidence":
        bad["confidence"] = 0.1
    elif reason == "context":
        bad["allocation_context_scale"] = 0.0
    else:
        bad["risk_weekly_returns"] = {}
    items = [good, bad]
    original = deepcopy(items)
    weighted = constrained(items, risk)
    assert all(row["symbol"] != "rejected" for row, weight in weighted if weight > v.EPS)
    rows, meta = v.production_projection(items, weighted, risk, budget, whole_units_only=True)
    assert all(row["suggested_amount"] == 0.0 for row in rows)
    assert all(row["suggested_units"] == 0.0 for row in rows)
    assert all(row["allocation_eligible"] is False for row in rows)
    assert meta["cash_reserve"] == budget
    assert items == original  # Caller data is not mutated.


def test_eligible_candidate_with_cent_target_zero_still_allowed_for_lot_fitting():
    # Eligibility is not the same as a positive amount after cent rounding.
    items = [asset("rounds-to-zero", price=.01), asset("unaffordable", price=10000)]
    weighted = [(items[0], .000001), (items[1], .10)]
    base, _ = v.constrained_cent_projection(items, weighted, "medium", 913.37)
    assert base[0]["suggested_amount"] == 0
    rows, _ = v.production_projection(items, weighted, "medium", 913.37, whole_units_only=True)
    assert rows[0]["suggested_amount"] > 0


def test_approved_collective_whole_lot_capacity_still_works():
    items = [asset("a", price=70), asset("b", price=80)]
    rows, meta = v.production_projection(items, [(items[0], .04), (items[1], .05)],
                                         "medium", 1000, whole_units_only=True)
    assert 0 < meta["deployed"] <= 90
    assert all(float(row["suggested_units"]).is_integer() for row in rows)


@pytest.mark.parametrize("bad", [{}, None, [], history(51),
    {**history(), "2020-W02": None}, {**history(), "2020-W02": "bad"},
    {**history(), "2020-W02": math.nan}, {**history(), "2020-W02": math.inf},
    {**history(), "2020-W02": -math.inf}, {**history(), "2020-W02": True},
    {**history(), "2020-W02": -1.01}, {**history(), "": .01}])
def test_bad_history_cannot_borrow_other_positions_history(bad):
    a = asset("observed")
    b = asset("unknown")
    b["risk_weekly_returns"] = bad
    assert not v.within_risk_target(v.risk_signature([(a, .1), (b, .1)]), "very_low")
    weights, meta = v.scale_down_to_risk_contract([(a, .1), (b, .1)], "very_low")
    assert weights == []
    assert meta["risk_scale"] == 0
    assert meta["blocked_reason"] == "insufficient_aligned_risk_history"


def test_no_history_candidate_stays_zero_in_mixed_whole_fractional_output():
    items = [asset("whole", price=10000), asset("missing", returns={}),
             asset("fractional", category="stock", name="Synthetic Company")]
    rows, _ = v.production_projection(items, constrained(items), "medium", 937.41,
                                      whole_unit_categories=["etf"])
    missing = next(row for row in rows if row["symbol"] == "missing")
    assert missing["suggested_amount"] == 0
    assert missing["allocation_eligible"] is False
    assert next(row for row in rows if row["symbol"] == "fractional")["suggested_amount"] > 0


@pytest.mark.parametrize("tiny", [1e-6, 1e-10, 1e-16])
def test_positive_but_tiny_unknown_position_is_not_cash(tiny):
    a, b = asset("a"), asset("b", returns={})
    assert not v.within_risk_target(v.risk_signature([(a, .1), (b, tiny)]), "medium")
    assert v.scale_down_to_risk_contract([(a, .1), (b, tiny)], "medium")[0] == []
    assert v.within_risk_target(v.risk_signature([(a, .1), (b, 0)]), "medium")


@pytest.mark.parametrize("weight", [math.nan, math.inf, -math.inf, -0.1, True])
def test_invalid_weights_do_not_produce_safe_signature(weight):
    assert not v.within_risk_target(v.risk_signature([(asset("a"), weight)]), "very_high")


@pytest.mark.parametrize("field", ["annualized_volatility_3y", "expected_shortfall_95_weekly_3y"])
@pytest.mark.parametrize("bad", [None, "bad", math.nan, math.inf, -math.inf, -0.01, True, False])
def test_nonfinite_or_negative_risk_statistic_cannot_pass(field, bad):
    sig = v.risk_signature([(asset("a"), .1)])
    sig[field] = bad
    assert not v.within_risk_target(sig, "very_high")


@pytest.mark.parametrize("count", [None, 0, 51, math.nan, math.inf])
def test_risk_signature_requires_enough_observations(count):
    sig = v.risk_signature([(asset("a"), .1)])
    sig["weekly_observations_3y"] = count
    assert not v.within_risk_target(sig, "medium")


def test_disjoint_history_is_not_zero_filled():
    a = asset("a", returns=history(offset=0, magnitude=.035))
    b = asset("b", returns=history(offset=156, magnitude=.035))
    sig = v.risk_signature([(a, .24), (b, .24)])
    assert sig["weekly_observations_3y"] == 0
    assert not v.within_risk_target(sig, "low")


@pytest.mark.parametrize("common", [0, 1, 51, 52, 100])
def test_common_history_threshold_and_values_are_exact(common):
    a = asset("a", returns=history(count=156))
    b = asset("b", returns=history(count=156, offset=156-common, magnitude=.002))
    series = v.portfolio_weekly_returns([(a, .2), (b, .3)])
    keys = sorted(set(a["risk_weekly_returns"]) & set(b["risk_weekly_returns"]))
    assert len(series) == common
    assert series == pytest.approx([.2*a["risk_weekly_returns"][k] + .3*b["risk_weekly_returns"][k] for k in keys])
    assert v.within_risk_target(v.risk_signature([(a,.2),(b,.3)]), "medium") == (common >= 52)


def test_well_formed_aligned_history_matches_direct_calculation():
    a = asset("a"); b = asset("b", returns=history(magnitude=.005))
    returns = [.12*a["risk_weekly_returns"][k] + .33*b["risk_weekly_returns"][k]
               for k in sorted(a["risk_weekly_returns"])]
    sig = v.risk_signature([(a, .12), (b, .33)])
    import statistics
    assert sig["annualized_volatility_3y"] == pytest.approx(statistics.pstdev(returns)*math.sqrt(52))
    assert sig["expected_shortfall_95_weekly_3y"] == pytest.approx(v.expected_shortfall_loss(returns))


def test_missing_weeks_are_not_miscounted_as_regular_returns():
    start = datetime(2021, 1, 4, tzinfo=timezone.utc)
    points = [(int((start+timedelta(days=21*i)).timestamp()),100+i*.01) for i in range(60)]
    assert v.weekly_return_map_from_points(points) == {}


def test_adjacent_iso_weeks_across_year_boundary_are_retained():
    days = [datetime(2020,12,28,tzinfo=timezone.utc), datetime(2021,1,4,tzinfo=timezone.utc),
            datetime(2021,1,18,tzinfo=timezone.utc), datetime(2021,1,25,tzinfo=timezone.utc)]
    result = v.weekly_return_map_from_points([(int(d.timestamp()), p) for d,p in zip(days,[10,11,12,13])])
    assert set(result) == {"2021-W01", "2021-W04"}
    assert result["2021-W01"] == pytest.approx(.1)
    assert result["2021-W04"] == pytest.approx(13/12-1)


@pytest.mark.parametrize("whole", [False, True])
def test_ai_cannot_restore_explicit_ineligible_candidate(whole):
    a = {**asset("no"), "allocation_eligible": False, "suggested_amount": 35.0}
    results = [a]
    ranking = [{**a,"action":"buy","score":100,"suggested_amount":10000}]
    v.clamp_ai_ranking_to_deterministic(results, ranking, 739.13, risk="medium", whole_units_only=whole)
    assert results[0]["ai_suggested_amount"] == 0
    assert results[0]["ai_action"] == "watch"


@pytest.mark.parametrize("seed", range(100))
@pytest.mark.parametrize("risk", v.RISK_ORDER)
def test_generated_allocation_contracts(seed, risk):
    # 500 generated markets x three execution modes; repeated call determinism
    # and final AI ceilings are also checked. Nothing requires a positive result.
    rng = random.Random(f"safety-v1:{risk}:{seed}")
    budget = round(math.exp(rng.uniform(math.log(15), math.log(50000))),2)
    common = [rng.gauss(.0002, .012) for _ in range(156)]
    items=[]
    for i in range(rng.randint(2,5)):
        category, name, scale = rng.choice([
            ("etf", "Synthetic Total Stock Market ETF", 1.4),
            ("etf", "Synthetic Overnight Money Market ETF", .05),
            ("fund", "Synthetic Government Bond Fund", .5),
            ("stock", "Synthetic Company", 2.0),
            ("crypto", "Synthetic Crypto", 3.0)])
        data = {key: scale*(.6*ret+rng.gauss(0,.01)) for key,ret in zip(history(),common)}
        if rng.random()<.10: data = {}
        items.append(asset(str(i), price=round(budget*rng.uniform(.03,.5),2),
                           score=rng.uniform(35,95),confidence=rng.uniform(.1,1),
                           context=rng.choice([0.,.3,.8,1.]), returns=data,category=category,name=name))
    cap=rng.choice([None, .1, .3, .6]); reserve=rng.choice([0.,.1,.5,.95,1.]); floor=rng.choice([0.,.45,.8])
    weighted=constrained(items,risk,floor,cap,reserve)
    approved={v._identity(a) for a,w in weighted if w>v.EPS}
    base,_=v.constrained_cent_projection(items,weighted,risk,budget)
    ss={}
    for a in base:
        k=a["economic_sleeve"]; ss[k]=ss.get(k,0)+a["suggested_amount"]
    for mode in ("fractional","whole","mixed"):
        kwargs={"max_candidate_fraction":cap, "whole_units_only":mode=="whole",
                "whole_unit_categories":["etf"] if mode=="mixed" else []}
        rows,meta=v.production_projection(items,weighted,risk,budget,**kwargs)
        again,again_meta=v.production_projection(items,weighted,risk,budget,**kwargs)
        assert rows==again and meta==again_meta
        assert meta["deployed"]<=budget*(1-reserve)+.011
        assert meta["deployed"]<=sum(a["suggested_amount"] for a in base)+.011
        total_by_sleeve={}
        for a in rows:
            amount=a["suggested_amount"]
            assert math.isfinite(amount) and amount>=0
            assert amount==0 or v._identity(a) in approved
            assert a["allocation_eligible"]==(amount>0)
            if cap is not None: assert amount<=budget*cap+.011
            if a["whole_units_only"]: assert float(a["suggested_units"]).is_integer()
            k=a["economic_sleeve"];total_by_sleeve[k]=total_by_sleeve.get(k,0)+amount
        assert all(z<=ss.get(k,0)+.011 for k,z in total_by_sleeve.items())
        if meta["deployed"]>0:
            sig=v.risk_signature([(a,a["suggested_amount"]/budget) for a in rows if a["suggested_amount"]>0])
            assert v.within_risk_target(sig,risk)
        ranking=[{**a,"action":"buy","suggested_amount":budget*10,"score":100} for a in rows]
        ai=deepcopy(rows)
        ai_meta=v.clamp_ai_ranking_to_deterministic(ai,ranking,budget,risk=risk,
                           whole_units_only=mode=="whole",whole_unit_categories=kwargs["whole_unit_categories"])
        assert ai_meta["deployed"]<=meta["deployed"]+.005
        for original, after in zip(rows,ai):
            assert after["ai_suggested_amount"]<=original["suggested_amount"]+.005
            if v._identity(after) not in approved: assert after["ai_suggested_amount"]==0
