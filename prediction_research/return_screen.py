"""Return-focused hypotheses, separate from candidate count and allocation size."""
from __future__ import annotations

import json
import math
from pathlib import Path

from .screen_factors import percentiles, select_profile


def load_return_contract(path=None):
    return json.loads(Path(path or Path(__file__).parent/'config/return_experiment.json').read_text(encoding='utf-8'))


def price_eligible_records(records,rules,atr_lookup=None,as_of=None):
    """Same quote/liquidity exclusions, without requiring order-size flow data.

    Used only by the shadow return channel. Missing flow is never filled with
    zero or fed back into the existing production money-flow score.
    """
    from .flow_context import finite
    from .screening import money_market_exclusion
    from .taxonomy import classify_etf,market_scope
    eligible=[]
    for item in records:
        row={**item,**classify_etf(item['name'],etf_board=True,symbol=item.get('symbol')),'market_scope':market_scope(item['name'])}
        amount=finite(row.get('amount')); cap=finite(row.get('market_cap')); price=finite(row.get('price'))
        if (row['asset_class'] not in rules['asset_classes'] or amount is None or amount<=0 or amount<rules['min_amount']
                or cap is None or cap<=0 or cap<rules['min_market_cap'] or price is None or price<=0):
            continue
        if money_market_exclusion(row,rules,atr_lookup,as_of) is not None: continue
        eligible.append(row)
    return eligible


def select_return_candidates(rows, strategy, contract, incumbents=()):
    """No asset-class quotas, volatility sizing, leverage or unverified news score."""
    k=int(strategy['top_k'])
    if k<1: raise ValueError('top_k must be positive')
    kind=strategy['score']
    if kind in {'legacy_multifactor','equal_weight','benchmark_hold'}:
        return select_profile(rows,{'legacy_multifactor':'multi_factor'}.get(kind,kind),{**contract,'top_k':k})
    eligible=[{**r,'factor_contributions':{}} for r in rows if r.get('features') and
              r['features'].get('adv20') is not None and r['features']['adv20']>=contract['min_adv20']]
    if kind=='global_multifactor':
        for r in eligible: r['factor_score']=0.0
        for factor,weight in contract['profiles']['multi_factor'].items():
            values=[r['features'][factor] for r in eligible]
            if any(v is None or not math.isfinite(v) for v in values): raise ValueError('Missing factor')
            for r,p in zip(eligible,percentiles(values)):
                r['factor_contributions'][factor]=weight*p
                r['factor_score']+=weight*p
    else:
        for r in eligible:
            f=r['features']
            if kind in {'momentum20','momentum60'}: score=f[kind]
            elif kind=='blend20_60': score=(f['momentum20']+f['momentum60'])/2
            elif kind=='momentum20_skip5': score=(1+f['momentum20'])/(1+f['momentum5'])-1
            else: raise ValueError(f'Unknown return score: {kind}')
            if not math.isfinite(score): raise ValueError('Invalid return score')
            r['factor_score']=score; r['factor_contributions'][kind]=score
    if strategy.get('positive_only'):
        eligible=[r for r in eligible if r['factor_score']>0]
    ranked=sorted(eligible,key=lambda r:(-r['factor_score'],r['symbol']))
    retained=[r for r in ranked[:k+strategy['retention_buffer']] if r['symbol'] in incumbents] if strategy.get('retention_buffer') else []
    selected=[]; groups=set(); visited=set(); rejected=[]
    for r in retained+ranked:
        if r['symbol'] in visited: continue
        visited.add(r['symbol']); group=(r['scope'],r['exposure'])
        if group in groups:
            rejected.append({'symbol':r['symbol'],'reason':'duplicate_exposure'}); continue
        groups.add(group); selected.append(r)
        if len(selected)==k: break
    return {'selected':selected,'weights':{r['symbol']:1/k for r in selected},'rejected':rejected,
            'ranked_symbols':[r['symbol'] for r in ranked], 'breadth':None}
