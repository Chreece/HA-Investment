"""Retrospective diagnostic, NOT a point-in-time untouched strategy validation.

Frozen universe and methods, no hyperparameter search. Prices are contemporary
adjusted-history downloads; survivors, revisions, idealized fills and selecting
a 2026 model for past years limit any inference. Failed downloads are reported,
never replaced by invented or synthetic market observations.
"""
from __future__ import annotations
import datetime as dt
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import sys
import time
import urllib.parse
import urllib.request

import numpy as np
from scipy.stats import norm
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler

ROOT=Path(__file__).resolve().parents[1]
COMP=ROOT/'custom_components/investment'
OUT=ROOT/'audit-results'; OUT.mkdir(exist_ok=True)


def load(name, path):
    spec=importlib.util.spec_from_file_location(name, COMP/path)
    m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
    return m


v=load('market_validated_audit','validated_model.py')
s=load('market_signal_audit','indication.py')
UNIVERSE=[('IEGE.AS','iShares Euro Govt Bond 0-1yr UCITS ETF'),
          ('IBGS.AS','iShares Euro Government Bond 1-3yr UCITS ETF'),
          ('EUNL.DE','iShares Core MSCI World UCITS ETF'),
          ('SXR8.DE','iShares Core S&P 500 UCITS ETF'),
          ('XEON.DE','Xtrackers EUR Overnight Rate Swap UCITS ETF')]
report={'base_commit':'5d0a72c65a5d122ac9e090a5f9bdd615ff157c49',
        'data_requested_start':'2010-01-01','evaluation_cutoff':'2025-12-31',
        'universe_prespecified':[a for a,_ in UNIVERSE],
        'scope':'Retrospective current-survivor EUR UCITS diagnostic; not independent future validation.'}
provenance=[]


def fetch(symbol):
    params={'period1':int(dt.datetime(2010,1,1,tzinfo=dt.timezone.utc).timestamp()),
            'period2':int(dt.datetime(2026,1,1,tzinfo=dt.timezone.utc).timestamp()),
            'interval':'1wk','events':'div,splits','includeAdjustedClose':'true'}
    error=None
    for host in ('query1.finance.yahoo.com','query2.finance.yahoo.com'):
        url=f'https://{host}/v8/finance/chart/{urllib.parse.quote(symbol,safe="")}?'+urllib.parse.urlencode(params)
        try:
            req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 scientific-reproducibility-audit'})
            with urllib.request.urlopen(req,timeout=20) as response:
                raw=response.read()
            j=json.loads(raw); item=j['chart']['result'][0]
            if item.get('meta',{}).get('currency') != 'EUR':
                raise ValueError('Currency is not EUR; no silent FX substitution')
            if item['meta'].get('symbol','').upper()!=symbol.upper():
                raise ValueError('Provider did not return the requested exact ticker')
            values=item['indicators']['adjclose'][0]['adjclose']
            stamps=item['timestamp']; rows={}
            for stamp, val in zip(stamps,values):
                when=dt.datetime.fromtimestamp(stamp,dt.timezone.utc).date()
                if when>=dt.date(2026,1,1) or val is None or not math.isfinite(float(val)) or val<=0:
                    continue
                iso=when.isocalendar(); key=f'{iso.year:04d}-W{iso.week:02d}'
                rows[key]=float(val)
            (OUT/f'{symbol}-source.json').write_bytes(raw)
            provenance.append({'symbol':symbol,'url':url,'sha256':hashlib.sha256(raw).hexdigest(),
                               'weekly_points':len(rows),'first':min(rows),'last':max(rows),
                               'currency':item['meta']['currency'],
                               'long_name':item['meta'].get('longName'),
                               'downloaded_at_utc':dt.datetime.now(dt.timezone.utc).isoformat()})
            return rows
        except Exception as exc:
            error=f'{type(exc).__name__}: {exc}'
    raise RuntimeError(error)


def metrics(returns, periods_per_year=52):
    a=np.asarray(returns,float)
    if not len(a): return None
    equity=np.cumprod(1+a); peaks=np.maximum.accumulate(np.r_[1.,equity])[1:]
    years=len(a)/periods_per_year
    return {'observations':len(a),'cagr':float(equity[-1]**(1/years)-1),
            'annualized_volatility':float(a.std()*math.sqrt(periods_per_year)),
            'max_drawdown':float(np.min(equity/peaks-1)),
            'terminal_multiple':float(equity[-1]),
            'sharpe_vs_zero_nominal_cash':float(a.mean()/a.std()*math.sqrt(periods_per_year)) if a.std()>0 else None}


