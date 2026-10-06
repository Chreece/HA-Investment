"""Independent audit of the unmodified indications algorithm.

No fixtures assert that a particular budget must produce an investment.
Synthetic experiments test contracts, not market profitability. Random seeds,
inputs, implementation hashes and counterexamples are exported for repetition.
"""
from __future__ import annotations
import ast
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time

import numpy as np
from sklearn.covariance import LedoitWolf

ROOT = Path(__file__).resolve().parents[1]
COMP = ROOT / 'custom_components/investment'
OUT = ROOT / 'audit-results'
OUT.mkdir(exist_ok=True)
BASE = '5d0a72c65a5d122ac9e090a5f9bdd615ff157c49'


def module(name, filename):
    spec = importlib.util.spec_from_file_location(name, COMP / filename)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


v = module('audited_validated', 'validated_model.py')
s = module('audited_signal', 'indication.py')
checks = []
report = {'base_commit': BASE, 'seed': 20261006}


def check(name, holds, evidence):
    checks.append({'name': name, 'property_holds': bool(holds), 'evidence': evidence})


def rmap(values, prefix='w'):
    return {f'{prefix}{i:04d}': float(x) for i, x in enumerate(values)}


def asset(symbol, sleeve='broad_equity', price=17.0, score=80.0, confidence=1.0,
          returns=None, provider='synthetic'):
    names = {'broad_equity': 'Synthetic Total Stock Market ETF',
             'cash_like': 'Synthetic Overnight Money Market ETF',
             'government_bond': 'Synthetic Government Bond ETF',
             'aggregate_bond': 'Synthetic Aggregate Bond ETF',
             'sector_equity': 'Synthetic Technology ETF',
             'single_equity': 'Synthetic Single Company',
             'commodity': 'Synthetic Gold ETF', 'crypto': 'Synthetic Crypto'}
    category = {'single_equity': 'stock', 'crypto': 'crypto'}.get(sleeve, 'etf')
    return {'provider': provider, 'provider_id': symbol, 'symbol': symbol,
            'name': names[sleeve], 'category': category,
            'economic_sleeve': sleeve, 'market_score': score, 'score': score,
            'confidence': confidence, 'portfolio_price': price, 'price': price,
            'allocation_context_scale': 1.0,
            'risk_weekly_returns': rmap(np.zeros(156)) if returns is None else returns}


def weights_and_constraints(items, risk='medium', confidence=.45, cap=None, reserve=0):
    exact, meta = v.validated_exact_weights(items, risk)
    constrained, cmeta = v.apply_downward_weight_constraints(
        exact, risk=risk, min_confidence=confidence,
        max_candidate_fraction=cap, minimum_cash_reserve_fraction=reserve)
    return constrained


def projection(items, weighted, risk='medium', budget=1000, **kw):
    start = time.perf_counter()
    rows, meta = v.production_projection(items, weighted, risk, budget, **kw)
    return rows, meta, time.perf_counter() - start


def compact_rows(rows):
    return [{k: row.get(k) for k in ('symbol', 'confidence', 'market_score',
             'suggested_units', 'suggested_amount', 'allocation_eligible')}
            for row in rows]


