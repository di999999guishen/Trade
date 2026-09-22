import json

import pandas as pd
import pytest

from quant_research.sec_financial_v2 import enrich, reconstruct_v2, selected_asof, ttm


def fact(metric, start, end, value, acc='first', available='2024-02-01T14:30:00Z'):
    return {'security_id': 'CIK0000000001', 'metric': metric, 'period_start': start,
            'period_end': end, 'value': value, 'unit': 'USD', 'revision_id': acc,
            'reconstructed_available_at': pd.Timestamp(available), 'form': '10-K', 'available_at': None}


def test_four_contiguous_quarters_and_gap_refusal():
    quarters = [('2023-01-01', '2023-03-31'), ('2023-04-01', '2023-06-30'),
                ('2023-07-01', '2023-09-30'), ('2023-10-01', '2023-12-31')]
    data = pd.DataFrame([fact('GrossProfit', s, e, i+1) for i, (s, e) in enumerate(quarters)])
    enriched = enrich(data, {'reviews': []})
    selected = selected_asof(enriched, '2024-03-01T20:00:00Z')
    parts, reason = ttm(selected, pd.Timestamp('2023-12-31'))
    assert reason is None and sum(r['value']*c for r, c in parts) == 10
    assert ttm(selected.iloc[1:], pd.Timestamp('2023-12-31'))[0] is None
    # An inconsistent annual must not silently supersede four exact quarters.
    data = pd.concat([data, pd.DataFrame([fact('GrossProfit', '2023-01-01', '2023-12-31', 99)])])
    selected = selected_asof(enrich(data, {'reviews': []}), '2024-03-01T20:00:00Z')
    assert ttm(selected, pd.Timestamp('2023-12-31'))[1] == 'inconsistent_ttm_paths_or_reporting_bases'


def test_same_period_required_and_algebraic_lineage():
    data = pd.DataFrame([fact('Assets', None, '2023-12-31', 100),
                         fact('SalesRevenueNet', '2023-01-01', '2023-12-31', 50),
                         fact('CostOfGoodsAndServicesSold', '2023-01-01', '2023-12-31', 30),
                         fact('NetIncomeLoss', '2022-01-01', '2022-12-31', 5)])
    panel = reconstruct_v2(enrich(data, {'reviews': []}), '2024-03-01T20:00:00Z').set_index('field')
    assert panel.loc['gross_profit_ttm', 'value'] == 20
    sources = json.loads(panel.loc['gross_profit_ttm', 'sources'])
    assert [s['coefficient'] for s in sources] == [1, -1]
    assert pd.isna(panel.loc['net_income_ttm', 'value'])


def test_alias_requires_review_and_later_revision_invalidates_derived_fact():
    tag = 'NetCashProvidedByUsedInOperatingActivitiesContinuingOperations'
    rows = [fact('Assets', None, '2023-12-31', 100), fact(tag, '2023-01-01', '2023-12-31', 10)]
    policy = {'reviews': [{'cik': 1, 'accession': 'first', 'url': 'fixture', 'aliases': {
        tag: 'NetCashProvidedByUsedInOperatingActivities'}, 'anchors': [
        {'metric': tag, 'end': '2023-12-31', 'value': 10, 'annual': True}]}]}
    no_review = reconstruct_v2(enrich(pd.DataFrame(rows), {'reviews': []}), '2024-03-01T20:00:00Z')
    assert pd.isna(no_review.set_index('field').loc['cash_flow_ttm', 'value'])
    rows.append(fact(tag, '2023-01-01', '2023-12-31', 999, 'future', '2024-04-01T14:30:00Z'))
    expanded = enrich(pd.DataFrame(rows), policy)
    before = reconstruct_v2(expanded, '2024-03-01T20:00:00Z').set_index('field')
    after = reconstruct_v2(expanded, '2024-05-01T20:00:00Z').set_index('field')
    assert before.loc['cash_flow_ttm', 'value'] == 10
    assert pd.isna(after.loc['cash_flow_ttm', 'value'])
    policy['reviews'][0]['anchors'][0]['value'] = 11
    with pytest.raises(ValueError, match='anchor mismatch'):
        enrich(pd.DataFrame(rows), policy)


def test_debt_missing_component_not_zero_and_no_cross_accession_mix():
    rows = [fact('Assets', None, '2023-12-31', 100), fact('LongTermDebtCurrent', None, '2023-12-31', 10),
            fact('LongTermDebtNoncurrent', None, '2023-12-31', 20),
            fact('CommercialPaper', None, '2023-12-31', 3, 'other')]
    review = {'reviews': [{'cik': 1, 'accession': 'first', 'url': 'fixture', 'aliases': {}, 'anchors': [],
              'debt_formulas': {'TotalDebtVerified': {'LongTermDebtCurrent': 1, 'LongTermDebtNoncurrent': 1,
                                                    'CommercialPaper': 1}}}]}
    def value(data):
        return reconstruct_v2(enrich(pd.DataFrame(data), review), '2024-03-01T20:00:00Z').set_index('field').loc['total_debt', 'value']
    assert pd.isna(value(rows))
    rows[-1]['revision_id'] = 'first'
    assert value(rows) == 33
