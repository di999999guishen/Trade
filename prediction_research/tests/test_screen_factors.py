from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json

import pytest

from prediction_research.data import Bar
from prediction_research.screen_factors import features_asof, load_contract, select_profile, exposure_info
from prediction_research.screen_portfolio import Portfolio, simulate, audit_ledger
from prediction_research.price_provenance import read_price_metadata, write_price_metadata
from prediction_research.screen_shadow import candidate_factor_evidence, shadow_from_frozen_screen
from prediction_research.tests.test_evidence import cfg
from prediction_research.tests.test_cycle_remediation import snapshot
from prediction_research.screening import screen_etfs


def bars(count=150):
    return [Bar(date(2023,1,1)+timedelta(days=i),100+i,101+i,99+i,100+i,1e6,1e8) for i in range(count)]


def row(symbol, history=None, **kwargs):
    return dict(symbol=symbol,name=symbol,asset_class='equity',scope='CN',exposure=symbol,
                features=features_asof(history or bars(), (history or bars())[-1].trading_date),**kwargs)


def test_features_cannot_see_future_and_require_current_bar():
    history=bars()
    expected=features_asof(history[:130],history[129].trading_date)
    future=[replace(b,close=b.close*10) for b in history[130:]]
    assert features_asof(history[:130]+future,history[129].trading_date)==expected
    assert features_asof(history[:129],history[129].trading_date) is None


def test_missing_amount_and_split_are_unavailable():
    history=bars(); history[-1]=replace(history[-1],amount=0)
    f=features_asof(history,history[-1].trading_date)
    assert f['adv20'] is None and f['log_adv20'] is None
    history[-1]=replace(history[-1],close=history[-2].close/2)
    assert features_asof(history,history[-1].trading_date) is None


def test_distinct_names_for_same_index_share_exposure():
    contract=load_contract()
    a=exposure_info({'symbol':'510320','name':'沪深300ETF'},contract)
    b=exposure_info({'symbol':'159300','name':'沪深300ETF基金'},contract)
    assert a['exposure']==b['exposure']=='CSI300'
    assert a['mapping_basis']=='name_heuristic_unverified'


def test_correlated_candidates_do_not_fill_slots_by_force():
    contract=load_contract(); contract['top_k']=2
    rows=[row('a'),row('b')]
    returns={s:{date(2023,1,1)+timedelta(days=i):.001*(i%5) for i in range(61)} for s in ('a','b')}
    result=select_profile(rows,'diversified',contract,returns)
    assert len(result['weights'])==1 and sum(result['weights'].values())==.5
    assert result['rejected'][0]['reason']=='high_correlation'


def test_unknown_liquidity_and_downtrend_leave_cash():
    contract=load_contract()
    a,b=row('a'),row('b')
    a['features']['adv20']=None; b['features']['trend60']=-.1
    assert select_profile([a,b],'diversified',contract)['weights']=={}


def trade(portfolio,target,day=2,opens=None,previous=None,adv=None,buffer=0,cost=100,capacity=1):
    return portfolio.rebalance(target,opens or {'a':10,'b':20}, previous or {'a':10,'b':20},
           adv or {'a':1e6,'b':1e6},{'a':.1,'b':.1},cost,capacity,buffer,date(2023,1,day),date(2023,1,day-1))


def test_full_investment_fee_budget_then_exit_even_with_buffer():
    p=Portfolio(1000)
    trade(p,{'a':1})
    assert p.cash==pytest.approx(0,abs=1e-8)
    assert p.units['a']==pytest.approx(1000/1.01/10)
    assert p.fills[0]['cost']==pytest.approx(1000/1.01*.01)
    trade(p,{},day=3,buffer=1)
    assert not p.units
    assert p.cash==pytest.approx(1000/1.01*.99)


def test_limit_and_lagged_capacity_restrictions():
    p=Portfolio(1000)
    blocks=trade(p,{'a':1},opens={'a':11,'b':20})
    assert not p.fills and blocks[0]['reason']=='open_limit_proxy'
    blocks=trade(p,{'a':1},adv={'a':100,'b':100},capacity=.1,cost=0)
    assert p.fills[-1]['notional']==pytest.approx(10)
    assert blocks[0]['reason']=='capacity_partial'
    with pytest.raises(ValueError,match='valuation'):
        p.value({})


def test_sells_fund_buys_without_borrowing():
    p=Portfolio(1000)
    trade(p,{'b':1})
    trade(p,{'a':1},day=3)
    assert [f['symbol'] for f in p.fills]==['b','b','a']
    assert p.cash>=-1e-8 and set(p.units)=={'a'}


def test_execution_is_next_open_and_ledger_detects_tampering():
    contract=load_contract(); contract.update(top_k=1,assets=[dict(symbol='510300',name='a',asset_class='equity',scope='CN',exposure='a',limit_pct=.1)],initial_cash=1000,max_participation_lagged_adv=1)
    history=bars(); calendar=[b.trading_date for b in history[126:]]
    prices={'510300':{b.trading_date:b for b in history}}
    features={'510300':{d:features_asof(history,d) for d in calendar}}
    returns={'510300':{b.trading_date:b.close/a.close-1 for a,b in zip(history,history[1:])}}
    result,nav,fills,targets=simulate(calendar,prices,features,returns,contract,'benchmark_hold',20,0)
    assert nav[0]['nav']==1000 and not nav[0]['holdings_count']
    assert fills[0]['date']==str(calendar[1])
    assert fills[0]['price']==prices['510300'][calendar[1]].open
    assert nav[-1]['nav']==pytest.approx(1000/prices['510300'][calendar[1]].open*prices['510300'][calendar[-1]].close)
    assert result['ledger']['passed']
    nav[-1]['nav']+=1
    with pytest.raises(AssertionError,match='ledger'):
        audit_ledger(nav,fills,prices,1000)