def targeted():
    # Candidates removed by signal / confidence / user context must not return
    # through a different stage of the allocation pipeline.
    for reason in ('signal', 'confidence', 'context'):
        good = asset('APPROVED', price=10000)
        bad = asset('FILTERED', price=13,
                    score=40 if reason == 'signal' else 80,
                    confidence=.10 if reason == 'confidence' else 1.0)
        if reason == 'context':
            bad['allocation_context_scale'] = 0.0
        items = [good, bad]
        w = weights_and_constraints(items)
        rows, meta, _ = projection(items, w, budget=937.41, whole_units_only=True)
        before = {row['symbol']: weight for row, weight in w}
        resurrected = [row['symbol'] for row in rows
                       if row['suggested_amount'] > 0 and before.get(row['symbol'], 0) == 0]
        check('no_resurrection_' + reason, not resurrected,
              {'approved_weights': before, 'rows': compact_rows(rows),
               'resurrected': resurrected})

    # Combined risk must not make a no-history instrument risk-free simply
    # because another instrument supplies observations.
    good = asset('VALID_WHOLE', price=10000)
    bad = asset('NO_HISTORY', price=7, returns={})
    anchor = asset('VALID_FRACTIONAL', 'single_equity', price=3)
    items = [good, bad, anchor]
    w = weights_and_constraints(items)
    rows, meta, _ = projection(items, w, budget=937.41, whole_unit_categories=['etf'])
    check('no_history_cannot_borrow_other_instruments_observations',
          next(row for row in rows if row['symbol'] == 'NO_HISTORY')['suggested_amount'] == 0,
          {'approved_weights': {a['symbol']: z for a, z in w}, 'rows': compact_rows(rows),
           'post_risk': meta['post_discrete_risk']['post']})

    # Disjoint dates: pairwise correlation is unidentified, not zero.
    returns = np.tile([-.035, .035], 78)
    a = asset('A', returns=rmap(returns, 'old_'))
    b = asset('B', returns=rmap(returns, 'new_'))
    disjoint = v.risk_signature([(a, .24), (b, .24)])
    aligned_b = {**b, 'risk_weekly_returns': a['risk_weekly_returns']}
    aligned = v.risk_signature([(a, .24), (aligned_b, .24)])
    check('disjoint_history_is_rejected_instead_of_zero_filled',
          not v.within_risk_target(disjoint, 'low'),
          {'common_weeks': 0, 'union_signature': disjoint,
           'fully_aligned_same_shocks_signature': aligned,
           'union_accepted': v.within_risk_target(disjoint, 'low'),
           'aligned_accepted': v.within_risk_target(aligned, 'low')})

    # The function name does not establish a drawdown-loss ceiling.
    declining = asset('SMOOTH_LOSS', returns=rmap(np.full(156, -.002)))
    sig = v.risk_signature([(declining, 1.0)])
    check('risk_envelope_does_not_label_large_cumulative_loss_safe',
          not v.within_risk_target(sig, 'very_low'), sig)

    # Historical maps contain no as-of date validation in the pure allocator.
    ancient = asset('OLD_HISTORY', returns=rmap(np.full(156, .0001), '2001_'))
    w, meta = v.validated_exact_weights([ancient], 'very_low')
    check('stale_risk_history_is_rejected', not w,
          {'weights': [z for _, z in w], 'history_keys': '2001_*'})

    hypothetical = [
        {'category': 'etf', 'symbol': 'LEVERAGED', 'name': 'Synthetic 3X Leveraged S&P 500 ETF'},
        {'category': 'etf', 'symbol': 'INVERSE', 'name': 'Synthetic Inverse S&P 500 ETF'},
        {'category': 'etf', 'symbol': 'CREDIT', 'name': 'Synthetic Ultra Short High Yield Bond ETF'},
    ]
    classes = [{**a, **v.classify_economic_exposure(a)} for a in hypothetical]
    check('complex_structure_not_automatically_plain_safe_sleeve',
          all(not a['allocatable'] for a in classes), classes)

    # Positive controls: these existing restrictions genuinely work.
    for a in ({'category': 'fx', 'symbol': 'EURUSD=X'},
              {'category': 'index', 'symbol': '^GSPC'},
              {'category': 'commodity', 'symbol': 'GC=F'}):
        check('reference_blocked_' + a['symbol'],
              not v.classify_economic_exposure(a)['allocatable'],
              v.classify_economic_exposure(a))

    # A sparse flat quote series is not evidence of zero economic risk.
    sparse = [ {'ts': 946684800 + i * 21 * 86400, 'value': 100.0 + .01*i}
               for i in range(60)]
    weekly = v.weekly_return_map_from_points(sparse)
    check('multiweek_gaps_are_not_treated_as_regular_weekly_observations',
          len(weekly) < v.MIN_RISK_HISTORY_WEEKS,
          {'source_points': 60, 'days_between_quotes': 21,
           'accepted_weekly_returns': len(weekly),
           'note': 'Each adjacent return spans three calendar weeks.'})

    # Forced execution is not part of the test objective.
    expensive = asset('NO_FIT', price=100000)
    w = weights_and_constraints([expensive])
    rows, meta, _ = projection([expensive], w, budget=713.19, whole_units_only=True)
    check('genuinely_unaffordable_lot_remains_zero', meta['deployed'] == 0,
          {'deployed': meta['deployed'], 'cash': meta['cash_reserve']})


