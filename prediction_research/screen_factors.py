"""Shared causal ETF factor calculation and fixed challenger strategies.

Live shadow screens and offline portfolio experiments use these same functions.
Missing liquidity/premium evidence never becomes a zero-risk observation.
"""
from __future__ import annotations

import json
import math
import statistics
from collections import Counter
from pathlib import Path

from .taxonomy import _strip_fund_company, classify_etf, market_scope


def load_contract(path=None):
    return json.loads(Path(path or Path(__file__).parent/'config/screen_experiment.json').read_text(encoding='utf-8'))


def feature_window(bars, min_history=126):
    """Bars must be chronologically ordered and already truncated at decision time."""
    if len(bars) < min_history:
        return None
    window = bars[-min_history:]
    closes = [b.close for b in window]
    if any(not math.isfinite(v) or v <= 0 for v in closes):
        return None
    returns = [b/a-1 for a,b in zip(closes,closes[1:])]
    if any(abs(r) > .22 for r in returns):
        return None
    daily = returns[-20:]
    adv20 = statistics.fmean(b.amount for b in window[-20:]) if all(math.isfinite(b.amount) and b.amount>0 for b in window[-20:]) else None
    adv60 = statistics.fmean(b.amount for b in window[-60:]) if all(math.isfinite(b.amount) and b.amount>0 for b in window[-60:]) else None
    vol20 = statistics.pstdev(daily)*math.sqrt(252)
    downside = math.sqrt(statistics.fmean(min(r,0)**2 for r in daily))*math.sqrt(252)
    ma20,ma60,ma120 = (statistics.fmean(closes[-n:]) for n in (20,60,120))
    sd20=statistics.pstdev(closes[-20:])
    path=sum(abs(b-a) for a,b in zip(closes[-21:],closes[-20:]))
    true_ranges=[max(b.high-b.low,abs(b.high-a.close),abs(b.low-a.close)) for a,b in zip(window[-15:],window[-14:])]
    return {'momentum5':closes[-1]/closes[-6]-1,'momentum20':closes[-1]/closes[-21]-1,
            'momentum60':closes[-1]/closes[-61]-1,'momentum120':closes[-1]/closes[-121]-1,
            'trend60':closes[-1]/ma60-1,'trend120':closes[-1]/ma120-1,
            'volatility20':vol20,'downside20':downside,
            'low_volatility20':-vol20,'low_downside20':-downside,
            'drawdown60':closes[-1]/max(closes[-60:])-1,
            'drawdown120':closes[-1]/max(closes[-120:])-1,
            'efficiency20':abs(closes[-1]-closes[-21])/path if path else 0.0,
            'pullback5':1-closes[-1]/closes[-6],
            'overheat_z20':(closes[-1]-ma20)/sd20 if sd20 else 0.0,
            'atr14_pct':statistics.fmean(true_ranges)/closes[-1]*100,
            'adv20':adv20,'adv60':adv60,'log_adv20':math.log10(adv20) if adv20 else None,
            'amount_ratio20':window[-1].amount/adv20 if adv20 else None,
            'history_bars':len(bars),'last_date':str(window[-1].trading_date)}


def features_asof(bars, asof, min_history=126):
    valid=[b for b in bars if b.trading_date<=asof]
    if not valid or valid[-1].trading_date!=asof:
        return None
    return feature_window(valid,min_history)


def exposure_info(row, contract):
    registered=next((a for a in contract['assets'] if a['symbol']==row['symbol']),None)
    if registered:
        return {**registered,'mapping_basis':'explicit_research_registry'}
    theme=_strip_fund_company(row.get('name',''))
    aliases={'沪深300':'CSI300','300':'CSI300','中证500':'CSI500','500':'CSI500',
             '中证A500':'CSIA500','A500':'CSIA500','中证A50':'CSIA50','A50':'CSIA50',
             '上证50':'SSE50','科创50':'STAR50','创业板':'CHINEXT','创业板指':'CHINEXT'}
    typed=classify_etf(row.get('name',''),etf_board=True,symbol=row.get('symbol'))
    scope='CROSS_BORDER_UNVERIFIED' if market_scope(row.get('name',''))=='cross_border' else 'CN'
    group=aliases.get(theme,'NAME:'+theme)
    return {**typed,'symbol':row['symbol'],'name':row.get('name'),'scope':scope,
            'exposure':group,'mapping_basis':'name_heuristic_unverified'}


def percentiles(values):
    if len(values)<2: return [.5]*len(values)
    counts=Counter(values); ranks={}; offset=0
    for value in sorted(counts):
        ranks[value]=(offset+(counts[value]-1)/2)/(len(values)-1)
        offset+=counts[value]
    return [ranks[v] for v in values]


