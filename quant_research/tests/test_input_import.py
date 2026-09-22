from dataclasses import asdict

import pytest

from quant_research.input_import import validate_file


def borrow_document():
    return {'evidence_kind': 'synthetic_fixture', 'source_id': 'test', 'records': [
        {'evidence': {'security_id': 'TEST', 'available_shares': 10, 'annual_fee': .02,
                      'observed_at': '2026-01-02T14:00:00Z', 'available_at': '2026-01-02T14:01:00Z',
                      'expires_at': '2026-01-02T15:00:00Z', 'recalled': False, 'source_id': 'test'},
         'at': '2026-01-02T14:30:00Z', 'requested_shares': 20}]}


def test_empty_and_fixture_never_ready():
    with pytest.raises(ValueError, match='nonempty'):
        validate_file('s6', {'evidence_kind': 'real_data', 'source_id': 'empty', 'records': []})
    result = validate_file('s6', borrow_document())
    assert result['return_code'] == 3
    assert not result['strategy_ready']
    assert result['rows'][0]['result']['approved_shares'] == 10


def test_partial_failure_and_duplicate_are_failure():
    doc = borrow_document()
    doc['records'].append(doc['records'][0])
    report = validate_file('s6', doc)
    assert report['return_code'] == 2
    assert report['accepted'] == report['rejected'] == 1
    doc = borrow_document()
    doc['records'][0]['at'] = '2026-01-02T16:00:00Z'
    assert validate_file('s6', doc)['rejected'] == 1


def test_s5_import_checks_consensus_and_history():
    from test_fundamental_signals import event
    row = {'event': asdict(event()), 'history': [asdict(event(q, 2020+q//4)) for q in range(8)],
           'decision': '2024-01-06T00:00:00Z'}
    doc = {'evidence_kind': 'synthetic_fixture', 'source_id': 'test', 'records': [row]}
    assert validate_file('s5', doc)['return_code'] == 3
    row['event']['estimate_available_at'] = '2024-01-05T20:30:00Z'
    assert '60 minutes' in validate_file('s5', doc)['rows'][0]['reason']


def test_s7_import_checks_quote_age():
    from test_execution_evidence import option_inputs
    leg, quote = option_inputs()
    row = {'legs': [asdict(leg)], 'quotes': [quote], 'at': '2024-01-02T14:45:02Z',
           'nav': 100000, 'cash': 90000, 'underlying_shares': 100, 'underlying_price': 100,
           'variant': 'covered_call'}
    doc = {'evidence_kind': 'synthetic_fixture', 'source_id': 'test', 'records': [row]}
    assert validate_file('s7', doc)['return_code'] == 3
    row['at'] = '2024-01-02T14:46:02Z'
    assert 'stale' in validate_file('s7', doc)['rows'][0]['reason']
