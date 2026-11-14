import hashlib,json
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from prediction_research.data import Bar,SeriesSnapshot
from prediction_research.taxonomy import classify_etf,market_scope,screen_group
from prediction_research.universe_assessment import assess_rows,CATALOG,candidate_universe_evidence,history_metrics,reference_metric,assessment_binding
from prediction_research.tests.test_evidence import cfg


def raw(symbol,name,amount=1e8,**kwargs):
    return {'symbol':symbol,'name':name,'exchange':'SSE','price':1.0,'amount':amount,'market_cap':1e9,
            'quote_epoch':datetime(2024,8,1,7,tzinfo=timezone.utc).timestamp(),**kwargs}


def assess(cfg,records,**kwargs):
    return assess_rows(cfg,{'records':records,'source_market_total':len(records)},
        {'sha256':'a'*64,'retrieved_at_utc':'2024-08-01T08:00:00+00:00'},'2024-08-01T09:00:00+00:00',
        history_loader=kwargs.pop('history_loader',lambda s,d:(None,())),flow_history={},**kwargs)


@pytest.mark.parametrize('symbol,name,asset',[
    ('159956','创业板F','equity'),('159998','计算机','equity'),('158000','港股金融','equity'),
    ('159003','招商快线','money'),('159005','添富快钱','money'),('159980','有色ETF','commodity'),
    ('x','股债平衡ETF','multi_asset'),('x','科创债ETF','bond'),('x','黄金股ETF','equity')])
def test_board_short_names_and_special_asset_identities(symbol,name,asset):
    assert classify_etf(name,etf_board=True,symbol=symbol)['asset_class']==asset
    assert market_scope('港低波GF')=='cross_border'


def test_all_categories_receive_rules_even_without_history_or_flow(cfg):
    names=['沪深300ETF','银行ETF','机器人ETF','红利ETF','主动ETF','国债ETF','政金债ETF','公司债ETF',
           '可转债ETF','综合债ETF','黄金ETF','能源化工ETF','原油ETF','货币ETF','股债ETF','杠杆ETF','未识别名称']
    rows=[raw(str(100000+i),n,amount=0 if i%2 else 1e8) for i,n in enumerate(names)]
    result=assess(cfg,rows)
    assert result['universe_coverage']['assessed_records']==len(rows)
    assert result['universe_coverage']['dropped_records']==0
    assert set(result['universe_coverage']['category_rows'])==set(CATALOG)
    assert {r['category'] for r in result['rows']}==set(CATALOG)
    assert all(r['criteria'] and r['category'] in CATALOG for r in result['rows'])
    money=next(r for r in result['rows'] if r['asset_class']=='money')
    assert money['assessment_status']=='income_data_missing'
    assert 'yield_7d_pct' in money['missing_criteria']
    assert not any(c['id']=='return20_pct' for c in money['criteria'])
    assert money['return_forecast'] is None


def test_missing_or_future_quote_is_retained_and_not_ranked_as_current(cfg):
    future=raw('1','银行ETF',quote_epoch=datetime(2024,8,2,7,tzinfo=timezone.utc).timestamp())
    missing=raw('2','国债ETF',quote_epoch=None)
    result=assess(cfg,[future,missing])
    assert len(result['rows'])==2
    assert {r['quote_state'] for r in result['rows']}=={'future_quote','missing_quote_time'}
    assert all(r['comparison_value'] is None for r in result['rows'])


def test_short_history_uses_short_rule_without_fabricating_long_signal(cfg):
    bars=tuple(Bar(date(2024,7,27)+timedelta(days=i),100+i,101+i,99+i,100+i,1e6,1e8) for i in range(6))
    series=SeriesSnapshot('1',bars,'test.csv','history')
    result=assess(cfg,[raw('1','银行ETF')],history_loader=lambda *a:(series,bars))
    row=result['rows'][0]
    assert row['history_tier']=='history_5d' and row['metrics']['return5_pct']==pytest.approx(5)
    assert row['metrics'].get('return20_pct') is None
    assert 'return20_pct' in row['missing_criteria']