def rank_rows(rows, profile, contract):
    rows=[{**r,'factor_contributions':{}} for r in rows]
    if profile in {'current','liquidity','momentum20'}:
        for row in rows:
            row['factor_score']=(row.get('screen_score',0) if profile=='current' else
                                 row['features']['adv20'] if profile=='liquidity' else row['features']['momentum20'])
        return sorted(rows,key=lambda r:(-r['factor_score'],r['symbol']))
    weights=contract['profiles']['pullback' if profile=='pullback' else 'multi_factor']
    cohorts={}
    for row in rows:
        cohorts.setdefault((row['asset_class'],row['scope']),[]).append(row)
    for cohort in cohorts.values():
        for row in cohort: row['factor_score']=0.0
        for factor,weight in weights.items():
            # Eligibility requires these fields; no missing-factor bonus.
            values=[r['features'][factor] for r in cohort]
            if any(v is None or not math.isfinite(v) for v in values):
                raise ValueError('ranking received an unavailable factor')
            for row,rank in zip(cohort,percentiles(values)):
                row['factor_contributions'][factor]=rank*weight
                row['factor_score']+=rank*weight
    return sorted(rows,key=lambda r:(-r['factor_score'],r['symbol']))


def correlation(left,right):
    common=sorted(set(left)&set(right))[-60:]
    if len(common)<60: return None
    a,b=[left[d] for d in common],[right[d] for d in common]
    if statistics.pstdev(a)==0 or statistics.pstdev(b)==0: return None
    return statistics.correlation(a,b)


def select_profile(rows, profile, contract, returns=None, incumbents=()):
    k=int(contract['top_k'])
    eligible=[r for r in rows if r['features'] and r['features']['adv20'] is not None
              and r['features']['adv20']>=contract['min_adv20']]
    breadth_rows=[r for r in eligible if r['asset_class']=='equity' and r['scope']=='CN']
    breadth=statistics.fmean(r['features']['trend60']>0 for r in breadth_rows) if breadth_rows else None
    if profile=='cash': return {'weights':{},'selected':[],'breadth':breadth,'rejected':[]}
    if profile=='benchmark_hold':
        eligible=[r for r in eligible if r['symbol']=='510300']
    if profile in {'pullback','diversified','regime_risk'}:
        eligible=[r for r in eligible if r['features']['trend60']>0 and r['features']['momentum60']>0]
    ranked=rank_rows(eligible,'multi_factor' if profile in {'equal_weight','benchmark_hold'} else profile,contract)
    retained=[]
    if profile in {'diversified','regime_risk'}:
        retained=[r for r in ranked[:k+contract['retention_rank_buffer']] if r['symbol'] in incumbents]
    selected=[]; seen=set(); groups=set(); rejected=[]
    for row in retained+ranked:
        sym=row['symbol']
        if sym in seen: continue
        seen.add(sym)
        group=(row['scope'],row['exposure'])
        if group in groups:
            rejected.append({'symbol':sym,'reason':'duplicate_exposure'}); continue
        if profile in {'diversified','regime_risk'} and returns:
            overlaps=[other['symbol'] for other in selected if (c:=correlation(returns.get(sym,{}),returns.get(other['symbol'],{}))) is not None and c>contract['correlation_cap']]
            if overlaps:
                rejected.append({'symbol':sym,'reason':'high_correlation','with':overlaps}); continue
        groups.add(group); selected.append(row)
        if profile not in {'equal_weight'} and len(selected)>=k: break
    divisor=len(selected) if profile in {'equal_weight','benchmark_hold'} else k
    weights={r['symbol']:1/divisor for r in selected} if divisor else {}
    scale=1.0
    if profile=='regime_risk':
        if breadth is None:
            weights={}
        elif breadth<contract['risk_off_breadth']:
            weights={r['symbol']:weights[r['symbol']]*(contract['risk_off_equity_multiplier'] if r['asset_class']=='equity' else 1) for r in selected}
        # Covariance uses only return observations supplied at this decision.
        if weights and returns:
            dates=sorted(set.intersection(*(set(returns.get(sym,{})) for sym in weights)))[-60:]
            if len(dates)>=60:
                portfolio=[sum(weight*returns[sym][day] for sym,weight in weights.items()) for day in dates]
                risk=statistics.pstdev(portfolio)*math.sqrt(252)
                scale=min(1.0,contract['target_annual_volatility']/risk) if risk else 1.0
                weights={sym:value*scale for sym,value in weights.items()}
    return {'weights':weights,'selected':selected,'breadth':breadth,'volatility_scale':scale,'rejected':rejected}
