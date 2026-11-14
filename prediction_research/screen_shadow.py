"""Observe richer ETF screens alongside production; never overwrite its ranking."""
from __future__ import annotations

import hashlib
import json
import statistics
from collections import Counter
from datetime import date, datetime, time, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .data import configured_data_dirs, find_cache
from .screen_factors import exposure_info, features_asof, load_contract, select_profile


def build_shadow(cfg, eligible, feature_date, top=10, cache=None, return_eligible=None):
    contract=load_contract()
    contract['top_k']=top
    cache={} if cache is None else cache
    asof=date.fromisoformat(feature_date)
    rows=[]; gaps=[]; past_returns={}; evidence={}
    flow_symbols={r['symbol'] for r in eligible}
    return_eligible=eligible if return_eligible is None else return_eligible
    return_symbols={r['symbol'] for r in return_eligible}
    combined_inputs={r['symbol']:r for r in return_eligible}
    combined_inputs.update({r['symbol']:r for r in eligible})
    for row in combined_inputs.values():
        sym=row['symbol']
        if sym not in cache:
            try: cache[sym]=find_cache(sym,configured_data_dirs(cfg))
            except (FileNotFoundError,ValueError): cache[sym]=None
        series=cache[sym]
        features=features_asof(series.bars,asof,contract['min_history_bars']) if series else None
        if features is None:
            gaps.append({'symbol':sym,'reason':'missing_stale_short_or_discontinuous_history'}); continue
        if features['adv20'] is None or features['adv20']<contract['min_adv20']:
            gaps.append({'symbol':sym,'reason':'missing_or_insufficient_historical_turnover'}); continue
        info=exposure_info(row,contract)
        combined={**row,**info,'features':features}
        rows.append(combined)
        history=[b for b in series.bars if b.trading_date<=asof][-61:]
        past_returns[sym]={b.trading_date:b.close/a.close-1 for a,b in zip(history,history[1:])}
        evidence[sym]={'features':features,'exposure':info,'history_sha256':series.sha256,
                       'price_provenance':series.provider_metadata or {'status':'unknown_legacy_basis'},
                       'contract_version':contract['version'],'research_only':True}
    flow_rows=[r for r in rows if r['symbol'] in flow_symbols]
    return_rows=[r for r in rows if r['symbol'] in return_symbols]
    profiles={}
    for method in ('current','momentum20','multi_factor','pullback','diversified','regime_risk'):
        plan=select_profile(flow_rows,method,contract,past_returns)
        profiles[method]={**{k:v for k,v in plan.items() if k!='selected'},
                          'selected':[{k:r.get(k) for k in ('symbol','name','exposure','scope','factor_score','factor_contributions')} for r in plan['selected']]}
    # Reproduce the existing grouping for the comparison baseline.
    selected=[]; groups=Counter()
    for row in sorted(flow_rows,key=lambda r:(-r['screen_score'],-r.get('amount',0))):
        group=row['screen_group']
        if groups[group]>=cfg['etf_market']['screen'].get('max_per_group',1): continue
        selected.append(row); groups[group]+=1
        if len(selected)==top: break
    profiles['current']['weights']={r['symbol']:1/top for r in selected}
    profiles['current']['selected']=[{k:r.get(k) for k in ('symbol','name','screen_score','screen_group')} for r in selected]
    from .return_screen import load_return_contract,select_return_candidates
    return_contract=load_return_contract()
    contract['return_challengers']=return_contract
    candidate_lists={}
    for strategy in return_contract['strategies']:
        if strategy['id'] not in return_contract['shadow_profiles']: continue
        name='return_'+strategy['id']
        plan=select_return_candidates(return_rows,strategy,contract)
        profiles[name]={**{k:v for k,v in plan.items() if k!='selected'},
                        'selected':[{k:r.get(k) for k in ('symbol','name','factor_score')} for r in plan['selected']],
                        'basis':'price_liquidity_channel','portfolio_top_k':strategy['top_k'],'rebalance_days':strategy['cadence']}
        coarse=select_return_candidates(return_rows,{**strategy,'top_k':top},contract)
        candidate_lists[name]={'candidate_limit':top,'allocation_top_k_is_separate':strategy['top_k'],
                              'selected':[{k:r.get(k) for k in ('symbol','name','factor_score','factor_contributions')} for r in coarse['selected']]}
    return {'schema_version':1,'feature_date':feature_date,'status':'shadow_only',
            'production_ranking_changed':False,'contract':contract,
            'coverage':{'eligible':len(eligible),'complete_factor_rows':len(flow_rows),
                        'excluded':sum(g['symbol'] in flow_symbols for g in gaps),
                        'price_channel_eligible':len(return_eligible),'price_channel_complete':len(return_rows),
                        'additional_price_channel_symbols':sorted({r['symbol'] for r in return_rows}-flow_symbols)},
            'gaps':gaps,'profiles':profiles,'return_candidate_lists':candidate_lists,'evidence_by_symbol':evidence,
            'limitations':['Missing price basis is marked unknown, never certified by smooth prices.',
                           'Missing historical amount is unavailable, not estimated from mixed volume units.',
                           'Premium/fees/official index membership remain unknown unless independently supplied.',
                           'No historical news score or calibrated probability is inferred.']}


