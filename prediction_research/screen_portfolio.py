"""Continuous adjusted-price portfolio diagnostics for fixed ETF screen rules.

Execution uses the next session's open, prior-day ADV, cash accounting and
one-way costs. Adjusted research units are fractional; these are not raw-share
broker fills. No inference of corporate-action ratios is permitted.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from .data import load_cache
from .screen_factors import feature_window, load_contract, select_profile


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def write_csv(path, rows):
    if not rows:
        Path(path).write_text('',encoding='utf-8'); return
    with Path(path).open('w',newline='',encoding='utf-8-sig') as handle:
        writer=csv.DictWriter(handle,fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)


def prepare_panel(snapshot, contract):
    snapshot=Path(snapshot)
    provenance=json.loads((snapshot/'manifest.json').read_text(encoding='utf-8'))
    if provenance.get('basis')!='eastmoney_fqt_1_adjusted_price_proxy':
        raise ValueError('This experiment requires a documented, consistently adjusted snapshot')
    series={}; quality={}; mapping={a['symbol']:a for a in contract['assets']}
    for sym in mapping:
        meta=provenance['assets'].get(sym,{})
        path=snapshot/f'cache_{sym}.csv'
        if meta.get('status')!='ok' or not path.exists() or digest(path)!=meta.get('sha256'):
            raise ValueError(f'{sym}: missing or changed frozen input')
        loaded=load_cache(sym,snapshot)
        raw_rows=list(csv.DictReader(path.open(encoding='utf-8-sig')))
        if len(loaded.bars)!=len(raw_rows):
            raise ValueError(f'{sym}: invalid/duplicate bars were silently discarded by cache loader')
        series[sym]=loaded.bars
        quality[sym]={'rows':len(loaded.bars),'first':str(loaded.bars[0].trading_date),
                      'last':str(loaded.bars[-1].trading_date),'sha256':loaded.sha256}
    calendar=[b.trading_date for b in series['510300'] if contract['start']<=str(b.trading_date)<=contract['end']]
    if not calendar or str(calendar[-1])!=contract['end']:
        raise ValueError('Benchmark does not cover the requested end date')
    prices={}; features={}; returns={}; calendar_set=set(calendar)
    for sym,bars in series.items():
        byday={b.trading_date:b for b in bars}
        missing=[str(d) for d in calendar if d not in byday]
        if missing: raise ValueError(f'{sym}: incomplete shared calendar {missing[:5]}')
        jumps=[str(b.trading_date) for a,b in zip(bars,bars[1:]) if abs(b.close/a.close-1)>.22 and contract['start']<=str(b.trading_date)<=contract['end']]
        if jumps: raise ValueError(f'{sym}: unresolved adjusted price discontinuity {jumps}')
        prices[sym]=byday
        features[sym]={}
        returns[sym]={b.trading_date:b.close/a.close-1 for a,b in zip(bars,bars[1:])}
        for i,bar in enumerate(bars):
            if bar.trading_date in calendar_set:
                features[sym][bar.trading_date]=feature_window(bars[max(0,i-125):i+1],contract['min_history_bars'])
        quality[sym]['missing_adv20_dates']=sum(v is None or v['adv20'] is None for v in features[sym].values())
    return calendar,prices,features,returns,quality


class Portfolio:
    def __init__(self, cash):
        self.cash=float(cash); self.units={}; self.fills=[]

    def value(self, prices):
        if any(sym not in prices or not math.isfinite(prices[sym]) or prices[sym]<=0 for sym in self.units):
            raise ValueError('Missing held position valuation')
        return self.cash+sum(self.units[sym]*prices[sym] for sym in self.units)

    def rebalance(self, target, opens, previous, known_adv, limits, cost_bps, capacity, buffer, day, signal_day):
        if any(not math.isfinite(w) or w<0 for w in target.values()) or sum(target.values())>1+1e-10:
            raise ValueError('Invalid long-only target weights')
        nav=self.value(opens); fee=cost_bps/10000
        symbols=sorted(set(self.units)|set(target))
        held={s:self.units.get(s,0)*opens[s] for s in symbols}
        low,high=0.0,nav
        for _ in range(50):
            sized=(low+high)/2
            cost=sum(abs(target.get(s,0)*sized-held[s])*fee for s in symbols)
            if sized+cost>nav: high=sized
            else: low=sized
        desired={s:target.get(s,0)*low-held[s] for s in symbols}
        trades={}; blocked=[]
        for sym,delta in desired.items():
            if abs(delta)<1e-7: continue
            # Always permit intended exits through the turnover buffer.
            if target.get(sym,0)>0 and abs(delta)/nav<buffer: continue
            gap=opens[sym]/previous[sym]-1
            if (delta>0 and gap>=limits[sym]-.001) or (delta<0 and gap<=-limits[sym]+.001):
                blocked.append({'symbol':sym,'reason':'open_limit_proxy'}); continue
            adv=known_adv.get(sym)
            if adv is None or adv<=0:
                blocked.append({'symbol':sym,'reason':'unknown_lagged_capacity'}); continue
            capped=math.copysign(min(abs(delta),adv*capacity),delta)
            if abs(capped)<abs(delta)-1e-6:
                blocked.append({'symbol':sym,'reason':'capacity_partial'})
            trades[sym]=max(capped,-held[sym])
        def fill(sym,value):
            units=value/opens[sym]; cost=abs(value)*fee
            self.cash-=value+cost
            self.units[sym]=self.units.get(sym,0)+units
            if abs(self.units[sym])<1e-10: self.units.pop(sym,None)
            self.fills.append({'date':str(day),'signal_date':str(signal_day),'symbol':sym,
                               'units':units,'price':opens[sym],'notional':value,'cost':cost,
                               'known_adv':known_adv[sym]})
        for sym in sorted(trades):
            if trades[sym]<0: fill(sym,trades[sym])
        need=sum(value*(1+fee) for value in trades.values() if value>0)
        scale=min(1.0,max(0,self.cash)/need) if need else 1.0
        for sym in sorted(trades):
            if trades[sym]>1e-7: fill(sym,trades[sym]*scale)
        if self.cash < -1e-6 or any(q < -1e-8 for q in self.units.values()):
            raise AssertionError('Cash/long-only invariant violated')
        return blocked


def metrics(nav, initial, fills, prior_date=None):
    if not nav: return None
    values=[initial]+[r['nav'] for r in nav]
    daily=[b/a-1 for a,b in zip(values,values[1:])]
    elapsed=(date.fromisoformat(nav[-1]['date'])-(prior_date or date.fromisoformat(nav[0]['date']))).days
    years=max(elapsed,1)/365.25
    total=values[-1]/initial-1
    vol=statistics.pstdev(daily)*math.sqrt(252)
    peak=initial; drawdown=0.0
    for value in values:
        peak=max(peak,value); drawdown=min(drawdown,value/peak-1)
    return {'total_return':total,'cagr':(1+total)**(1/years)-1,
            'max_drawdown':drawdown,'annual_volatility':vol,
            'sharpe_rf0':statistics.fmean(daily)/statistics.pstdev(daily)*math.sqrt(252) if vol else None,
            'mean_exposure':statistics.fmean(r['exposure'] for r in nav),
            'trades':len(fills),'total_cost':sum(f['cost'] for f in fills),
            'one_way_turnover_per_year':sum(abs(f['notional']) for f in fills)/statistics.fmean(values)/2/years,
            'sessions':len(nav)}


def audit_ledger(nav, fills, prices, initial):
    """Rebuild cash/units from fills, independently of the execution object."""
    byday=defaultdict(list)
    for fill in fills: byday[fill['date']].append(fill)
    cash=float(initial); positions={}; max_error=0.0
    for row in nav:
        for fill in byday[row['date']]:
            if fill['signal_date']>=fill['date']: raise AssertionError('Same-day/future execution')
            sym=fill['symbol']; positions[sym]=positions.get(sym,0)+fill['units']
            cash-=fill['units']*fill['price']+fill['cost']
        d=date.fromisoformat(row['date'])
        expected=cash+sum(q*prices[s][d].close for s,q in positions.items())
        max_error=max(max_error,abs(expected-row['nav']),abs(cash-row['cash']))
        if max_error>1e-6 or cash < -1e-6: raise AssertionError('Independent ledger reconciliation failed')
    return {'passed':True,'maximum_absolute_error':max_error,'sessions':len(nav)}


def simulate(calendar, prices, features, returns, contract, method, cadence, bps):
    portfolio=Portfolio(contract['initial_cash'])
    nav=[]; targets=[]; blocks=[]; pending=None
    assets={a['symbol']:a for a in contract['assets']}
    for idx,day in enumerate(calendar):
        opens={s:prices[s][day].open for s in assets}; closes={s:prices[s][day].close for s in assets}
        if pending:
            signal_day,target,adv=pending
            previous={s:prices[s][signal_day].close for s in assets}
            reasons=portfolio.rebalance(target,opens,previous,adv,{s:a['limit_pct'] for s,a in assets.items()},bps,
                    contract['max_participation_lagged_adv'],contract['trade_weight_buffer'] if method in {'diversified','regime_risk'} else 0,
                    day,signal_day)
            blocks.extend({'date':str(day),**reason} for reason in reasons)
            pending=None
        value=portfolio.value(closes)
        nav.append({'date':str(day),'nav':value,'cash':portfolio.cash,'exposure':1-portfolio.cash/value,
                    'holdings_count':len(portfolio.units)})
        scheduled=(idx%cadence==0) if method!='benchmark_hold' else idx==0
        if not scheduled or idx==len(calendar)-1: continue
        rows=[{**assets[s],'features':features[s][day]} for s in assets]
        known={s:{d:r for d,r in returns[s].items() if d<=day and (day-d).days<=150} for s in assets}
        result=select_profile(rows,method,contract,known,set(portfolio.units))
        target=result['weights']
        adv={s:features[s][day]['adv20'] if features[s][day] else None for s in assets}
        pending=(day,target,adv)
        targets.append({'signal_date':str(day),'execute_not_before':str(calendar[idx+1]),'weights':target,
                        'breadth':result['breadth'],'volatility_scale':result.get('volatility_scale'),
                        'rejected':result['rejected']})
    result={'full':metrics(nav,contract['initial_cash'],portfolio.fills), 'periods':{},
            'block_counts':dict(Counter(r['reason'] for r in blocks)),
            'ledger':audit_ledger(nav,portfolio.fills,prices,contract['initial_cash'])}
    for name,(start,end) in contract['periods'].items():
        part=[r for r in nav if start<=r['date']<=end]
        preceding=[r for r in nav if r['date']<start]
        initial=preceding[-1]['nav'] if preceding else contract['initial_cash']
        previous_date=date.fromisoformat(preceding[-1]['date']) if preceding else None
        result['periods'][name]=metrics(part,initial,[f for f in portfolio.fills if start<=f['date']<=end],previous_date)
    return result,nav,portfolio.fills,targets


def run_portfolio_experiment(snapshot, output_root, contract_path=None):
    contract=load_contract(contract_path)
    output=Path(output_root)/f"screen_experiment_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
    output.mkdir(parents=True)
    # Write the complete fixed contract BEFORE loading/evaluating any outcomes.
    write_json(output/'experiment_contract.json',contract)
    manifest={'started_at':datetime.now(timezone.utc).isoformat(),'status':'running',
              'contract_sha256':digest(output/'experiment_contract.json'),
              'snapshot_manifest':str(Path(snapshot).resolve()/'manifest.json'),
              'snapshot_manifest_sha256':digest(Path(snapshot)/'manifest.json'),
              'source_hashes':{name:digest(Path(__file__).parent/name) for name in ('screen_portfolio.py','screen_factors.py')},
              'production_promotion_allowed':False}
    write_json(output/'manifest.json',manifest)
    try:
        calendar,prices,features,returns,quality=prepare_panel(snapshot,contract)
        write_json(output/'data_quality.json',quality)
        results={}; curves={}
        for cadence in contract['rebalance_days']:
            for bps in contract['one_way_cost_bps']:
                for method in contract['methods']:
                    name=f'{method}_r{cadence}_c{bps}'
                    result,nav,fills,targets=simulate(calendar,prices,features,returns,contract,method,cadence,bps)
                    results[name]={'method':method,'rebalance_days':cadence,'one_way_cost_bps':bps,**result}
                    write_csv(output/f'{name}_nav.csv',nav)
                    write_csv(output/f'{name}_fills.csv',fills)
                    write_json(output/f'{name}_targets.json',targets)
                    if cadence==20 and bps==15: curves[method]=nav
                    print(name, 'CAGR',round(result['full']['cagr']*100,2),'MDD',round(result['full']['max_drawdown']*100,2),flush=True)
        manifest.update(status='complete',finished_at=datetime.now(timezone.utc).isoformat(),
                        scenarios=len(results),data_quality_caveats=[
                            'Adjusted-price research units; no raw-share corporate-action, lot/minimum-fee or tax ledger.',
                            'Current fixed universe, no historical delisted-fund membership. No untouched holdout.',
                            'One-way all-in friction and open-gap limit proxy; intraday fillability is unverified.',
                            'Portfolio dates are next-session executions; no news/flow history invented for the long experiment.'])
        write_json(output/'results.json',results)
        manifest['results_sha256']=digest(output/'results.json')
        manifest['economic_checks']=economic_checks(results,contract)
        write_json(output/'manifest.json',manifest)
        build_report(output,results,manifest,contract,curves)
        return {'directory':str(output.resolve()),'scenarios':len(results),'economic_checks':manifest['economic_checks'],
                'production_promotion_allowed':False}
    except BaseException as exc:
        manifest.update(status='failed',error=f'{type(exc).__name__}: {exc}')
        write_json(output/'manifest.json',manifest)
        raise


def economic_checks(results,contract):
    checks={}
    for method in ('multi_factor','pullback','diversified','regime_risk'):
        checks[method]={}
        for cadence in contract['rebalance_days']:
            pairs=[]
            for bps in contract['one_way_cost_bps']:
                candidate=results[f'{method}_r{cadence}_c{bps}']['periods']['later']
                mom=results[f'momentum20_r{cadence}_c{bps}']['periods']['later']
                equal=results[f'equal_weight_r{cadence}_c{bps}']['periods']['later']
                pairs.append(candidate['total_return']>max(mom['total_return'],equal['total_return']) and
                             candidate['max_drawdown']>=mom['max_drawdown'])
            checks[method][str(cadence)]={'later_period_return_beats_both_baselines_and_drawdown_not_worse_at_all_costs':all(pairs),
                                         'promotable':False,'reason':'PIT/universe/corporate-action certification and forward validation not completed'}
    return checks


def build_report(output,results,manifest,contract,curves):
    labels={'cash':'现金','benchmark_hold':'沪深300持有','equal_weight':'同池等权','momentum20':'20日动量',
            'multi_factor':'多维趋势','pullback':'趋势内回撤','diversified':'趋势+去重+换手约束','regime_risk':'再加市场状态/风险预算'}
    lines=['# ETF 因子与组合实验', '', f"固定池：{len(contract['assets'])} 只；{contract['start']} 至 {contract['end']}。",
           '代表场景：每20个交易日调仓，单边总摩擦15bp。其他成本/频率全部保存在 results.json。',
           '', '| 方案 | 全期年化 | 全期最大回撤 | 2025起累计收益 | 2025起最大回撤 | 平均仓位 |',
           '|---|---:|---:|---:|---:|---:|']
    for method in contract['methods']:
        row=results[f'{method}_r20_c15']; f=row['full']; late=row['periods']['later']
        lines.append(f"| {labels[method]} | {f['cagr']:.2%} | {f['max_drawdown']:.2%} | {late['total_return']:.2%} | {late['max_drawdown']:.2%} | {f['mean_exposure']:.1%} |")
    lines+=['','所有结果为固定规则的探索性研究；不代表原资金流+LLM主链收益。',
            '后期分段沿用连续组合状态，不是事前完全未触碰的样本；本轮没有根据结果反复调权。',
            '收益采用供应商前复权价格代理和分数研究单位，未逐笔重建基金分红/拆分原始份额。',
            '冻结了配置、数据和源码哈希，逐日现金/持仓账本独立复算通过。生产替换仍未放行。',
            '']
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        plt.rcParams['font.sans-serif']=['Microsoft YaHei','SimHei','DejaVu Sans']
        plt.rcParams['axes.unicode_minus']=False
        fig,(ax,dd)=plt.subplots(2,1,figsize=(12,8),sharex=True,gridspec_kw={'height_ratios':[2,1]})
        for method,nav in curves.items():
            if method=='cash': continue
            dates=[date.fromisoformat(r['date']) for r in nav]; values=[r['nav']/contract['initial_cash'] for r in nav]
            ax.plot(dates,values,label=labels[method],linewidth=1.5)
            peak=1; draws=[]
            for value in values: peak=max(peak,value); draws.append((value/peak-1)*100)
            dd.plot(dates,draws,linewidth=1.1)
        ax.set_ylabel('归一化净值'); dd.set_ylabel('回撤 %'); ax.legend(ncol=2,fontsize=9)
        ax.set_title('固定15只ETF｜20日调仓｜单边成本15bp｜调整价格研究代理')
        ax.grid(alpha=.2); dd.grid(alpha=.2); fig.tight_layout(); fig.savefig(output/'equity.png',dpi=150); plt.close(fig)
    except ImportError:
        pass
    has_chart=(output/'equity.png').is_file()
    lines.append('净值图：![净值与回撤](equity.png)' if has_chart else '未生成图表（可选绘图库不可用）；逐日净值仍保存于各场景 CSV。')
    (output/'report.md').write_text('\n'.join(lines),encoding='utf-8')
    table=''.join('<tr>'+''.join(f'<td>{html.escape(cell.strip())}</td>' for cell in line.strip('|').split('|'))+'</tr>' for line in lines if line.startswith('|') and not line.startswith('|---'))
    document=f'<!doctype html><meta charset="utf-8"><title>ETF 因子实验</title><style>body{{font:16px system-ui;max-width:1100px;margin:40px auto;color:#223}}table{{border-collapse:collapse;width:100%}}td{{padding:10px;border-bottom:1px solid #ddd}}tr:first-child{{font-weight:700;background:#eef3f8}}img{{width:100%}}p{{line-height:1.7}}</style><h1>ETF 因子与组合实验</h1><p>{html.escape(lines[2])}</p><p>{html.escape(lines[3])}</p><table>{table}</table><p>探索性结果：当前固定池、供应商前复权代理。未认证历史成分/公司行动，未放行生产替换；不能视为原资金流+LLM策略的收益。</p><img src="equity.png"><p>详细配置、输入哈希、48个场景和逐日账本均在同一目录。</p>'
    if not has_chart:
        document=document.replace('<img src="equity.png">','<p>绘图库不可用；可查看逐日净值 CSV。</p>')
    (output/'report.html').write_text(document,encoding='utf-8')