def model_weight(prices, keys, symbols, names, t, risk, benchmark=False):
    candidates=[]
    for j,symbol in enumerate(symbols):
        series=prices[max(0,t-260):t+1,j]
        risk_prices=prices[t-156:t+1,j]
        risk_ret=risk_prices[1:]/risk_prices[:-1]-1
        history={key:float(val) for key,val in zip(keys[t-155:t+1],risk_ret)}
        a={'provider':'yahoo','provider_id':symbol,'symbol':symbol,
           'name':names[j],'category':'etf','portfolio_price':float(prices[t,j])}
        scaffold=s.analyze_prices(series.tolist(),current_price=float(series[-1]),category='etf',
                                 risk_tolerance=v.SIGNAL_SCAFFOLD_RISK,horizon='very_long',
                                 strategy='adaptive',holding_weight=0,category_weight=0,
                                 portfolio_overlap=0,overlap_policy='allow').as_dict()
        candidate=v.prepare_scored_candidate({**a,**scaffold},a,history)
        candidates.append(candidate)
    if not benchmark:
        weighted,_=v.validated_exact_weights(candidates,risk)
        weighted,_=v.apply_downward_weight_constraints(weighted,risk=risk,
                        min_confidence=.45,max_candidate_fraction=.45,minimum_cash_reserve_fraction=0)
    else:
        groups={}
        for c in candidates:
            if c['risk_history_eligible']:
                groups.setdefault(c['economic_sleeve'],[]).append(c)
        weighted=[]
        for sleeve,cs in groups.items():
            cap=v.ECONOMIC_SLEEVE_CAPS[risk].get(sleeve,0)
            for c in cs:
                if cap>0: weighted.append((c,min(.45,cap/len(cs))))
        total=sum(w for _,w in weighted)
        if total>1:weighted=[(c,w/total) for c,w in weighted]
        weighted,_=v.scale_down_to_risk_contract(weighted,risk)
    by={c['symbol']:w for c,w in weighted}
    return np.array([by.get(symbol,0) for symbol in symbols])


def backtest(prices,keys,symbols,names):
    output=[]; start=260
    for risk in ('very_low','medium','very_high'):
        for bm in (False,True):
            gross=[]; net=[]; w=np.zeros(len(symbols)); cash=1.; wealth=10000.
            fees=0.; trades=0; cash_samples=[]
            for t in range(start,len(keys)-1):
                cost=0
                if (t-start)%4==0:
                    target=model_weight(prices,keys,symbols,names,t,risk,bm)
                    changes=np.abs(target-w)
                    ntrades=int(np.sum(changes*wealth>1.0))
                    # Illustrative receipt-sized fee, not a broker tariff.
                    cost=min(wealth,3*ntrades+.001*float(changes.sum())*wealth)
                    fees+=cost;trades+=ntrades
                    w=target;cash=max(0.,1-float(w.sum()))
                ret=prices[t+1]/prices[t]-1
                r=float(w@ret); gross.append(r)
                nr=(1-cost/wealth)*(1+r)-1 if wealth>0 else 0.
                net.append(nr);wealth*=1+nr
                w=w*(1+ret)/(1+r); cash=cash/(1+r)
                cash_samples.append(cash)
            output.append({'risk':risk,'method':'same-caps-risk-no-signal-baseline' if bm else 'native-continuous-score-model',
                           'start':keys[start], 'end':keys[-1], 'gross':metrics(gross),
                           'illustrative_net':metrics(net),'initial_wealth':10000,
                           'total_fees':fees,'trade_count':trades,
                           'mean_cash_weight':float(np.mean(cash_samples))})
    return output


