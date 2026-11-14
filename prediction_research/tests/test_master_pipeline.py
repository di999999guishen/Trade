import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from prediction_research.master_pipeline import inspect_cycle, pipeline_lock, main
from prediction_research.news_handoff import news_for_candidate, validate_screen_date
from prediction_research.screen_research import price_features, rank_candidates


def test_stale_screen_cannot_be_run_as_current():
    screen = {"selected": [{"quote_epoch": datetime(2026, 10, 30, 7, tzinfo=timezone.utc).timestamp()}]}
    with pytest.raises(ValueError, match="stale"):
        validate_screen_date(screen, {}, datetime(2026, 11, 5, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="differs"):
        validate_screen_date(screen, {"workflow": {"expected_feature_date": "2026-10-29"}}, datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    assert validate_screen_date(screen, {}, datetime(2026, 10, 30, 12, tzinfo=timezone.utc)) == '2026-10-30'


def test_news_availability_relevance_dedup_and_price_dependency(tmp_path):
    event = {"id": 1, "time": "2026-11-04 19:00", "title": "银行测试新闻", "sentiment_src": "market"}
    future = {**event, "id": 2, "time": "2026-11-04 23:00"}
    path = tmp_path / 'news.json'
    path.write_text(json.dumps({"generated_at": "2026-11-04 20:00:00", "cls_telegraph": {
        "etfs": [{"ticker": "512800.SS", "strong_news": [event, event, future]}],
        "events": [event]}}), encoding='utf-8')
    result = news_for_candidate(path, '512800.SS', '2026-11-04T21:00:00+08:00')
    assert len(result['events']) == 1
    assert result['rejected_events'] == 1
    assert result['independent_text_events'] == 0
    assert result['price_derived_events'] == 1
    assert not result['scoring_eligible']
    assert news_for_candidate(path, '510050.SS', '2026-11-04T21:00:00+08:00')['status'] == 'no_matching_events'
    assert news_for_candidate(path, '512800.SS', '2026-11-04T19:00:00+08:00')['status'] == 'future'
    assert news_for_candidate(path, '512800.SS', '2026-11-05T21:00:00+08:00')['status'] == 'stale'
    mapped = news_for_candidate(path, '515290.SS', '2026-11-04T21:00:00+08:00', candidate_name='银行ETF天弘')
    assert mapped['events'][0]['via'] == 'candidate_name_theme'


def test_features_do_not_read_future_prices():
    start = date(2025, 1, 1)
    bars = [SimpleNamespace(trading_date=start+timedelta(days=i), close=100+i) for i in range(80)]
    expected = price_features(bars, start+timedelta(days=60))
    for b in bars[61:]:
        b.close = 999999
    assert price_features(bars, start+timedelta(days=60)) == expected
    assert price_features(bars[:60], start+timedelta(days=59)) is None


def test_incomplete_or_wrong_cycle_handoff_is_rejected(tmp_path):
    path = tmp_path / 'batch.json'
    payload = {"outcome": "complete", "artifacts": {"screen": str(tmp_path/'screen.json'), "tradingagents_batch": str(path)}}
    batch = {"trade_date": "2026-11-04", "screen_report": payload['artifacts']['screen'], "candidates": [{}], "items": [{"status": "failed"}]}
    path.write_text(json.dumps(batch))
    with pytest.raises(ValueError, match='incomplete'):
        inspect_cycle(payload, '2026-11-04')
    batch['items'][0]['status'] = 'ok'
    path.write_text(json.dumps(batch))
    assert inspect_cycle(payload, '2026-11-04')['trade_date'] == '2026-11-04'
    with pytest.raises(ValueError, match='date'):
        inspect_cycle(payload, '2026-11-05')


def test_single_run_lock_is_released(tmp_path):
    with pipeline_lock(tmp_path):
        with pytest.raises(OSError):
            with pipeline_lock(tmp_path):
                pass
    with pipeline_lock(tmp_path):
        pass


def test_dry_run_has_no_side_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr('prediction_research.master_pipeline.load_config', lambda _: {
        '_project_dir': str(tmp_path), 'tradingagents': {'project_dir': '.'},
        'runs_dir': 'runs', 'workflow': {'agent_top_n': 10, 'prediction_top_n': 20}})
    assert main(['--date', '2026-11-04', '--dry-run']) == 0
    assert not (tmp_path / 'runs').exists()
    assert 'news_flow' in capsys.readouterr().out


def test_pipeline_full_handoff_and_resume(tmp_path, monkeypatch):
    from prediction_research import master_pipeline as master
    project = tmp_path / 'project'
    project.mkdir()
    runs = project / 'runs'
    runs.mkdir()
    reports = project / 'reports'
    reports.mkdir()
    cfg = {'_project_dir': str(project), 'tradingagents': {'project_dir': '.'},
           'runs_dir': 'runs', 'workflow': {'agent_top_n': 10, 'prediction_top_n': 20}}
    monkeypatch.setattr(master, 'load_config', lambda _: cfg)
    calls = []
    def fake_execute(command, project, log, timeout):
        calls.append(command)
        if 'news_aggregator/run_flow.py' in command:
            (reports/'news_flow_2026-11-04.json').write_text(json.dumps({'generated_at': datetime.now(timezone.utc).isoformat()}))
        elif 'backfill' in command:
            (runs/'backfill_test.json').write_text('{"outcome":"complete"}')
        elif 'cycle' in command:
            effective = json.loads(Path(command[command.index('--config')+1]).read_text())
            assert effective['workflow']['expected_feature_date'] == '2026-11-04'
            assert Path(effective['workflow']['news_context_path']).exists()
            (runs/'screen.json').write_text('{}')
            trader = runs/'agent/3_trading/trader.md'
            trader.parent.mkdir(parents=True)
            trader.write_text('**Entry Price**: 1.0\n\n**Stop Loss**: 0.9')
            batch = {'trade_date':'2026-11-04','screen_report':str(runs/'screen.json'),
                     'candidates':[{}], 'items':[{'status':'ok','ticker':'510050.SS','decision':'Hold','report_dir':str(runs/'agent')}]}
            (runs/'batch.json').write_text(json.dumps(batch))
            (runs/'cycle_test.json').write_text(json.dumps({'outcome':'complete', 'artifacts': {
                'screen':str(runs/'screen.json'),'tradingagents_batch':str(runs/'batch.json'),'report':'report.md'}}))
        elif 'daily_predict.py' in command:
            assert command[command.index('--analysis-date')+1] == '2026-11-04'
            assert '--news-file' in command
            dest = Path(command[command.index('--results-dir')+1])
            dest.mkdir()
            (dest/'holding.json').write_text(json.dumps({'ticker':'510050.SS','analysis_date':'2026-11-04','decision':'Hold','result':{}}))
        else:
            raise AssertionError(command)
    monkeypatch.setattr(master, 'execute', fake_execute)
    assert main(['--date','2026-11-04']) == 0
    folder = next((runs/'pipelines').glob('2026-11-04_*'))
    manifest = json.loads((folder/'manifest.json').read_text())
    assert manifest['outcome'] == 'complete'
    assert len(calls) == 4
    # 2026-11-13 用户指令：四只自选持仓/跟踪池必须先于粗筛备选。
    stages = ['news' if 'news_aggregator/run_flow.py' in c else
              'backfill' if 'backfill' in c else
              'holdings' if 'daily_predict.py' in c else 'cycle'
              for c in calls]
    assert stages == ['news', 'backfill', 'holdings', 'cycle'], stages
    assert (folder/'briefing.json').exists()
    assert main(['--date','2026-11-04','--resume',str(folder)]) == 0
    assert len(calls) == 4
    (folder/'news.json').write_text('{}')
    assert main(['--date','2026-11-04','--resume',str(folder)]) == 2
    assert len(calls) == 4


def test_news_loader_rejects_stale_and_unknown_times(tmp_path):
    from news_aggregator.report_flow import load_latest_context
    path = tmp_path / 'news.json'
    path.write_text(json.dumps({'generated_at':'2020-01-01 12:00:00','cls_telegraph':{}}))
    assert '不可用' in load_latest_context(source_path=str(path))
    path.write_text('{}')
    assert '不可用' in load_latest_context(source_path=str(path))


def test_one_constituent_can_map_to_multiple_etfs(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'news_aggregator'))
    from news_aggregator.flows.cls_telegraph_flow import _match_etfs
    item = {'_title':'公司公告','stock_list':[{'StockID':'sh600000','name':'测试'}]}
    hits = _match_etfs(item, {'ONE.SS': {'600000'}, 'TWO.SS': {'600000'}})
    assert {r['ticker'] for r in hits} == {'ONE.SS','TWO.SS'}


def test_failed_stage_is_recorded_and_blocks_downstream(tmp_path, monkeypatch):
    from prediction_research import master_pipeline as master
    monkeypatch.setattr(master, 'load_config', lambda _: {
        '_project_dir': str(tmp_path), 'tradingagents': {'project_dir': '.'},
        'runs_dir': 'runs', 'workflow': {}})
    calls = []
    def fail(*args):
        calls.append(args)
        raise RuntimeError('simulated provider failure')
    monkeypatch.setattr(master, 'execute', fail)
    assert main(['--date','2026-11-04']) == 2
    path = next((tmp_path/'runs/pipelines').glob('*/manifest.json'))
    result = json.loads(path.read_text())
    assert result['outcome'] == 'failed'
    assert result['steps']['news']['status'] == 'failed'
    assert len(calls) == 1