def randomized_contracts():
    rng = np.random.default_rng(20261006)
    problems = []; timings = []; count = 0
    counts = {'budget': 0, 'sleeve': 0, 'manual_cap': 0, 'risk': 0,
              'units_or_price': 0, 'resurrection': 0, 'nonfinite': 0}
    # Every risk profile, budgets spread over several orders of magnitude,
    # full-fractional, all-whole and per-category mixed modes.
    for risk in v.RISK_ORDER:
        for seed in range(20):
            budget = round(float(np.exp(rng.uniform(np.log(15), np.log(50000)))), 2)
            n = int(rng.integers(2, 5))
            common = rng.normal(0, .012, 156)
            items = []
            for i in range(n):
                sleeve = ('cash_like', 'government_bond', 'broad_equity', 'single_equity')[int(rng.integers(0, 4))]
                scale = {'cash_like': .04, 'government_bond': .6,
                         'broad_equity': 1.5, 'single_equity': 2.0}[sleeve]
                ret = .0001 + scale*(.65*common + rng.normal(0, .010, 156))
                items.append(asset(f'{risk}_{seed}_{i}', sleeve,
                             price=round(float(budget*rng.uniform(.04, .45)), 4),
                             score=float(rng.uniform(42, 90)),
                             confidence=float(rng.uniform(.30, 1)), returns=rmap(ret)))
            cap = (.20, .45, None)[seed % 3]
            reserve = (0, .10, .50, .95)[seed % 4]
            w = weights_and_constraints(items, risk, confidence=.45, cap=cap, reserve=reserve)
            active = {v._identity(a) for a, z in w if z > v.EPS}
            continuous, cm = v.constrained_cent_projection(items, w, risk, budget)
            continuous_sleeves = {}
            for a in continuous:
                k = a['economic_sleeve']
                continuous_sleeves[k] = continuous_sleeves.get(k, 0) + a['suggested_amount']
            for mode in ('fractional', 'whole', 'mixed'):
                kw = {'max_candidate_fraction': cap, 'max_candidate_fraction_is_hard': True,
                      'whole_units_only': mode == 'whole',
                      'whole_unit_categories': ['etf'] if mode == 'mixed' else []}
                rows, meta, elapsed = projection(items, w, risk, budget, **kw)
                count += 1; timings.append(elapsed)
                amounts = [a['suggested_amount'] for a in rows]
                total = sum(amounts)
                bad = []
                if not all(math.isfinite(z) and z >= 0 for z in amounts): bad.append('nonfinite')
                if total > budget*(1-reserve) + .011 or total > sum(a['suggested_amount'] for a in continuous) + .011: bad.append('budget')
                ss = {}
                for a in rows:
                    k = a['economic_sleeve']; ss[k] = ss.get(k, 0) + a['suggested_amount']
                if any(z > continuous_sleeves.get(k, 0) + .011 for k,z in ss.items()): bad.append('sleeve')
                if cap is not None and any(z > cap*budget + .011 for z in amounts): bad.append('manual_cap')
                if any(a['suggested_amount'] > 0 and v._identity(a) not in active for a in rows): bad.append('resurrection')
                for a in rows:
                    if a.get('whole_units_only') and (not float(a['suggested_units']).is_integer() or abs(a['suggested_amount'] - round(a['suggested_units']*a['portfolio_price'], 2)) > .011):
                        bad.append('units_or_price'); break
                sig = v.risk_signature([(a, a['suggested_amount']/budget) for a in rows if a['suggested_amount'] > 0])
                if total > 0 and not v.within_risk_target(sig, risk): bad.append('risk')
                for k in set(bad): counts[k] += 1
                if bad and len(problems) < 12:
                    problems.append({'risk': risk, 'seed_index': seed, 'budget': budget,
                                     'mode': mode, 'violations': bad,
                                     'rows': compact_rows(rows)})
    report['randomized_contracts'] = {'portfolio_cases': count, 'violations': counts,
                                     'seconds_p50': float(np.median(timings)),
                                     'seconds_max': max(timings), 'examples': problems}