def forecasts(prices,keys,symbols):
    # One-week ahead return prediction, never price targets. At decision t,
    # training label r[t] is already known, while r[t+1] is held out.
    r=prices[1:]/prices[:-1]-1
    mse_by_week={}; absolute=[]; total_n=0; pooled={'ridge':[],'mean':[],'zero':[]}
    brier={'ridge':[],'mean':[]}; coverage=[]; per_asset=[]
    for j,symbol in enumerate(symbols):
        rr=r[:,j]
        x=np.full((len(rr),4),np.nan)
        for t in range(52,len(rr)):
            x[t]=[np.prod(1+rr[t-3:t+1])-1,np.prod(1+rr[t-12:t+1])-1,
                  np.prod(1+rr[t-51:t+1])-1,np.std(rr[t-12:t+1])]
        errors={'ridge':[],'mean':[],'zero':[]}
        for t in range(260,len(rr)-1):
            ids=np.arange(t-156,t)
            model=make_pipeline(StandardScaler(),Ridge(alpha=10.0))
            model.fit(x[ids],rr[ids+1])
            pred=float(model.predict(x[t:t+1])[0]); actual=float(rr[t+1])
            train=rr[t-155:t+1]; mu=float(train.mean()); sd=float(train.std())
            vals={'ridge':(actual-pred)**2,'mean':(actual-mu)**2,'zero':actual**2}
            for k,z in vals.items(): errors[k].append(z); pooled[k].append(z)
            mse_by_week.setdefault(keys[t+1],[]).append(vals['mean']-vals['ridge'])
            label=float(actual>0)
            for k,p in [('ridge',pred),('mean',mu)]:
                probability=float(norm.cdf(p/max(sd,1e-12)))
                brier[k].append((label-probability)**2)
            coverage.append(abs(actual-mu)<=norm.ppf(.95)*sd)
            total_n+=1
        per_asset.append({'symbol':symbol,'forecasts':len(errors['ridge']),
                          'out_of_sample_r2_vs_rolling_mean':1-np.mean(errors['ridge'])/np.mean(errors['mean'])})
    weeks=sorted(mse_by_week); diffs=np.array([np.mean(mse_by_week[k]) for k in weeks])
    rng=np.random.default_rng(6102026); estimates=[];block=13
    if len(diffs):
        for _ in range(2000):
            starts=rng.integers(0,len(diffs),int(np.ceil(len(diffs)/block)))
            idx=np.concatenate([(z+np.arange(block))%len(diffs) for z in starts])[:len(diffs)]
            estimates.append(float(diffs[idx].mean()))
    return {'target':'next-week adjusted return','model':'rolling 156-week standardized ridge, alpha=10 fixed before results',
            'features':'4/13/52-week trailing returns and 13-week realized volatility',
            'forecast_count':total_n,'calendar_weeks':len(weeks),
            'mse':{k:float(np.mean(z)) for k,z in pooled.items()},
            'out_of_sample_r2_vs_rolling_mean':float(1-np.mean(pooled['ridge'])/np.mean(pooled['mean'])),
            'brier_positive_return':{k:float(np.mean(z)) for k,z in brier.items()},
            'normal_90pct_interval_empirical_coverage':float(np.mean(coverage)),
            'mean_loss_reduction':float(np.mean(diffs)),
            'paired_week_block_bootstrap_95pct_interval':np.quantile(estimates,[.025,.975]).tolist() if estimates else None,
            'bootstrap_block_weeks':block,'bootstrap_resamples':2000,'by_asset':per_asset}


def main():
    data={}; errors=[]; names={}
    for symbol,name in UNIVERSE:
        try:
            data[symbol]=fetch(symbol); names[symbol]=name
        except Exception as e:
            errors.append({'symbol':symbol,'error':str(e)})
    report['data_provenance']=provenance;report['download_errors']=errors
    if len(data)<3:
        report['status']='insufficient_downloaded_data_no_market_performance_claim'
        return
    symbols=list(data)
    keys=sorted(set.intersection(*(set(z) for z in data.values())))
    # Do not turn an interior missing calendar week into a one-week return.
    def date(key):
        y,w=key.split('-W');return dt.date.fromisocalendar(int(y),int(w),1)
    gaps=[(a,b) for a,b in zip(keys,keys[1:]) if (date(b)-date(a)).days!=7]
    report['common_calendar_gaps']=gaps
    if gaps or len(keys)<400:
        report['status']='calendar_or_history_gate_blocked_no_market_performance_claim'
        report['common_weeks']=len(keys)
        return
    prices=np.array([[data[s][k] for s in symbols] for k in keys],float)
    report['used_symbols']=symbols;report['common_weeks']=len(keys)
    report['status']='retrospective_diagnostic_completed'
    report['backtests']=backtest(prices,keys,symbols,[names[s] for s in symbols])
    report['forecast_experiment']=forecasts(prices,keys,symbols)
    report['limitations']=['Chosen surviving funds, not a point-in-time historical discovery universe.',
        'Current corporate-action-adjusted history may be revised; not an archived vintage.',
        'Model written in 2026 evaluated retrospectively; not untouched model-selection holdout.',
        'Continuous weights diagnostic only; whole-lot logic audited separately.',
        'Four-week rebalancing, zero nominal return on uninvested cash.',
        'Net example uses EUR 3 per material trade plus 10bp turnover, no tax or slippage model.',
        'No claim that measured past performance predicts future returns.']


if __name__=='__main__':
    begin=time.perf_counter()
    try: main()
    except Exception as e:
        report['status']='experiment_error';report['error']=f'{type(e).__name__}: {e}'
        import traceback
        report['traceback']=traceback.format_exc()
    report['seconds']=time.perf_counter()-begin
    (OUT/'retrospective_market.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print('MARKET_AUDIT_JSON_BEGIN');print(json.dumps(report,indent=2));print('MARKET_AUDIT_JSON_END')