def test_reference_data_requires_source_and_first_availability():
    decision=datetime(2024,8,1,9,tzinfo=timezone.utc); asof=date(2024,8,1)
    good={'value':1.8,'available_at_utc':'2024-08-01T08:00:00Z','asof_date':'2024-08-01','source_url':'https://example.test/disclosure'}
    refs={'1':{'yield_7d_pct':good}}
    assert reference_metric(refs,'1','yield_7d_pct',decision,asof)[0]==1.8
    good['available_at_utc']='2024-08-02T08:00:00Z'
    assert reference_metric(refs,'1','yield_7d_pct',decision,asof)[1]=='future_reference'


def test_future_snapshot_and_duplicate_records_fail_explicitly(cfg):
    with pytest.raises(ValueError,match='Duplicate'):
        assess(cfg,[raw('1','ETF'),raw('1','ETF')])
    with pytest.raises(ValueError,match='unavailable'):
        assess_rows(cfg,{'records':[raw('1','ETF')]},{'retrieved_at_utc':'2024-08-02T09:00:00Z'},'2024-08-01T09:00:00Z')
    with pytest.raises(ValueError,match='count mismatch'):
        assess_rows(cfg,{'records':[raw('1','ETF')],'source_market_total':2},
                    {'retrieved_at_utc':'2024-08-01T08:00:00Z'},'2024-08-01T09:00:00Z')


def test_candidate_evidence_cannot_attach_later_or_different_snapshot(cfg,tmp_path):
    report=assess(cfg,[raw('1','银行ETF')]); path=tmp_path/'assessment.json'
    path.write_text(json.dumps(report),encoding='utf-8'); cfg['workflow']['universe_assessment_path']=str(path)
    screen={'source_snapshot':{'sha256':'a'*64},'decision_at_utc':'2024-08-01T09:00:00+00:00'}
    assert candidate_universe_evidence(cfg,'1',screen)['status']=='available'
    screen['decision_at_utc']='2024-08-01T08:30:00+00:00'
    assert candidate_universe_evidence(cfg,'1',screen)['status']=='not_available_for_frozen_decision'


def test_sina_repeated_page_fails_and_absent_flow_date_stays_unknown(monkeypatch):
    from prediction_research.adapters import sina_etf
    page=[{'symbol':f'sz{i:06d}'} for i in range(100)]
    monkeypatch.setattr(sina_etf,'_get',lambda *a,**k:json.dumps(page).encode())
    with pytest.raises(RuntimeError,match='repeated'):
        sina_etf.fetch_etf_list()
    assert sina_etf._session_epoch({})==(None,None)


def test_cycle_produces_all_record_assessment_before_filtering(cfg,monkeypatch):
    from prediction_research.tests.test_cycle_remediation import snapshot,flow_row
    from prediction_research.cycle import run_cycle
    rows=[flow_row(),{**flow_row('511010'),'name':'国债ETF'},{**flow_row('511990'),'name':'货币ETF'}]
    snapshot(cfg,rows)
    cfg['workflow']['skip_tradingagents']=True
    cfg['universe_assessment']={'enabled':True}
    monkeypatch.setattr('prediction_research.cycle.now_utc',lambda:'2024-08-17T12:00:00+00:00')
    result=run_cycle(cfg,2,True)
    assert result['universe_coverage']['assessed_records']==3
    report=json.loads(Path(result['artifacts']['universe_assessment']).read_text(encoding='utf-8'))
    assert {r['asset_class'] for r in report['rows']}=={'commodity','bond','money'}
    assert result['candidate_symbols']==['518880']
    screen=json.loads(Path(result['artifacts']['screen']).read_text(encoding='utf-8'))
    # A separate scheduled agents process has only the frozen screen, not the
    # cycle's in-memory runtime config. The original evidence must survive.
    assert candidate_universe_evidence({},'518880',screen)['status']=='available'


