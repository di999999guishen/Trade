import json

import pandas as pd

from quant_research.sec_reconstruction import public_versions, reconstruct


def test_ttm_bridge_and_future_revision():
    rows = []
    for start, end, value, available, revision in [
        ('2022-01-01', '2022-12-31', 100, '2023-02-01', 'annual'),
        ('2022-01-01', '2022-06-30', 40, '2023-02-01', 'prior'),
        ('2023-01-01', '2023-06-30', 60, '2023-08-01', 'current'),
        ('2023-01-01', '2023-06-30', 999, '2024-08-01', 'future')]:
        rows.append({'metric': 'NetCashProvidedByUsedInOperatingActivities', 'unit': 'USD',
            'period_start': start, 'period_end': end, 'value': value, 'form': '10-Q', 'revision_id': revision,
            'reconstructed_available_at': pd.Timestamp(available, tz='UTC')})
    versions = pd.DataFrame(rows)
    result = reconstruct(versions, '2023-09-01T22:00:00Z').set_index('field')
    assert result.loc['cash_flow_ttm', 'value'] == 120
    assert 'future' not in result.loc['cash_flow_ttm', 'sources']
    assert len(json.loads(result.loc['cash_flow_ttm', 'sources'])) == 3
    assert pd.isna(result.loc['total_debt', 'value'])
    # Concurrent conflicting facts may not be resolved by row order.
    conflict = versions.iloc[[2]].copy()
    conflict['value'] = 70
    result = reconstruct(pd.concat([versions, conflict]), '2023-09-01T22:00:00Z').set_index('field')
    assert pd.isna(result.loc['cash_flow_ttm', 'value'])


def test_public_availability_after_weekend_and_holiday():
    facts = {'cik': 1, 'facts': {'us-gaap': {'Assets': {'units': {'USD': [
        {'accn': 'a', 'end': '2023-03-31', 'val': 10, 'filed': '2023-05-26', 'form': '10-Q'}]}}}}}
    filings = pd.DataFrame({'accessionNumber': ['a'], 'acceptanceDateTime': ['2023-05-26T21:00:00Z']})
    versions = public_versions(facts, {'cik': 1}, filings, 1)
    assert versions.iloc[0].reconstructed_available_at == pd.Timestamp('2023-05-30T13:30:00Z')
    assert versions.available_at.isna().all()
