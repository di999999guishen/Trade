"""Predeclared return-first hypotheses and causal yearly model selection replay.

Uses the existing independently reconciled cash execution engine. Historical
model selection is explicitly separate from untouched forward validation.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import math
import random
import shutil
import statistics
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .screen_factors import load_contract
from .screen_portfolio import Portfolio, audit_ledger, digest, metrics, prepare_panel, write_csv, write_json
from .return_screen import load_return_contract, select_return_candidates


def cache_panel(calendar, prices, features, base):
    """Cache shared daily rows once; no scan of future history inside a signal."""
    assets={a['symbol']:a for a in base['assets']}
    return {'calendar':calendar,'prices':prices,'assets':assets,
            'rows':{d:[{**a,'features':features[s][d]} for s,a in assets.items()] for d in calendar},
            'opens':{d:{s:prices[s][d].open for s in assets} for d in calendar},
            'closes':{d:{s:prices[s][d].close for s in assets} for d in calendar},
            'adv':{d:{s:features[s][d]['adv20'] if features[s][d] else None for s in assets} for d in calendar}}


def summarize(nav, fills, base):
    result={'full':metrics(nav,base['initial_cash'],fills),'periods':{},'years':{}}
    for name,(start,end) in base['periods'].items():
        rows=[r for r in nav if start<=r['date']<=end]
        before=[r for r in nav if r['date']<start]
        result['periods'][name]=metrics(rows,before[-1]['nav'] if before else base['initial_cash'],
            [f for f in fills if start<=f['date']<=end],date.fromisoformat(before[-1]['date']) if before else None)
    for year in sorted({r['date'][:4] for r in nav}):
        rows=[r for r in nav if r['date'].startswith(year)]
        before=[r for r in nav if r['date'][:4]<year]
        result['years'][year]=metrics(rows,before[-1]['nav'] if before else base['initial_cash'],
            [f for f in fills if f['date'].startswith(year)],date.fromisoformat(before[-1]['date']) if before else None)
    return result


def simulate_return(panel, base, strategy, bps, start_index=0, schedule=None):
    calendar=panel['calendar']; portfolio=Portfolio(base['initial_cash'])
    nav=[]; targets=[]; blocks=[]; pending=None; active=strategy
    schedule={} if schedule is None else schedule
    limits={s:a['limit_pct'] for s,a in panel['assets'].items()}
    for idx in range(start_index,len(calendar)):
        day=calendar[idx]; opens=panel['opens'][day]; closes=panel['closes'][day]
        if pending is not None:
            signal_day,target,old_strategy=pending
            reasons=portfolio.rebalance(target,opens,panel['closes'][signal_day],panel['adv'][signal_day],limits,
                bps,base['max_participation_lagged_adv'],old_strategy.get('weight_buffer',0),day,signal_day)
            blocks.extend({'date':str(day),**r} for r in reasons); pending=None
        value=portfolio.value(closes)
        commodity=sum(q*closes[s] for s,q in portfolio.units.items() if panel['assets'][s]['asset_class']=='commodity')/value
        nav.append({'date':str(day),'nav':value,'cash':portfolio.cash,'exposure':1-portfolio.cash/value,
                    'holdings_count':len(portfolio.units),'commodity_exposure':commodity})
        switched=day in schedule
        if switched: active=schedule[day]
        scheduled=idx==start_index or switched or (active['score']!='benchmark_hold' and idx%active['cadence']==0)
        if not scheduled or idx==len(calendar)-1: continue
        selected=select_return_candidates(panel['rows'][day],active,base,set(portfolio.units))
        target=selected['weights']; pending=(day,target,active)
        targets.append({'signal_date':str(day),'execute_not_before':str(calendar[idx+1]),
                        'strategy_id':active['id'],'weights':target,'rejected':selected['rejected'],
                        'ranked_symbols':selected.get('ranked_symbols',[r['symbol'] for r in selected['selected']])})
    result=summarize(nav,portfolio.fills,base)
    result.update(block_counts=dict(Counter(r['reason'] for r in blocks)),
                  ledger=audit_ledger(nav,portfolio.fills,panel['prices'],base['initial_cash']),
                  mean_commodity_exposure=statistics.fmean(r['commodity_exposure'] for r in nav))
    return result,nav,portfolio.fills,targets


def choose_from_history(histories, candidates, cutoff, sessions):
    """Choice reads closed NAV observations through cutoff, never the next year."""
    scores=[]
    for strategy in candidates:
        rows=[r for r in histories[strategy['id']] if r['date']<=str(cutoff)]
        if len(rows)<sessions+1 or rows[-1]['date']!=str(cutoff):
            raise ValueError('Insufficient or asynchronous training observations')
        window=rows[-sessions-1:]
        score=math.log(window[-1]['nav']/window[0]['nav'])
        peak=window[0]['nav']; dd=0.0
        for r in window:
            peak=max(peak,r['nav']); dd=min(dd,r['nav']/peak-1)
        scores.append({'strategy_id':strategy['id'],'training_start':window[0]['date'],
                       'training_end':window[-1]['date'],'closed_return_intervals':sessions,
                       'net_log_return':score,'net_return':math.expm1(score),'max_drawdown':dd})
    scores.sort(key=lambda r:(-r['net_log_return'],r['strategy_id']))
    return scores[0]['strategy_id'],scores


def annual_schedule(calendar,histories,contract):
    candidates=[s for s in contract['strategies'] if s['selectable']]
    byid={s['id']:s for s in candidates}; schedule={}; records=[]
    for idx,day in enumerate(calendar[:-1]):
        following=calendar[idx+1]
        if following.year==day.year or str(following)<contract['evaluation_start']: continue
        chosen,scores=choose_from_history(histories,candidates,day,contract['train_sessions'])
        schedule[day]=byid[chosen]
        records.append({'decision_date':str(day),'first_possible_fill':str(following),'strategy_id':chosen,'training_scores':scores})
    if not schedule: raise ValueError('No eligible year boundary for selection replay')
    return schedule,records


def paired_block_interval(candidate,baseline,contract):
    """Conditional uncertainty only; does not repair post-hoc hypothesis choice."""
    if [r['date'] for r in candidate]!=[r['date'] for r in baseline]: raise ValueError('Unpaired curves')
    excess=[math.log(b['nav']/a['nav'])-math.log(d['nav']/c['nav'])
            for a,b,c,d in zip(candidate,candidate[1:],baseline,baseline[1:])]
    if not excess: return None
    rng=random.Random(contract['bootstrap_seed']); n=len(excess); block=contract['bootstrap_block_sessions']; means=[]
    for _ in range(contract['bootstrap_repetitions']):
        sampled=[]
        while len(sampled)<n:
            start=rng.randrange(n)
            sampled.extend(excess[(start+j)%n] for j in range(min(block,n-len(sampled))))
        means.append(statistics.fmean(sampled)*252)
    means.sort()
    return {'annual_log_relative_growth':statistics.fmean(excess)*252,
            'conditional_95pct_interval':[means[int(.025*len(means))],means[int(.975*len(means))]],
            'block_sessions':block,'replicates':len(means),
            'note':'Conditional block bootstrap; no multiple-search or hypothesis-selection adjustment, not an alpha significance claim.'}


def run_return_experiment(snapshot,output_root,contract_path=None):
    contract=load_return_contract(contract_path)
    base_path=(Path(contract_path).resolve().parent if contract_path else Path(__file__).parent/'config')/contract['base_contract']
    base=load_contract(base_path)
    output=Path(output_root)/f'return_experiment_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}'
    output.mkdir(parents=True)
    write_json(output/'contract.json',contract); write_json(output/'base_contract.json',base)
    source=output/'calculation_source'; source.mkdir()
    files=('return_research.py','return_screen.py','screen_portfolio.py','screen_factors.py','data.py','price_provenance.py','taxonomy.py')
    hashes={}
    for name in files:
        shutil.copy2(Path(__file__).parent/name,source/name); hashes[name]=digest(source/name)
    manifest={'status':'running','started_at':datetime.now(timezone.utc).isoformat(),'contract_sha256':digest(output/'contract.json'),
              'base_contract_sha256':digest(output/'base_contract.json'),'snapshot_manifest':str(Path(snapshot).resolve()/'manifest.json'),
              'snapshot_manifest_sha256':digest(Path(snapshot)/'manifest.json'),'source_hashes':hashes,'production_promotion_allowed':False}
    write_json(output/'manifest.json',manifest)
    try:
        calendar,prices,features,_,quality=prepare_panel(snapshot,base)
        panel=cache_panel(calendar,prices,features,base); write_json(output/'data_quality.json',quality)
        results={}; histories={}; curves={}
        def save(name,result,nav,fills,targets):
            results[name]=result; curves[name]=nav
            write_csv(output/f'{name}_nav.csv',nav); write_csv(output/f'{name}_fills.csv',fills)
            write_json(output/f'{name}_targets.json',targets)
            print(name,'CAGR',round(result['full']['cagr']*100,2),'MDD',round(result['full']['max_drawdown']*100,2),flush=True)
        for bps in contract['costs_bps']:
            for strategy in contract['strategies']:
                result,nav,fills,targets=simulate_return(panel,base,strategy,bps)
                save(f"full_{strategy['id']}_c{bps}",result,nav,fills,targets)
                if bps==contract['primary_one_way_cost_bps']: histories[strategy['id']]=nav
        schedule,selections=annual_schedule(calendar,histories,contract)
        write_json(output/'yearly_selections.json',selections)
        start_index=calendar.index(min(schedule))
        for bps in contract['costs_bps']:
            for strategy in contract['strategies']:
                save(f"eval_{strategy['id']}_c{bps}",*simulate_return(panel,base,strategy,bps,start_index))
            save(f'walk_forward_c{bps}',*simulate_return(panel,base,schedule[min(schedule)],bps,start_index,schedule))
        primary=contract['primary_one_way_cost_bps']
        selected_after_full_sample=max((s for s in contract['strategies'] if s['selectable']),
            key=lambda s:results[f"full_{s['id']}_c{primary}"]['full']['cagr'])['id']
        checks={'hindsight_best_full_sample':selected_after_full_sample,'hindsight_selection_is_biased':True,
                'walk_forward_choice_primary_cost_bps':primary,
                'stress_uses_same_primary_cost_choices':True,
                'walk_forward_conditional_interval':paired_block_interval(curves[f'walk_forward_c{primary}'],curves[f'eval_m20_k5_r20_c{primary}'],contract),
                'production_promotion_allowed':False}
        write_json(output/'results.json',results); write_json(output/'checks.json',checks)
        manifest.update(status='complete',finished_at=datetime.now(timezone.utc).isoformat(),scenarios=len(results),
                        results_sha256=digest(output/'results.json'),checks=checks)
        write_json(output/'manifest.json',manifest)
        build_return_report(output,results,contract,selections,checks)
        return {'directory':str(output.resolve()),'scenarios':len(results),'checks':checks,
                'walk_forward':{str(b):results[f'walk_forward_c{b}']['full'] for b in contract['costs_bps']}}
    except BaseException as exc:
        manifest.update(status='failed',error=f'{type(exc).__name__}: {exc}'); write_json(output/'manifest.json',manifest)
        raise


def build_return_report(output,results,contract,selections,checks):
    bps=contract['primary_one_way_cost_bps']
    lines=['# 收益优先的 ETF 粗筛实验','',
        '沿用固定15只ETF与统一前复权数据。全期2021-07-01至2026-11-04；所有参数和候选在本轮计算前固定。',
        '假设是在看过上一轮历史后提出，以下滚动选择属于时间顺序回放，不是此前未接触的样本外证明。',
        f'代表成本：单边{bps}bp。另测30bp；次日开盘、现金、成交额容量和成本账本规则保持一致。',
        '', '| 固定方案 | 全期年化 | 全期最大回撤 | 2024起同现金起点年化 | 2024起最大回撤 |', '|---|---:|---:|---:|---:|']
    for s in contract['strategies']:
        full=results[f"full_{s['id']}_c{bps}"]['full']; evaluation=results[f"eval_{s['id']}_c{bps}"]['full']
        lines.append(f"| {s['label']} | {full['cagr']:.2%} | {full['max_drawdown']:.2%} | {evaluation['cagr']:.2%} | {evaluation['max_drawdown']:.2%} |")
    lines+=['','每年仅使用上一年末已知的504个交易日净收益选择一个规则，再用独立持仓账本继续下一年；换规则的交易成本照常扣除。30bp压力场景沿用15bp时选出的规则，不重新挑赢家。',
             '', '| 决策日 | 首个可交易日 | 按此前收益选择的规则 |','|---|---|---|']
    lines += [f"| {r['decision_date']} | {r['first_possible_fill']} | {r['strategy_id']} |" for r in selections]
    for cost in contract['costs_bps']:
        wf=results[f'walk_forward_c{cost}']['full']; baseline=results[f'eval_m20_k5_r20_c{cost}']['full']
        lines += ['',f"滚动选择（单边{cost}bp）年化 {wf['cagr']:.2%}，最大回撤 {wf['max_drawdown']:.2%}；同起点动量5只基线年化 {baseline['cagr']:.2%}，最大回撤 {baseline['max_drawdown']:.2%}。"]
    lines+=['',f"全历史事后最佳规则为 {checks['hindsight_best_full_sample']}，它经过多方案挑选，不能直接视为今后的最佳规则。",
        '', '数据限制：当前存续固定池、供应商前复权价格代理、分数研究份额；没有认证历史成分、原始份额分红/拆分、整手或真实盘中成交。',
        '本实验不复现原资金流+LLM完整链，不使用补造的历史新闻。集中度收益与排序能力分开比较；生产排名未自动替换。',
        '所有场景、目标权重、成交、净值、年分段、训练选择记录、输入和原计算代码哈希保存在同一目录。']
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    rendered=[]; in_table=False
    for line in lines:
        if line.startswith('|---'): continue
        if line.startswith('|'):
            if not in_table: rendered.append('<table>'); in_table=True
            rendered.append('<tr>'+''.join('<td>'+html.escape(x.strip())+'</td>' for x in line.strip('|').split('|'))+'</tr>')
        else:
            if in_table: rendered.append('</table>'); in_table=False
            rendered.append('<h1>'+html.escape(line[2:])+'</h1>' if line.startswith('# ') else '<p>'+html.escape(line)+'</p>')
    if in_table: rendered.append('</table>')
    document='<!doctype html><meta charset="utf-8"><title>收益优先 ETF 实验</title><style>body{font:16px system-ui;color:#223;max-width:1150px;margin:40px auto}p{line-height:1.7}table{border-collapse:collapse;width:100%}td{padding:8px;border-bottom:1px solid #ddd}tr:first-child{background:#eef3fa;font-weight:700}</style>'+''.join(rendered)
    (output/'report.html').write_text(document,encoding='utf-8')