def test_cycle_blocks_candidate_selection_when_full_assessment_fails(cfg,monkeypatch):
    from prediction_research.tests.test_cycle_remediation import snapshot,flow_row
    from prediction_research.cycle import run_cycle
    snapshot(cfg,[flow_row()]); cfg['universe_assessment']={'enabled':True}
    cfg['workflow']['skip_tradingagents']=True
    monkeypatch.setattr('prediction_research.cycle.now_utc',lambda:'2024-08-17T12:00:00+00:00')
    def failed(*a,**k): raise ValueError('universe count mismatch')
    monkeypatch.setattr('prediction_research.universe_assessment.run_universe_assessment',failed)
    result=run_cycle(cfg,2,True)
    assert result['candidate_symbols']==[]
    assert any(s['name']=='screen_etfs' and s.get('reason')=='current_universe_assessment_failed' for s in result['steps'])
    assert result['outcome']=='partial_failure'


@pytest.mark.parametrize('name,subtype',[
    ('消费50ETF','sector'),('科创芯片ETF','thematic'),('科创50ETF','broad_market'),
    ('上证医药ETF','sector'),('中证500增强ETF','strategy_factor')])
def test_specific_exposure_precedes_exchange_and_index_prefix(name,subtype):
    assert classify_etf(name,etf_board=True)['subtype']==subtype


def test_short_names_do_not_create_duplicate_index_exposures_or_false_cross_border():
    assert {screen_group(n,'broad_market') for n in ('A500ETF华夏','A500E','A500中证')}=={'index:中证A500'}
    assert screen_group('沪深300ETF','broad_market')==screen_group('300ETF','broad_market')
    assert screen_group('GZ2000','broad_market')!=screen_group('CSI2000','broad_market')
    assert market_scope('港口ETF')=='domestic_or_unverified'
    assert market_scope('港股通ETF')=='cross_border'
    assert classify_etf('杠杆ETF',etf_board=True)['asset_class']=='special'


def test_income_categories_do_not_use_price_momentum_ranking(cfg):
    bars=tuple(Bar(date(2024,7,12)+timedelta(days=i),100+i,101+i,99+i,100+i,1e6,1e8) for i in range(21))
    series=SeriesSnapshot('1',bars,'test.csv','history')
    result=assess(cfg,[raw('1','国债ETF'),raw('2','货币ETF'),raw('3','银行ETF')],history_loader=lambda *a:(series,bars))
    assert [r['comparison_basis'] for r in result['rows']]==['liquidity_only','liquidity_only','return20_pct']
    equity=result['rows'][-1]
    checks={c['id']:c for c in equity['criteria']}
    assert checks['trend_efficiency20']['value']==pytest.approx(1)
    assert checks['amount_ratio20']['value']==pytest.approx(1)
    assert checks['distance_ma20']['value']>0
    assert 'relative_strength20' in equity['missing_criteria']


def test_frozen_report_hash_is_verified_and_new_report_does_not_replace_it(cfg,tmp_path):
    report=assess(cfg,[raw('1','银行ETF')]); path=tmp_path/'assessment.json'
    path.write_text(json.dumps(report),encoding='utf-8'); cfg['workflow']['universe_assessment_path']=str(path)
    screen={'source_snapshot':{'sha256':'a'*64},'decision_at_utc':'2024-08-01T09:00:00+00:00'}
    screen['universe_assessment']=assessment_binding(cfg,screen)
    assert candidate_universe_evidence({'workflow':{'universe_assessment_path':'unrelated'}},'1',screen)['status']=='available'
    path.write_text(json.dumps({**report,'decision_at_utc':'2024-08-02T09:00:00Z'}),encoding='utf-8')
    with pytest.raises(ValueError,match='hash mismatch'):
        candidate_universe_evidence({},'1',screen)