def exact_lot_oracle():
    # Two correlated instruments: enumerate EVERY affordable unit-count pair.
    # This oracle is independent of the production beam's candidate pruning.
    rng = np.random.default_rng(73191)
    cases = []; worse = 0; false_zero = 0
    for seed in range(40):
        budget_cents = int(rng.integers(50000, 500000))
        budget = budget_cents/100
        prices = [int(rng.integers(90, 1800)) for _ in range(2)]
        magnitude = float(rng.uniform(.02, .22))
        ratio = float(rng.uniform(.55, 1.8))
        series = np.tile([-magnitude, magnitude], 78)
        items = [asset('ORACLE_A', price=prices[0]/100, returns=rmap(series)),
                 asset('ORACLE_B', price=prices[1]/100, returns=rmap(-series*ratio))]
        # Equal-risk opposing continuous positions with collective sleeve <=20%.
        wa = .20*ratio/(1+ratio); wb = .20/(1+ratio)
        w = [(items[0], wa), (items[1], wb)]
        cont, cm = v.constrained_cent_projection(items, w, 'very_low', budget)
        target = cm['projected_total_cents']
        ranges = [np.arange(target//p + 1, dtype=np.int64) for p in prices]
        ca, cb = np.meshgrid(*ranges, indexing='ij')
        aa = ca*prices[0]; bb = cb*prices[1]
        total = aa + bb
        exposure = np.abs((aa - ratio*bb)/budget_cents)
        # Alternating shocks have exact ES=magnitude*abs(net exposure).
        admissible = (total <= target) & (exposure*magnitude*math.sqrt(52) <= v.PORTFOLIO_VOL_TARGET['very_low']*v.RISK_TOLERANCE + 1e-14) & (exposure*magnitude <= v.PORTFOLIO_WEEKLY_ES95_TARGET['very_low']*v.RISK_TOLERANCE + 1e-14)
        best = int(np.max(np.where(admissible, total, 0)))
        rows, meta, elapsed = projection(items, w, 'very_low', budget, whole_units_only=True)
        found = int(round(meta['deployed']*100))
        if found < best: worse += 1
        if found == 0 and best > 0: false_zero += 1
        if found < best and len(cases) < 8:
            candidates = np.argwhere(admissible & (total == best))
            i,j = candidates[0]
            candidate_rows = [(items[0], int(aa[i,j])/budget_cents), (items[1], int(bb[i,j])/budget_cents)]
            assert v.within_risk_target(v.risk_signature(candidate_rows), 'very_low')
            cases.append({'seed_index': seed, 'budget': budget, 'prices': [p/100 for p in prices],
                          'continuous_target': target/100, 'production': found/100,
                          'exhaustive_best': best/100, 'exhaustive_units': [int(ca[i,j]),int(cb[i,j])],
                          'beam_retained_states': meta.get('whole_unit_search_states'),
                          'seconds': elapsed})
    report['independent_lot_oracle'] = {'cases': 40, 'suboptimal_cases': worse,
                                       'false_zero_cases': false_zero, 'examples': cases,
                                       'scope': 'Exact two-instrument finite lattices; not a general proof.'}


def monte_carlo_risk():
    rng = np.random.default_rng(110731)
    summaries = []
    for law in ('normal', 'student_t4', 'volatility_jump'):
        vols = []; eses = []; breach = 0; weights = []
        n = 250
        for i in range(n):
            if law == 'student_t4':
                train = rng.standard_t(4, 156)/math.sqrt(2)*.4/math.sqrt(52)
                future = rng.standard_t(4, 52)/math.sqrt(2)*.4/math.sqrt(52)
            else:
                train = rng.normal(0, .4/math.sqrt(52), 156)
                future = rng.normal(0, (.4 if law == 'normal' else 1.0)/math.sqrt(52), 52)
            a = asset('SIMULATED', returns=rmap(train))
            after, meta = v.scale_down_to_risk_contract([(a, .20)], 'very_low')
            w = after[0][1] if after else 0.0
            assert not after or v.within_risk_target(meta['post'], 'very_low')
            future_sig = v.risk_signature([(asset('SIMULATED', returns=rmap(future)), w)])
            outside = w > 0 and not v.within_risk_target(future_sig, 'very_low')
            breach += int(outside); weights.append(w)
            vols.append(future_sig['annualized_volatility_3y'] or 0)
            eses.append(future_sig['expected_shortfall_95_weekly_3y'] or 0)
        summaries.append({'law': law, 'independent_paths': n, 'training_weeks': 156,
                          'future_weeks': 52, 'future_empirical_envelope_breaches': breach,
                          'breach_fraction': breach/n, 'median_allocation_weight': float(np.median(weights)),
                          'median_future_volatility': float(np.median(vols)),
                          'p95_future_volatility': float(np.quantile(vols, .95)),
                          'interpretation': 'Simulation, not historical market results or a loss probability forecast.'})
    report['risk_generalization_simulation'] = summaries


def covariance_experiment():
    rng = np.random.default_rng(22013)
    sample_error=[]; shrink_error=[]; sample_risk=[]; shrink_risk=[]; equal_risk=[]
    for trial in range(250):
        n=15; t=80
        load = rng.normal(0, .012, (n,3))
        sigma = load@load.T + np.diag(rng.uniform(.006,.025,n)**2)
        returns = rng.multivariate_normal(np.zeros(n), sigma, size=t)
        sample = np.cov(returns, rowvar=False, ddof=0)
        shrunk = LedoitWolf().fit(returns).covariance_
        sample_error.append(float(np.linalg.norm(sample-sigma,'fro')))
        shrink_error.append(float(np.linalg.norm(shrunk-sigma,'fro')))
        # Unconstrained analytical GMV used ONLY to diagnose estimation error.
        # Not a proposed retail allocation or a long-only production solver.
        def risk(mat):
            invones=np.linalg.solve(mat,np.ones(n)); w=invones/invones.sum()
            return float(np.sqrt(w@sigma@w)*np.sqrt(52))
        sample_risk.append(risk(sample)); shrink_risk.append(risk(shrunk))
        w=np.ones(n)/n; equal_risk.append(float(np.sqrt(w@sigma@w)*np.sqrt(52)))
    report['shrinkage_prototype_simulation']={'independent_trials':250, 'assets':15,
        'training_observations':80,'sample_covariance_mean_frobenius_error':float(np.mean(sample_error)),
        'ledoit_wolf_mean_frobenius_error':float(np.mean(shrink_error)),
        'sample_gmv_mean_true_vol':float(np.mean(sample_risk)),
        'shrink_gmv_mean_true_vol':float(np.mean(shrink_risk)),
        'equal_weight_mean_true_vol':float(np.mean(equal_risk)),
        'scope':'Gaussian factor simulation; unconstrained GMV diagnostic, not a traded strategy.'}


def metadata():
    report['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in (COMP/'validated_model.py', COMP/'indication.py', COMP/'manager.py')}
    diff=subprocess.run(['git','diff','--exit-code',BASE,'--','custom_components','tests'],capture_output=True,text=True)
    report['production_and_existing_tests_unchanged']=diff.returncode==0
    report['fee_input_in_projection_signature'] = 'fee' in str(__import__('inspect').signature(v.production_projection))
    report['fees_demonstration']={'security_notional':98.98,'broker_fee':3,'cash_required':101.98,
                               'fee_percent_of_notional':3/98.98*100,
                               'note':'Arithmetic using the user-supplied receipt, not a current broker tariff.'}
    report['limitations']=['No guarantee of future safety or profit.',
                          'Synthetic contracts do not replace point-in-time market validation.',
                          'Adversarial cases are hypothetical instruments, not claims about named products.']


if __name__ == '__main__':
    start=time.perf_counter()
    targeted(); randomized_contracts(); exact_lot_oracle(); monte_carlo_risk(); covariance_experiment(); metadata()
    report['targeted_contract_checks']=checks
    report['targeted_contract_violations']=sum(not c['property_holds'] for c in checks)
    report['runtime_seconds']=time.perf_counter()-start
    (OUT/'scientific_safety.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('AUDIT_JSON_BEGIN')
    print(json.dumps(report,indent=2))
    print('AUDIT_JSON_END')
