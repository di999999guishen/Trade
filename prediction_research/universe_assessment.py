"""Every provider-universe record gets class-specific criteria and an explicit result.

Coverage is not a claim of expected return. Unknown identity, short histories,
missing income data and stale quotes remain visible, never silently discarded.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import sqlite3
import statistics
from collections import Counter,defaultdict
from datetime import date,datetime,time,timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import resolve_project_path
from .data import configured_data_dirs,load_cache
from .flow_context import finite
from .taxonomy import classify_etf,market_scope


VERSION='universe-class-assessment-v1'
CATALOG={
    'equity.broad_market':('股票·宽基','同类中期相对强弱与趋势持续性'),
    'equity.sector':('股票·行业','行业强弱、资金持续性与成交承接'),
    'equity.thematic':('股票·主题','主题趋势、追高程度与拥挤风险'),
    'equity.strategy_factor':('股票·策略/红利/增强','策略相对表现与跟踪质量'),
    'equity.active_equity':('股票·主动','净值表现、费用与持仓披露'),
    'bond.government_bond':('债券·国债/地方债','收益率、久期与利率敏感度'),
    'bond.policy_bank_bond':('债券·政金债','收益率、久期与流动性'),
    'bond.credit_bond':('债券·信用/短融/存单','收益率、信用利差与久期'),
    'bond.convertible_bond':('债券·可转债','债性、权益趋势与转股溢价'),
    'bond.bond_other':('债券·其他','收益率、久期与底层债券身份复核'),
    'commodity.gold_spot':('商品·黄金','价格趋势、波动与净值溢价'),
    'commodity.commodity_futures':('商品·期货','价格趋势、期限结构与展期收益'),
    'commodity.commodity_other':('商品·其他','标的核验、趋势与跟踪质量'),
    'money.money_market':('货币','披露收益、流动性与交易成本'),
    'multi_asset.multi_asset':('多资产','资产权重、组合波动与回撤'),
    'special.special':('特殊结构/非ETF待核','产品身份、杠杆重置或非ETF属性核验'),
    'unknown.unclassified':('待核分类','身份核验、报价有效性与流动性保底评价'),
}
REFERENCE_FIELDS={
    'premium_pct':('折溢价','%',2),'yield_7d_pct':('披露七日年化收益','%',3),
    'yield_to_maturity_pct':('底层到期收益率','%',7),'modified_duration':('修正久期','年',30),
    'credit_spread_bps':('信用利差','bp',7),'conversion_premium_pct':('转股溢价','%',2),
    'roll_yield_pct':('展期收益','%',3),'tracking_error_pct':('披露跟踪误差','%',180),
    'expense_ratio_pct':('披露年费率','%',400),'underlying_return20_pct':('底层市场20日收益','%',2),
    'fx_return20_pct':('相关汇率20日变动','%',2),'holdings_concentration_pct':('披露持仓集中度','%',120),
}


def stamp(value):
    result=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if result.utcoffset() is None: raise ValueError('Timestamp must include timezone')
    return result


def assessment_identity(row):
    name=str(row.get('name') or ''); sym=str(row.get('symbol') or '')
    typed=classify_etf(name,etf_board=True,symbol=sym)
    if any(k in name.upper() for k in ('LOF','REIT','反向','杠杆','两倍','三倍')):
        typed={'asset_class':'special','subtype':'special'}
    key=typed['asset_class']+'.'+typed['subtype']
    if key not in CATALOG: key='unknown.unclassified'; typed={'asset_class':'unknown','subtype':'unclassified'}
    return {**typed,'category':key,'market_scope':market_scope(name),
            'classification_basis':'provider_etf_board_name_or_explicit_known_code',
            'classification_verified_by_contract':False}


def history_for(cfg,symbol,asof):
    choices=[]
    for directory in configured_data_dirs(cfg):
        try:
            series=load_cache(symbol,directory,min_bars=1)
            bars=tuple(b for b in series.bars if b.trading_date<=asof)
            if bars: choices.append((bars[-1].trading_date,len(bars),series,bars))
        except (ValueError,OSError): continue
    if not choices: return None,()
    _,_,series,bars=max(choices,key=lambda v:(v[0],v[1]))
    return series,bars


def history_metrics(bars,asof,min_amount):
    if not bars or bars[-1].trading_date!=asof:
        return {},'missing_or_stale_history'
    recent=bars[-126:]
    if any(abs(b.close/a.close-1)>.22 for a,b in zip(recent,recent[1:])):
        return {},'unresolved_price_discontinuity'
    values={}
    for n in (5,20,60,120):
        if len(bars)>n: values[f'return{n}_pct']=(bars[-1].close/bars[-n-1].close-1)*100
    if len(bars)>=20:
        values['distance_ma20_pct']=(bars[-1].close/statistics.fmean(b.close for b in bars[-20:])-1)*100
        values['drawdown20_pct']=(bars[-1].close/max(b.close for b in bars[-20:])-1)*100
        amounts=[b.amount for b in bars[-20:]]
        if all(v>0 and math.isfinite(v) for v in amounts):
            values['adv20']=statistics.fmean(amounts)
            values['liquidity_days20_pct']=sum(v>=min_amount for v in amounts)/20*100
            values['amount_ratio20']=amounts[-1]/values['adv20']
    if len(bars)>=21:
        returns=[b.close/a.close-1 for a,b in zip(bars[-21:],bars[-20:])]
        volatility=statistics.pstdev(returns)
        values['annual_vol20_pct']=volatility*math.sqrt(252)*100
        values['downside20_pct']=math.sqrt(statistics.fmean(min(r,0)**2 for r in returns))*math.sqrt(252)*100
        if volatility>0: values['runup5_sigma']=values['return5_pct']/100/(volatility*math.sqrt(5))
        travel=sum(abs(b.close-a.close) for a,b in zip(bars[-21:],bars[-20:]))
        values['trend_efficiency20']=abs(bars[-1].close-bars[-21].close)/travel if travel else 0
    periods=[values.get(f'return{n}_pct') for n in (5,20,60,120)]
    present=[v for v in periods if v is not None]
    if len(present)>=2: values['positive_trend_fraction']=sum(v>0 for v in present)/len(present)
    tier='history_120d' if len(bars)>=121 else 'history_60d' if len(bars)>=61 else 'history_20d' if len(bars)>=21 else 'history_5d' if len(bars)>=6 else 'snapshot_only'
    return values,tier


def load_flow_history(cfg,decision,asof):
    """Read-only, latest available record per symbol/observed date."""
    db=resolve_project_path(cfg,cfg['state_db']); daily=defaultdict(dict)
    if not db.exists(): return daily
    zone=ZoneInfo(cfg.get('timezone','Asia/Shanghai'))
    try:
        with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as con:
            rows=con.execute('SELECT symbol,quote_epoch,retrieved_at_utc,main_net_inflow,amount FROM etf_flow_snapshots ORDER BY retrieved_at_utc').fetchall()
    except sqlite3.OperationalError: return daily
    for sym,epoch,received,net,amount in rows:
        try:
            observed=datetime.fromtimestamp(epoch,timezone.utc); available=max(observed,stamp(received))
            day=observed.astimezone(zone).date()
            if available>decision or day>asof: continue
        except (TypeError,ValueError,OverflowError,OSError): continue
        old=daily[sym].get(day)
        if old is None or available>=old['available']:
            daily[sym][day]={'net':finite(net),'amount':finite(amount),'available':available}
    return daily


def flow_metrics(history,asof):
    days=sorted(history)[-5:]
    if len(days)<5 or days[-1]!=asof: return {}
    rows=[history[d] for d in days]
    if any(r['net'] is None or r['amount'] is None or r['amount']<=0 for r in rows): return {}
    return {'flow_positive5_pct':sum(r['net']>0 for r in rows)/5*100,
            'flow_ratio5_pct':sum(r['net'] for r in rows)/sum(r['amount'] for r in rows)*100,
            'flow_observation_dates':[str(d) for d in days]}


def reference_metric(references,symbol,key,decision,asof):
    record=references.get(symbol,{}).get(key)
    if not isinstance(record,dict): return None,'missing_reference',None
    try:
        available=stamp(record['available_at_utc']); source_date=date.fromisoformat(record['asof_date'])
        if available>decision or source_date>asof: return None,'future_reference',None
        if (asof-source_date).days>REFERENCE_FIELDS[key][2]: return None,'stale_reference',None
        value=finite(record['value'])
        if value is None or not record.get('source_url'): return None,'invalid_reference',None
        return value,'evaluated',record
    except (KeyError,TypeError,ValueError): return None,'invalid_reference',None


def criterion(key,label,value,rule,unit='',missing='insufficient_data',basis=None):
    return {'id':key,'label':label,'status':'evaluated' if value is not None else missing,
            'value':value,'unit':unit,'rule':rule,'basis':basis}


def uses_price_strength(row):
    return row['asset_class'] in {'equity','commodity','multi_asset'} or row['subtype']=='convertible_bond'


def assess_rows(cfg,snapshot,manifest,decision_at,history_loader=None,flow_history=None,references=None):
    decision=stamp(decision_at); available=stamp(manifest['retrieved_at_utc'])
    if available>decision: raise ValueError('Snapshot was unavailable at decision time')
    zone=ZoneInfo(cfg.get('timezone','Asia/Shanghai')); records=snapshot['records']
    if not records: raise ValueError('Empty ETF universe')
    if snapshot.get('source_market_total') is not None and snapshot['source_market_total']!=len(records):
        raise ValueError('Provider universe count mismatch; refusing to label a partial snapshot complete')
    ids=[(r.get('exchange'),str(r.get('symbol'))) for r in records]
    if len(ids)!=len(set(ids)) or any(not r.get('symbol') for r in records):
        raise ValueError('Duplicate or missing universe identities')
    days=[]
    for row in records:
        epoch=finite(row.get('quote_epoch'))
        try:
            if epoch is not None and epoch<=decision.timestamp(): days.append(datetime.fromtimestamp(epoch,zone).date())
        except (ValueError,OverflowError,OSError): pass
    asof=max(days) if days else decision.astimezone(zone).date()
    loader=history_loader or (lambda sym,day:history_for(cfg,sym,day))
    flows=load_flow_history(cfg,decision,asof) if flow_history is None else flow_history
    references={} if references is None else references
    minimum=cfg['etf_market']['screen']['min_amount']; output=[]
    for source in records:
        identity=assessment_identity(source); sym=str(source['symbol']); asset=identity['asset_class']
        series,bars=loader(sym,asof); metrics,tier=history_metrics(bars,asof,minimum)
        metrics.update(flow_metrics(flows.get(sym,{}),asof))
        epoch=finite(source.get('quote_epoch')); price=finite(source.get('price')); amount=finite(source.get('amount'))
        try:
            quote=datetime.fromtimestamp(epoch,timezone.utc) if epoch is not None else None
            quote_day=quote.astimezone(zone).date() if quote else None
        except (ValueError,OverflowError,OSError): quote=None; quote_day=None
        quote_state='missing_quote_time' if quote is None else 'future_quote' if quote>decision else 'stale_quote' if quote_day!=asof or (decision-quote).total_seconds()>cfg.get('workflow',{}).get('max_quote_age_days',4)*86400 else 'current'
        if quote_state=='current' and (price is None or price<=0): quote_state='invalid_price'
        if quote_state!='current': metrics={}; tier='stale_or_unavailable_quote'
        checks=[criterion('identity','类别与产品身份',identity['category'],'产品名称/已知代码初判；基金合同待核验',basis=identity['classification_basis']),
                criterion('quote_valid','报价有效性',bool(price is not None and price>0) if quote_state=='current' else None,'需要决策时已知的同日有效报价；过期仍保留记录',missing=quote_state),
                criterion('liquidity','当日成交额',amount if quote_state=='current' and amount is not None and amount>=0 else None,f'参考门槛{minimum:g}元；低于门槛标记而不删除','元',missing=quote_state if quote_state!='current' else 'missing_turnover'),
                criterion('history_depth','已知历史根数',len(bars),'121根长周期/61根中期/21根短期/6根新基金/单快照分层评价','根'),
                criterion('adv20','持续流动性',metrics.get('adv20'),'仅用真实成交额；不从混合单位成交量推算','元'),
                criterion('liquidity_days20_pct','20日流动性达标比例',metrics.get('liquidity_days20_pct'),'过去20根均有真实成交额才计算','%'),
                criterion('spread_bps','买卖价差成本',None,'需同一时点买一/卖一报价；未采集不假定为零','bp')]
        keys=[]
        if uses_price_strength(identity):
            for n in (5,20,60,120): checks.append(criterion(f'return{n}_pct',f'{n}日价格收益',metrics.get(f'return{n}_pct'),'同一价格口径；仅历史强弱，不是预期收益','%'))
            checks += [criterion('trend_consistency','多周期趋势一致性',metrics.get('positive_trend_fraction'),'至少两个周期；上涨周期数/有效周期数'),
                       criterion('trend_efficiency20','20日趋势连贯度',metrics.get('trend_efficiency20'),'净价格位移/累计绝对价格变化；须结合方向，下降趋势也可能很连贯'),
                       criterion('distance_ma20','距离20日均价',metrics.get('distance_ma20_pct'),'价格相对20日均价的偏离，结合波动判断拉升或回撤阶段','%'),
                       criterion('amount_ratio20','成交额相对20日均值',metrics.get('amount_ratio20'),'当日真实成交额/含当日20日均额；放量本身不代表看涨','倍'),
                       criterion('runup5_sigma','近期追高程度',metrics.get('runup5_sigma'),'5日涨幅相对20日波动；高值提示已快速上涨','标准差'),
                       criterion('flow_persistence5','资金持续性',metrics.get('flow_positive5_pct'),'5个已观测日期的净流入比例；非保证连续交易日，非申购赎回','%',basis=metrics.get('flow_observation_dates')),
                       criterion('flow_ratio5','5观测日净流入占成交额',metrics.get('flow_ratio5_pct'),'与资金持续性相关，不当作独立确认','%',basis=metrics.get('flow_observation_dates'))]
        if asset!='money':
            checks += [criterion('downside20','下行波动',metrics.get('downside20_pct'),'20日负收益的年化半偏差','%'),
                       criterion('drawdown20','20日高点回撤',metrics.get('drawdown20_pct'),'当前位置距离20日收盘高点','%')]
        if asset=='bond':
            keys+=['yield_to_maturity_pct','modified_duration']
            if identity['subtype']=='credit_bond': keys+=['credit_spread_bps']
            if identity['subtype']=='convertible_bond': keys+=['conversion_premium_pct']
        if asset=='money': keys+=['yield_7d_pct']
        if identity['subtype']=='commodity_futures': keys+=['roll_yield_pct']
        if asset=='multi_asset' or identity['subtype']=='active_equity': keys+=['holdings_concentration_pct']
        if asset not in {'special','unknown'}: keys+=['premium_pct','tracking_error_pct','expense_ratio_pct']
        if identity['market_scope']=='cross_border': keys+=['underlying_return20_pct','fx_return20_pct']
        for key in keys:
            value,status,provenance=reference_metric(references,sym,key,decision,asof)
            if quote_state!='current': value=None; status=quote_state
            metrics[key]=value
            label,unit,_=REFERENCE_FIELDS[key]
            checks.append(criterion(key,label,value,'需独立来源、首次可得时间及对应日期；缺失不推算',unit,missing=status,basis=provenance))
        if asset in {'special','unknown'}:
            checks.append(criterion('contract_review','产品合同复核',None,'先核实产品类型及特殊风险；仍保留报价/流动性评价',missing='identity_review_required'))
        if asset=='money':
            return_status='income_evidence' if metrics.get('yield_7d_pct') is not None else 'income_data_missing'
        elif asset=='bond' and identity['subtype']!='convertible_bond':
            return_status='bond_evidence' if metrics.get('yield_to_maturity_pct') is not None and metrics.get('modified_duration') is not None else 'bond_fundamentals_missing'
        elif asset in {'unknown','special'}: return_status='identity_review_required'
        else: return_status='price_evidence' if metrics.get('return20_pct') is not None else 'limited_evidence'
        output.append({'symbol':sym,'exchange':source.get('exchange'),'name':source.get('name'),**identity,
                       'assessment_status':return_status,'history_tier':tier,'quote_state':quote_state,
                       'criteria':checks,'metrics':metrics,'amount':amount,'price':price,
                       'price_provenance':series.provider_metadata or {'status':'unknown_legacy_basis'} if series else None,
                       'history_sha256':series.sha256 if series else None,
                       'missing_criteria':[c['id'] for c in checks if c['status']!='evaluated'],
                       'historical_features_not_certified_pit':True,'return_forecast':None})
    peers=defaultdict(list)
    for row in output:
        if uses_price_strength(row) and row['quote_state']=='current' and row['metrics'].get('return20_pct') is not None:
            peers[(row['category'],row['market_scope'])].append(row)
    for row in output:
        if not uses_price_strength(row): continue
        group=peers.get((row['category'],row['market_scope']),[])
        median=statistics.median(r['metrics']['return20_pct'] for r in group) if len(group)>=3 else None
        own_return=row['metrics'].get('return20_pct')
        value=own_return-median if median is not None and own_return is not None else None
        row['metrics']['relative_strength20_pct']=value
        row['criteria'].append(criterion('relative_strength20','同类20日相对强弱',value,'同类别、同市场范围，至少3只；同类中位数差，不等于独立指数收益','%',basis={'peer_count':len(group)}))
    coverage={key:{'label':label,'criterion':rule,'rows':0,'cross_border_rows':0,'numeric_evidence_rows':0} for key,(label,rule) in CATALOG.items()}
    queues=defaultdict(list)
    for row in output:
        row['missing_criteria']=[c['id'] for c in row['criteria'] if c['status']!='evaluated']
        coverage[row['category']]['rows']+=1
        coverage[row['category']]['cross_border_rows']+=row['market_scope']=='cross_border'
        numeric=any(c['status']=='evaluated' and isinstance(c['value'],(int,float)) and not isinstance(c['value'],bool) and c['id']!='history_depth' for c in row['criteria'])
        coverage[row['category']]['numeric_evidence_rows']+=numeric
        basis='yield_7d_pct' if row['asset_class']=='money' and row['metrics'].get('yield_7d_pct') is not None else 'return20_pct' if uses_price_strength(row) and row['metrics'].get('return20_pct') is not None else 'liquidity_only'
        value=row['metrics'].get(basis) if basis!='liquidity_only' else row['amount'] if row['quote_state']=='current' else None
        row['comparison_basis']=basis; row['comparison_value']=value
        queues[(row['category'],row['market_scope'],row['history_tier'],basis)].append(row)
    groups=[]
    for key,group in sorted(queues.items()):
        ranked=sorted(group,key=lambda r:(r['comparison_value'] is None,-(r['comparison_value'] or 0),r['symbol']))
        groups.append({'category':key[0],'market_scope':key[1],'history_tier':key[2],'basis':key[3],
                       'members':[r['symbol'] for r in ranked],'note':'同证据层内排序；流动性排序不代表收益排序'})
    assert len(output)==len(records) and all(r['criteria'] and r['category'] in CATALOG for r in output)
    reported=snapshot.get('source_market_total')
    return {'schema_version':1,'algorithm_version':VERSION,'status':'complete','created_at_utc':datetime.now(timezone.utc).isoformat(),
            'decision_at_utc':decision.isoformat(),'feature_date':str(asof),'source_snapshot':manifest,
            'universe_coverage':{'input_records':len(records),'assessed_records':len(output),'rule_coverage':1.0,'dropped_records':0,
                'provider_reported_total':reported,'provider_count_matches':reported==len(records) if reported is not None else None,
                'exchange_registry_completeness':'not_independently_certified','category_rows':coverage},
            'counts':{'history_tiers':dict(Counter(r['history_tier'] for r in output)),
                      'assessment_status':dict(Counter(r['assessment_status'] for r in output)),
                      'quote_state':dict(Counter(r['quote_state'] for r in output))},
            'rows':output,'comparison_groups':groups,'agent_limit':cfg.get('workflow',{}).get('agent_top_n',10),
            'prediction_limit':cfg.get('workflow',{}).get('prediction_top_n',20),
            'limitations':['Full coverage of the supplied provider snapshot, not proof of exchange-registry completeness.',
                'All categories have rules; rule assignment is not complete data or a validated return forecast.',
                'Name-based category and legacy price basis remain explicitly unverified.',
                'All rows are evaluated before profitability/liquidity filters; missing observations remain missing.']}


def run_universe_assessment(cfg,decision_at=None,output_dir=None):
    from .screening import _snapshot_path
    directory=resolve_project_path(cfg,cfg['etf_market']['snapshot_dir'])
    manifest=json.loads((directory/'latest.json').read_text(encoding='utf-8'))
    path=_snapshot_path(directory,manifest)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=manifest['sha256']: raise ValueError('Universe snapshot hash mismatch')
    snapshot=json.loads(path.read_text(encoding='utf-8'))
    references={}
    reference_path=cfg.get('universe_assessment',{}).get('reference_data_path')
    if reference_path: references=json.loads(resolve_project_path(cfg,reference_path).read_text(encoding='utf-8'))
    result=assess_rows(cfg,snapshot,manifest,decision_at or datetime.now(timezone.utc).isoformat(),references=references)
    root=Path(output_dir) if output_dir else resolve_project_path(cfg,cfg['runs_dir'])
    folder=root/f"universe_assessment_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:8]}"
    folder.mkdir(parents=True)
    if reference_path: result['reference_data_sha256']=hashlib.sha256(resolve_project_path(cfg,reference_path).read_bytes()).hexdigest()
    result['source_sha256']=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    target=folder/'assessment.json'; target.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    write_assessment_report(folder,result)
    return {'path':str(target.resolve()),'report':str((folder/'report.html').resolve()),'status':'complete',
            'coverage':result['universe_coverage'],'counts':result['counts']}


def write_assessment_report(folder,result):
    coverage=result['universe_coverage']; rows=result['rows']
    table=[]
    for cat,values in coverage['category_rows'].items():
        table.append([values['label'],values['rows'],values['cross_border_rows'],values['numeric_evidence_rows'],values['criterion']])
    headers=['类别','记录数','跨境','有数值证据','基本评价标准']
    def cells(values,tag='td'): return ''.join(f'<{tag}>{html.escape(str(v))}</{tag}>' for v in values)
    body='<tr>'+cells(headers,'th')+'</tr>'+''.join('<tr>'+cells(r)+'</tr>' for r in table)
    details=[]
    for row in rows:
        checks=''.join('<tr>'+cells([c['label'],c['value'] if c['value'] is not None else '待补',c['unit'],c['status'],c['rule']])+'</tr>' for c in row['criteria'])
        details.append('<details><summary>'+html.escape(f"{row['symbol']} {row['name']} | {CATALOG[row['category']][0]} | {row['history_tier']} | {row['assessment_status']}")+'</summary><table>'+checks+'</table></details>')
    page='<!doctype html><meta charset="utf-8"><title>全量ETF分类评价</title><style>body{font:15px system-ui;margin:32px auto;max-width:1200px;color:#223}table{width:100%;border-collapse:collapse;margin:15px 0}td,th{padding:8px;border-bottom:1px solid #ddd;text-align:left}th{background:#edf3fa}summary{padding:9px;cursor:pointer}p{line-height:1.7}</style>'
    page+=f'<h1>全量 ETF 分类评价</h1><p>特征日 {result["feature_date"]}。输入 {coverage["input_records"]} 条，评价 {coverage["assessed_records"]} 条，遗漏 {coverage["dropped_records"]} 条。</p>'
    page+='<p>覆盖的是输入行情板全量记录；尚未独立核验交易所名录完整性。每类都有规则，不代表每项数据齐全或收益预测已经验证。债券和货币按各自收入来源评价；数据缺失保留待补。</p><table>'+body+'</table>'
    page+=f'<p>规则评价全量；agents 上限 {result["agent_limit"]}，预测层上限 {result["prediction_limit"]}。15只仅为旧长期研究样本池。</p><h2>逐只评价</h2>'+''.join(details)
    (folder/'report.html').write_text(page,encoding='utf-8')
    with (folder/'coverage.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f); writer.writerow(headers); writer.writerows(table)


def assessment_binding(cfg,screen):
    path=cfg.get('workflow',{}).get('universe_assessment_path')
    if not path: return None
    raw=Path(path).read_bytes(); report=json.loads(raw)
    if (report['source_snapshot']['sha256']!=screen['source_snapshot']['sha256'] or
            stamp(report['decision_at_utc'])>stamp(screen['decision_at_utc'])):
        raise ValueError('Universe assessment does not match this screening decision')
    return {'path':str(Path(path).resolve()),'sha256':hashlib.sha256(raw).hexdigest()}


def candidate_universe_evidence(cfg,symbol,screen):
    # Prefer the frozen binding so a later, independent run-agents invocation
    # receives exactly the original observations, never a newer daily report.
    binding=screen.get('universe_assessment') or {}
    path=binding.get('path') or cfg.get('workflow',{}).get('universe_assessment_path')
    if not path: return {'status':'not_bound'}
    raw=Path(path).read_bytes()
    if binding.get('sha256') and hashlib.sha256(raw).hexdigest()!=binding['sha256']:
        raise ValueError('Frozen universe assessment hash mismatch')
    report=json.loads(raw)
    if (report['source_snapshot']['sha256']!=screen['source_snapshot']['sha256'] or
            stamp(report['decision_at_utc'])>stamp(screen['decision_at_utc'])):
        return {'status':'not_available_for_frozen_decision'}
    row=next((r for r in report['rows'] if r['symbol']==symbol),None)
    if row is None: raise ValueError('Full-universe assessment omitted frozen candidate')
    return {'status':'available','algorithm_version':report['algorithm_version'],
            **{k:row[k] for k in ('category','market_scope','assessment_status','history_tier','quote_state',
               'criteria','missing_criteria','history_sha256','price_provenance','return_forecast')},
            'note':'Category-specific observations and missing evidence, not a calibrated return forecast.'}