def save_screen_shadow(cfg, screen, eligible, return_eligible=None):
    zone=ZoneInfo(cfg.get('timezone','Asia/Shanghai'))
    days={datetime.fromtimestamp(r['quote_epoch'],zone).date().isoformat() for r in screen['selected']}
    if len(days)!=1: raise ValueError('Shadow requires a single frozen quote date')
    payload=build_shadow(cfg,eligible,next(iter(days)),int(cfg.get('workflow',{}).get('agent_top_n',10)),return_eligible=return_eligible)
    payload['contract']['shadow_round_trip_cost_bps']=30
    payload.update(decision_at_utc=screen['decision_at_utc'],source_snapshot=screen.get('source_snapshot'),
                   created_at_utc=datetime.now(timezone.utc).isoformat(),screen_rule_hash=screen.get('rule_hash'))
    path=resolve_project_path(cfg,cfg['runs_dir'])/f"screen_shadow_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.json"
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(payload,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return {'path':str(path.resolve()),'status':'shadow_only','coverage':payload['coverage']}


def shadow_from_frozen_screen(cfg,screen):
    from .screening import _snapshot_path, select_etfs, build_atr_lookup
    if not screen.get('selected'):
        return {'status':'waiting_for_screen_candidates'}
    directory=resolve_project_path(cfg,cfg['etf_market']['snapshot_dir'])
    manifest=screen['source_snapshot']
    path=_snapshot_path(directory,manifest)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['sha256']:
        raise ValueError('Frozen snapshot hash mismatch for factor observation')
    source=json.loads(path.read_text(encoding='utf-8'))
    zone=ZoneInfo(cfg.get('timezone','Asia/Shanghai'))
    day=max(datetime.fromtimestamp(r['quote_epoch'],zone).date() for r in screen['selected'])
    decision=datetime.fromisoformat(screen['decision_at_utc'].replace('Z','+00:00'))
    rows=[r for r in source['records'] if r.get('quote_epoch') and
          datetime.fromtimestamp(r['quote_epoch'],zone).date()==day and r['quote_epoch']<=decision.timestamp()]
    from .return_screen import price_eligible_records
    atr=build_atr_lookup(cfg)
    eligible,_=select_etfs(rows,screen['rules'],None,atr_lookup=atr,as_of=day)
    price_rows=price_eligible_records(rows,screen['rules'],atr,day)
    return save_screen_shadow(cfg,screen,eligible,return_eligible=price_rows)


def candidate_factor_evidence(cfg, symbol, trade_date):
    if not cfg.get('factor_research',{}).get('inject_agent_context',False):
        return {'status':'shadow_only_not_injected','research_only':True}
    path=cfg.get('workflow',{}).get('factor_shadow_path')
    if not path: return {'status':'not_collected','research_only':True}
    payload=json.loads(Path(path).read_text(encoding='utf-8'))
    if payload.get('feature_date')!=trade_date:
        raise ValueError('Factor evidence date differs from candidate date')
    record=payload.get('evidence_by_symbol',{}).get(symbol)
    return {'status':'available','research_only':True,**record} if record else {'status':'unavailable','research_only':True}


def settle_factor_shadows(cfg):
    """Evaluate frozen weights only after the observation actually existed.

    These overlapping forward cohorts are not an executable portfolio NAV.
    Historical source screens cannot backdate a newly created observation.
    """
    from .settlement import decision_outcome
    from dataclasses import replace
    settled_at=datetime.now(timezone.utc)
    zone=ZoneInfo(cfg.get('timezone','Asia/Shanghai'))
    runs=resolve_project_path(cfg,cfg['runs_dir'])
    cache={}; cohorts=[]; sources={}; observations=[]; seen=set(); duplicates=[]
    for path in sorted(runs.glob('screen_shadow_*.json')):
        payload=json.loads(path.read_text(encoding='utf-8'))
        if payload.get('status')!='shadow_only': continue
        created=datetime.fromisoformat(payload['created_at_utc'].replace('Z','+00:00'))
        if created.utcoffset() is None: raise ValueError('Unzoned shadow creation time')
        observations.append((created,path,payload))
    for created,path,payload in sorted(observations,key=lambda item:(item[0],str(item[1]))):
        identity=(payload['feature_date'],payload.get('screen_rule_hash'),
                  hashlib.sha256(json.dumps(payload['contract'],sort_keys=True).encode()).hexdigest())
        if identity in seen:
            duplicates.append(str(path.resolve())); continue
        seen.add(identity)
        decision=datetime.fromisoformat(payload['decision_at_utc'].replace('Z','+00:00'))
        available=max(created,decision)
        if available.utcoffset() is None: raise ValueError('Unzoned shadow observation time')
        sources[str(path.resolve())]=hashlib.sha256(path.read_bytes()).hexdigest()
        profiles=payload['profiles']
        symbols={s for p in profiles.values() for s in p['weights']}
        for s in symbols:
            if s not in cache:
                try:
                    loaded=find_cache(s,configured_data_dirs(cfg))
                    cache[s]=replace(loaded,bars=tuple(b for b in loaded.bars if datetime.combine(b.trading_date,time(15),zone)<=settled_at))
                except (ValueError,FileNotFoundError): cache[s]=None
        for horizon in cfg.get('horizons',[5,20]):
            outcomes={s:decision_outcome(cfg,cache[s],payload['feature_date'],horizon,available.isoformat())
                      if cache[s] else None for s in symbols}
            missing=[s for s,value in outcomes.items() if value is None]
            row={'source':str(path.resolve()),'available_at_utc':available.isoformat(),
                 'horizon':horizon,'status':'paired' if symbols and not missing else 'waiting_for_outcomes',
                 'missing_symbols':missing,'outcomes':outcomes,
                 'price_sha256':{s:cache[s].sha256 for s in symbols if cache[s]}}
            if row['status']=='paired':
                bps=payload['contract'].get('shadow_round_trip_cost_bps',30)
                row['round_trip_cost_bps']=bps
                row['net_returns']={m:sum(w*(outcomes[s]['actual_return']-bps/10000) for s,w in p['weights'].items()) for m,p in profiles.items()}
                row['exposures']={m:sum(p['weights'].values()) for m,p in profiles.items()}
            cohorts.append(row)
    result={'schema_version':1,'status':'forward_observation_only','created_at_utc':datetime.now(timezone.utc).isoformat(),
            'sources':sources,'cohorts':cohorts,'paired_cohorts':sum(r['status']=='paired' for r in cohorts),
            'duplicate_observations_skipped':duplicates,
            'production_promotion_allowed':False,
            'limitations':['Frozen forward cohorts with overlap, not portfolio NAV; no automatic weight promotion.',
                           'Only the earliest observation per feature date, screen rule and factor contract is evaluated.',
                           'Entry is the first market open after both creation and decision timestamps.',
                           'Round-trip proxy cost is recorded in the observation contract; no lot/fill/corporate-action certification.']}
    runs.mkdir(parents=True,exist_ok=True)
    output=runs/f"factor_shadow_settlement_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}.json"
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return {'path':str(output.resolve()),'status':result['status'],'observations':len(sources),'paired_cohorts':result['paired_cohorts']}


def review_historical_factors(cfg, output):
    """Paired current-vs-challenger cohorts; preserve missing outcomes and cash."""
    import sqlite3
    from .screening import select_etfs,atr14_pct
    from .settlement import decision_outcome
    db=resolve_project_path(cfg,cfg['state_db'])
    with sqlite3.connect(db.as_uri()+'?mode=ro',uri=True) as con:
        con.row_factory=sqlite3.Row
        records=[dict(r) for r in con.execute('SELECT * FROM etf_flow_snapshots ORDER BY retrieved_at_utc')]
    zone=ZoneInfo(cfg.get('timezone','Asia/Shanghai')); snapshots={}; daily={}; cache={}
    for row in records:
        if not row.get('quote_epoch'): continue
        day=datetime.fromtimestamp(row['quote_epoch'],zone).date()
        available=datetime.fromisoformat(row['retrieved_at_utc'])
        if available<datetime.combine(day,time(15),zone): continue
        snap=snapshots.setdefault((day,row['snapshot_sha256']),{'day':day,'available':available,'rows':[]})
        snap['rows'].append(row); snap['available']=max(snap['available'],available)
    for snap in sorted(snapshots.values(),key=lambda s:s['available']): daily.setdefault(snap['day'],snap)
    cohorts=[]; coverage=[]
    for day,snap in sorted(daily.items()):
        def atr(symbol,asof):
            if symbol not in cache:
                try: cache[symbol]=find_cache(symbol,configured_data_dirs(cfg))
                except (FileNotFoundError,ValueError): cache[symbol]=None
            return atr14_pct(cache[symbol].bars,asof) if cache[symbol] else None
        eligible,_=select_etfs(snap['rows'],cfg['etf_market']['screen'],10,atr_lookup=atr,as_of=day)
        shadow=build_shadow(cfg,eligible,str(day),10,cache)
        coverage.append({'date':str(day),**shadow['coverage']})
        profiles=shadow['profiles']
        for horizon in cfg.get('horizons',[5,20]):
            symbols={s for p in profiles.values() for s in p['weights']}
            outcomes={s:decision_outcome(cfg,cache[s],str(day),horizon,snap['available'].isoformat()) for s in symbols}
            missing=[s for s,r in outcomes.items() if r is None]
            paired=not missing and bool(profiles['current']['weights'])
            result={'date':str(day),'horizon':horizon,'status':'paired' if paired else 'incomplete','missing':missing,
                    'selected':{m:p['weights'] for m,p in profiles.items()}}
            if paired:
                result['net_returns']={m:sum(w*(outcomes[s]['actual_return']-.003) for s,w in p['weights'].items()) for m,p in profiles.items()}
                result['exposures']={m:sum(p['weights'].values()) for m,p in profiles.items()}
            cohorts.append(result)
    summary={}
    for h in cfg.get('horizons',[5,20]):
        rows=[r for r in cohorts if r['horizon']==h and r['status']=='paired']
        summary[str(h)]={'paired_days':len(rows),'methods':{}}
        methods=sorted({m for r in cohorts for m in r.get('selected',{})})
        for method in methods:
            summary[str(h)]['methods'][method]={'mean_net_return':statistics.fmean(r['net_returns'][method] for r in rows) if rows else None,
                'mean_exposure':statistics.fmean(r['exposures'][method] for r in rows) if rows else None,
                'mean_excess_vs_current':statistics.fmean(r['net_returns'][method]-r['net_returns']['current'] for r in rows) if rows else None}
    result={'status':'exploratory_paired_cohorts','summary':summary,'coverage':coverage,'cohorts':cohorts,
            'production_promotion_allowed':False,
            'limitations':['Overlapping small-sample cohorts, not NAV or annualized returns.',
                'Shared history/liquidity-available subset, not original full-universe production results.',
                'Legacy mixed price provenance; this diagnostic is separate from the newly adjusted fixed-pool experiment.',
                'Cash earns zero; cost is 30bp round-trip on invested exposure. Lower losses may reflect lower exposure.']}
    Path(output).parent.mkdir(parents=True,exist_ok=True); Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return {'path':str(Path(output).resolve()),'summary':summary,'production_promotion_allowed':False}
