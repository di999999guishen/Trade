import copy
from dataclasses import replace
from datetime import date,timedelta

import pytest

from prediction_research.data import Bar
from prediction_research.return_screen import select_return_candidates,load_return_contract
from prediction_research.return_research import cache_panel,choose_from_history,simulate_return
from prediction_research.screen_factors import features_asof,load_contract,select_profile
from prediction_research.screen_portfolio import simulate
from prediction_research.tests.test_evidence import cfg


def row(symbol,score,asset_class='equity'):
    return {'symbol':symbol,'name':symbol,'asset_class':asset_class,'scope':'CN','exposure':symbol,
            'features':{'adv20':1e8,'momentum20':score,'momentum60':score,'momentum5':0,'trend60':score}}


def test_global_ranking_does_not_promote_weak_asset_class_leader():
    base=load_contract(); base['profiles']['multi_factor']={'momentum20':1}
    rows=[row('a',.3),row('b',.2),row('c',-.2,'commodity')]
    global_plan=select_return_candidates(rows,{'score':'global_multifactor','top_k':2},base)
    old=select_profile(rows,'multi_factor',{**base,'top_k':2})
    assert list(global_plan['weights'])==['a','b']
    assert list(old['weights'])==['a','c']


def test_concentration_changes_allocation_without_changing_ranking():
    rows=[row('a',.3),row('b',.2),row('c',.1)]
    two=select_return_candidates(rows,{'score':'momentum20','top_k':2},load_contract())
    five=select_return_candidates(rows,{'score':'momentum20','top_k':5},load_contract())
    assert two['ranked_symbols']==five['ranked_symbols']
    assert two['weights']=={'a':.5,'b':.5}
    assert sum(five['weights'].values())==pytest.approx(.6)


def test_skip_recent_runup_and_unknown_liquidity():
    a,b,c=row('a',.2),row('b',.1),row('c',1)
    a['features']['momentum5']=.3; c['features']['adv20']=None
    plan=select_return_candidates([a,b,c],{'score':'momentum20_skip5','top_k':2,'positive_only':True},load_contract())
    assert plan['weights']=={'b':.5}


def test_history_choice_never_observes_future_and_demands_closed_window():
    start=date(2021,1,1)
    histories={s:[{'date':str(start+timedelta(days=i)),'nav':100*(1+rate)**i} for i in range(700)] for s,rate in [('a',.001),('b',.002)]}
    candidates=[{'id':'a'},{'id':'b'}]; cutoff=start+timedelta(days=550)
    chosen,scores=choose_from_history(histories,candidates,cutoff,504)
    assert chosen=='b' and all(s['training_end']==str(cutoff) for s in scores)
    assert all(s['closed_return_intervals']==504 for s in scores)
    for r in histories['a'][551:]: r['nav']=1e20
    assert choose_from_history(histories,candidates,cutoff,504)==(chosen,scores)
    with pytest.raises(ValueError,match='Insufficient'):
        choose_from_history(histories,candidates,start+timedelta(days=400),504)


def market():
    base=load_contract(); base['assets']=[a for a in base['assets'] if a['symbol'] in {'510300','518880'}]
    series={}
    for sym in ('510300','518880'):
        series[sym]=[Bar(date(2023,1,1)+timedelta(days=i),100+i,101+i,99+i,100+i,1e6,1e8) for i in range(170)]
    calendar=[b.trading_date for b in series['510300'][126:]]
    prices={s:{b.trading_date:b for b in bars} for s,bars in series.items()}
    features={s:{d:features_asof(bars,d) for d in calendar} for s,bars in series.items()}
    returns={s:{b.trading_date:b.close/a.close-1 for a,b in zip(bars,bars[1:])} for s,bars in series.items()}
    return base,calendar,prices,features,returns


def test_execution_matches_previous_engine_exactly():
    base,calendar,prices,features,returns=market()
    panel=cache_panel(calendar,prices,features,base)
    strategy=load_return_contract()['strategies'][0]
    new=simulate_return(panel,base,strategy,15)
    old=simulate(calendar,prices,features,returns,base,'momentum20',20,15)
    assert new[0]['full']==old[0]['full']
    assert new[2]==old[2]
    assert [{k:v for k,v in r.items() if k!='commodity_exposure'} for r in new[1]]==old[1]


def test_future_price_changes_do_not_change_prior_signals_or_nav():
    base,calendar,prices,features,_=market()
    first=cache_panel(calendar,prices,features,base)
    strategy=load_return_contract()['strategies'][2]
    result=simulate_return(first,base,strategy,15)
    changed=copy.deepcopy(first)
    for d in calendar[21:]:
        changed['prices']['510300'][d]=replace(changed['prices']['510300'][d],close=1000)
        changed['closes'][d]['510300']=1000
        changed['rows'][d][0]['features']['momentum20']=10
    other=simulate_return(changed,base,strategy,15)
    cutoff=str(calendar[21])
    assert [r for r in result[3] if r['signal_date']<cutoff]==[r for r in other[3] if r['signal_date']<cutoff]
    assert result[1][:21]==other[1][:21]


def test_rule_switch_uses_own_cash_ledger_and_next_session():
    base,calendar,prices,features,_=market()
    panel=cache_panel(calendar,prices,features,base)
    first={'id':'first','score':'momentum20','top_k':2,'cadence':20}
    second={'id':'second','score':'benchmark_hold','top_k':1,'cadence':20}
    result,nav,fills,targets=simulate_return(panel,base,first,15,schedule={calendar[3]:second})
    assert result['ledger']['passed']
    assert targets[1]['signal_date']==str(calendar[3]) and targets[1]['strategy_id']=='second'
    assert any(f['date']==str(calendar[4]) and f['symbol']=='518880' and f['notional']<0 for f in fills)
    assert nav[4]['cash']>=-1e-6


def test_price_channel_keeps_missing_flow_unknown_and_preserves_production(cfg):
    from prediction_research.return_screen import price_eligible_records
    from prediction_research.screening import select_etfs
    from prediction_research.screen_shadow import build_shadow
    from prediction_research.tests.test_cycle_remediation import flow_row
    from prediction_research.data import SeriesSnapshot
    quote=flow_row(net=None,pct=None)
    rules=cfg['etf_market']['screen']
    eligible,_=select_etfs([quote],rules)
    assert eligible==[]
    price_rows=price_eligible_records([quote],rules)
    assert len(price_rows)==1 and price_rows[0]['main_net_inflow'] is None
    base,calendar,prices,features,_=market()
    # Exact date/history and real turnover remain mandatory in the price channel.
    history=[Bar(date(2023,1,1)+timedelta(days=i),100+i,101+i,99+i,100+i,1e6,1e8) for i in range(170)]
    cache={'518880':SeriesSnapshot('518880',tuple(history),'fixture.csv','test')}
    result=build_shadow(cfg,[],str(history[-1].trading_date),10,cache,price_rows)
    assert result['profiles']['current']['weights']=={}
    assert result['profiles']['return_m20_k2_r20']['weights']=={'518880':.5}
    assert result['return_candidate_lists']['return_m20_k2_r20']['candidate_limit']==10
    assert result['coverage']['additional_price_channel_symbols']==['518880']
    assert not result['production_ranking_changed']