def test_signal_prefix_is_unchanged_after_future_price_perturbation():
    contract=load_contract(); contract.update(top_k=1,assets=[dict(symbol='a',name='a',asset_class='equity',scope='CN',exposure='a',limit_pct=.1)])
    history=bars(160); calendar=[b.trading_date for b in history[126:]]
    def run(h):
        prices={'a':{b.trading_date:b for b in h}}
        features={'a':{d:features_asof(h,d) for d in calendar}}
        returns={'a':{b.trading_date:b.close/a.close-1 for a,b in zip(h,h[1:])}}
        return simulate(calendar,prices,features,returns,contract,'regime_risk',5,15)
    first=run(history)
    changed=history[:145]+[replace(b,open=b.open*1.05,high=b.high*1.05,low=b.low*1.05,close=b.close*1.05) for b in history[145:]]
    second=run(changed)
    cutoff=str(history[145].trading_date)
    assert [t for t in first[3] if t['signal_date']<cutoff]==[t for t in second[3] if t['signal_date']<cutoff]
    assert [n for n in first[1] if n['date']<cutoff]==[n for n in second[1] if n['date']<cutoff]


def test_metadata_must_match_file_and_shadow_never_injects_by_default(tmp_path):
    path=tmp_path/'cache_a.csv'; path.write_text('old')
    write_price_metadata(path,provider='test',adjustment='qfq',volume_unit='shares',amount_status='available',source_url='https://example.test')
    assert read_price_metadata(path,hashlib.sha256(path.read_bytes()).hexdigest())['adjustment']=='qfq'
    path.write_text('new')
    assert read_price_metadata(path,hashlib.sha256(path.read_bytes()).hexdigest())['status']=='metadata_hash_mismatch'
    assert candidate_factor_evidence({'workflow':{'factor_shadow_path':'nonexistent'}},'a','2023-01-01')['status']=='shadow_only_not_injected'


def test_shadow_uses_frozen_source_and_rejects_tampering(cfg):
    manifest=snapshot(cfg)
    _,screen=screen_etfs(cfg,decision_at_utc='2024-08-17T12:00:00Z')
    result=shadow_from_frozen_screen(cfg,screen)
    payload=json.loads(open(result['path'],encoding='utf-8').read())
    assert payload['feature_date']=='2024-08-17'
    assert payload['source_snapshot']['sha256']==manifest['sha256']
    assert payload['coverage']['complete_factor_rows']==0
    from pathlib import Path
    Path(manifest['path']).write_text('{}')
    with pytest.raises(ValueError,match='hash mismatch'):
        shadow_from_frozen_screen(cfg,screen)


def test_forward_settlement_cannot_backdate_observation_creation(cfg,monkeypatch):
    from pathlib import Path
    from prediction_research.data import SeriesSnapshot
    from prediction_research.screen_shadow import settle_factor_shadows
    history=bars(); series=SeriesSnapshot('a',tuple(history),'a.csv','test')
    monkeypatch.setattr('prediction_research.screen_shadow.find_cache',lambda *a:series)
    runs=Path(cfg['_project_dir'])/cfg['runs_dir']; runs.mkdir(parents=True,exist_ok=True)
    frozen={'status':'shadow_only','feature_date':'2023-01-02','decision_at_utc':'2023-01-02T12:00:00+00:00',
            'created_at_utc':'2023-01-20T12:00:00+00:00','profiles':{'current':{'weights':{'a':.5}},'cash':{'weights':{}}},
            'contract':{'shadow_round_trip_cost_bps':30}}
    (runs/'screen_shadow_test.json').write_text(json.dumps(frozen))
    later={**frozen,'created_at_utc':'2023-01-22T12:00:00+00:00'}
    (runs/'screen_shadow_repeat.json').write_text(json.dumps(later))
    result=settle_factor_shadows(cfg)
    payload=json.loads(Path(result['path']).read_text())
    cohort=payload['cohorts'][0]
    assert cohort['outcomes']['a']['entry_date']=='2023-01-21'
    assert cohort['net_returns']['current']==pytest.approx(.5*(124/120-1-.003))
    assert cohort['net_returns']['cash']==0
    assert len(payload['cohorts'])==1 and len(payload['duplicate_observations_skipped'])==1
    assert not payload['production_promotion_allowed']


def test_cycle_binds_shadow_artifacts_without_changing_candidates(cfg,monkeypatch):
    from pathlib import Path
    from prediction_research.cycle import run_cycle
    from prediction_research.tests.test_evidence import write_histories
    write_histories(cfg); snapshot(cfg)
    cfg['factor_research']={'enabled':True,'mode':'shadow','inject_agent_context':False}
    cfg['workflow']['skip_tradingagents']=True
    monkeypatch.setattr('prediction_research.cycle.now_utc',lambda:'2024-08-17T12:00:00+00:00')
    result=run_cycle(cfg,2,True)
    assert result['outcome']=='complete_with_data_waits_without_llm'
    shadow=json.loads(Path(result['artifacts']['screen_factor_shadow']).read_text(encoding='utf-8'))
    screen=json.loads(Path(result['artifacts']['screen']).read_text(encoding='utf-8'))
    assert result['candidate_symbols']==[r['symbol'] for r in screen['selected'][:2]]
    assert shadow['source_snapshot']['sha256']==screen['source_snapshot']['sha256']
    assert not shadow['production_ranking_changed']
    assert Path(result['artifacts']['factor_shadow_settlement']).exists()
