"""Accession-scoped financial reconstruction with explicit algebra and period alignment."""
import json
from itertools import product
from math import isclose, isfinite

import pandas as pd

from .sec_evidence import TAGS

EXTRA_TAGS = ('NetCashProvidedByUsedInOperatingActivitiesContinuingOperations',
              'CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations',
              'NetIncomeLossAvailableToCommonStockholdersBasic', 'SalesRevenueNet',
              'RevenueFromContractWithCustomerIncludingAssessedTax',
              'RevenueFromContractWithCustomerExcludingAssessedTax', 'Revenues',
              'CostOfGoodsAndServicesSold', 'CostOfGoodsSold', 'CostOfRevenue',
              'LongTermDebtAndCapitalLeaseObligationsCurrent', 'LongTermDebtAndCapitalLeaseObligations',
              'DebtAndCapitalLeaseObligations', 'CapitalLeaseObligations', 'OtherShortTermBorrowings')
ALL_TAGS = (*TAGS, *EXTRA_TAGS)
FLOW_FIELDS = {'GrossProfit': 'gross_profit_ttm', 'NetIncomeLoss': 'net_income_ttm',
               'NetCashProvidedByUsedInOperatingActivities': 'cash_flow_ttm'}


def leaf(row, coefficient=1):
    return {'metric': row['metric'], 'period_start': row['period_start'], 'period_end': row['period_end'],
            'value': float(row['value']), 'coefficient': coefficient, 'accession': row['revision_id'],
            'available_at': row['reconstructed_available_at'].isoformat()}


def derived(metric, parts, rule, review_url=None):
    row = dict(parts[0][0])
    row.update(metric=metric, value=sum(r['value']*c for r, c in parts),
               reconstructed_available_at=max(r['reconstructed_available_at'] for r, _ in parts),
               lineage=json.dumps([leaf(r, c) for r, c in parts], sort_keys=True),
               derivation=rule, review_url=review_url)
    return row


def enrich(versions, policy):
    """All derivations occur within the same issuer/accession/period, never across revisions."""
    base = versions[versions.unit == 'USD'].copy()
    if base.security_id.nunique() != 1:
        raise ValueError('one issuer required')
    if not base.value.map(isfinite).all():
        raise ValueError('finite financial facts required')
    rows = base.to_dict('records')
    for r in rows:
        r.update(lineage=json.dumps([leaf(r)], sort_keys=True), derivation='reported', review_url=None)
    additions = []
    reviews = {r['accession']: r for r in policy['reviews']
               if f"CIK{r['cik']:010d}" == base.security_id.iloc[0]}
    for acc, review in reviews.items():
        filing = base[base.revision_id == acc]
        if filing.empty:
            raise ValueError(f'missing reviewed accession: {acc}')
        for anchor in review['anchors']:
            match = filing[(filing.metric == anchor['metric']) & (filing.period_end == anchor['end'])]
            if 'start' in anchor:
                match = match[match.period_start == anchor['start']]
            if anchor.get('annual'):
                match = match[(pd.to_datetime(match.period_end)-pd.to_datetime(match.period_start)).dt.days.between(350, 380)]
            if match.empty or set(match.value) != {anchor['value']}:
                raise ValueError(f'review anchor mismatch: {acc}/{anchor["metric"]}')
        for r in filing.to_dict('records'):
            if r['metric'] in review['aliases']:
                if review.get('annual_aliases_only') and not (
                    r['period_start'] and 350 <= (pd.Timestamp(r['period_end'])-pd.Timestamp(r['period_start'])).days <= 380
                ):
                    continue
                additions.append(derived(review['aliases'][r['metric']], [(r, 1)],
                                         'reviewed_statement_alias', review['url']))
    for (acc, start, _end), group in base.groupby(['revision_id', 'period_start', 'period_end'], dropna=False):
        values = {}
        for metric, g in group.groupby('metric'):
            if g.value.nunique() == 1:
                values[metric] = g.iloc[0].to_dict()
        if pd.notna(start):
            # Generate every available interpretation; differing results remain a conflict.
            revenue = [values[k] for k in ('SalesRevenueNet', 'RevenueFromContractWithCustomerExcludingAssessedTax',
                       'RevenueFromContractWithCustomerIncludingAssessedTax', 'Revenues') if k in values]
            costs = [values[k] for k in ('CostOfGoodsAndServicesSold', 'CostOfGoodsSold', 'CostOfRevenue') if k in values]
            for sales, cost in product(revenue, costs):
                additions.append(derived('GrossProfit', [(sales, 1), (cost, -1)], 'revenue_minus_reported_cost'))
            c, d = ('NetCashProvidedByUsedInOperatingActivitiesContinuingOperations',
                    'CashProvidedByUsedInOperatingActivitiesDiscontinuedOperations')
            if c in values and d in values:
                additions.append(derived('NetCashProvidedByUsedInOperatingActivities',
                                         [(values[c], 1), (values[d], 1)], 'continuing_plus_discontinued'))
        else:
            review = reviews.get(acc, {})
            for metric, formula in review.get('debt_formulas', {}).items():
                if all(k in values for k in formula):
                    additions.append(derived(metric, [(values[k], c) for k, c in formula.items()],
                                             'reviewed_debt_components', review['url']))
    return pd.DataFrame([*rows, *additions])


def selected_asof(versions, decision):
    stamp = pd.Timestamp(decision)
    if stamp.tzinfo is None or pd.isna(stamp):
        raise ValueError('explicit decision timezone required')
    if versions.security_id.nunique() != 1:
        raise ValueError('one issuer required')
    rows = versions[(versions.reconstructed_available_at <= stamp) &
                    versions.form.isin(['10-K', '10-K/A', '10-Q', '10-Q/A'])].copy()
    rows['end'] = pd.to_datetime(rows.period_end)
    rows['start'] = pd.to_datetime(rows.period_start)
    rows = rows[rows.end <= stamp.tz_convert('America/New_York').tz_localize(None)]
    keys = ['metric', 'period_start', 'period_end', 'unit']
    rows = rows[rows.reconstructed_available_at == rows.groupby(keys, dropna=False).reconstructed_available_at.transform('max')]
    # Preserve a conflict sentinel rather than falling back to an older value.
    rows['conflict'] = rows.groupby(keys, dropna=False).value.transform('nunique') > 1
    rows = rows.sort_values(['derivation', 'revision_id']).drop_duplicates(keys).reset_index(drop=True)
    originals = rows[rows.derivation == 'reported']
    for i, r in rows[rows.derivation != 'reported'].iterrows():
        for source in json.loads(r.lineage):
            start_match = originals.period_start.isna() if source['period_start'] is None else originals.period_start == source['period_start']
            newer = originals[(originals.metric == source['metric']) & start_match &
                              (originals.period_end == source['period_end']) &
                              (originals.reconstructed_available_at > pd.Timestamp(source['available_at']))]
            if not newer.empty and (newer.conflict.any() or (newer.value != source['value']).any()):
                rows.loc[i, 'conflict'] = True
    return rows


def ttm(rows, end):
    """Use one annual, an exact annual/YTD bridge, or four contiguous quarters."""
    rows = rows[rows.start.notna()]
    rows = rows[rows.end <= end]
    length = (rows.end-rows.start).dt.days
    annual = rows[length.between(350, 380)]
    candidates = []
    for r in annual[annual.end == end].to_dict('records'):
        candidates.append([(r, 1)])
    for cur in rows[(rows.end == end) & length.between(60, 300)].to_dict('records'):
        for year in annual[annual.end+pd.Timedelta(days=1) == cur['start']].to_dict('records'):
            prior = rows[(rows.start == year['start']) & ((cur['end']-rows.end).dt.days.between(350, 380)) &
                         (((rows.end-rows.start).dt.days-(cur['end']-cur['start']).days).abs() <= 7)]
            for old in prior.to_dict('records'):
                candidates.append([(year, 1), (cur, 1), (old, -1)])
    quarters = rows[length.between(70, 105)].to_dict('records')
    def walk(target, path):
        if len(path) == 4:
            if 350 <= (end-path[-1]['start']).days <= 380:
                candidates.append([(r, 1) for r in path])
            return
        for row in quarters:
            if row['end'] == target:
                walk(row['start']-pd.Timedelta(days=1), [*path, row])
    walk(end, [])
    if not candidates:
        return None, 'no_annual_ytd_or_four_quarter_path'
    # Do not discard ambiguous paths merely because a second path succeeds.
    if any(r['conflict'] for path in candidates for r, _ in path):
        return None, 'conflicting_source_fact'
    values = [sum(r['value']*c for r, c in path) for path in candidates]
    if not all(isclose(v, values[0], rel_tol=0, abs_tol=.01) for v in values):
        return None, 'inconsistent_ttm_paths_or_reporting_bases'
    return min(candidates, key=len), None


def reconstruct_v2(versions, decision):
    rows = selected_asof(versions, decision)
    assets = rows[rows.metric == 'Assets']
    end = assets.end.max()
    result = []
    def emit(field, parts=None, reason=None):
        sources = []
        for r, coefficient in parts or []:
            for original in json.loads(r['lineage']):
                sources.append({**original, 'coefficient': original['coefficient']*coefficient,
                                'derivation': r['derivation'], 'review_url': r['review_url']})
        value = sum(s['value']*s['coefficient'] for s in sources) if sources else None
        if value is not None and not isfinite(value):
            raise ValueError('nonfinite reconstruction')
        age = (pd.Timestamp(decision).date()-end.date()).days if pd.notna(end) else None
        result.append({'security_id': versions.security_id.iloc[0], 'decision_at': pd.Timestamp(decision).isoformat(),
                       'field': field, 'value': value, 'missing_reason': reason,
                       'financial_period_end': end.date().isoformat() if pd.notna(end) else None,
                       'report_age_days': age, 'report_age_over_365_days': age > 365 if age is not None else None,
                       'evidence_kind': 'public_filing_reconstruction', 'sources': json.dumps(sources, sort_keys=True)})
    for metric, field in FLOW_FIELDS.items():
        parts, reason = ttm(rows[rows.metric == metric], end) if pd.notna(end) else (None, 'no_assets_period')
        emit(field, parts, reason)
    for field, group in [('assets_end', assets[assets.end == end]),
                         ('assets_start', assets[(end-assets.end).dt.days.between(350, 380)])]:
        if len(group) == 1 and not group.conflict.any():
            emit(field, [(group.iloc[0].to_dict(), 1)])
        else:
            emit(field, reason='missing_or_conflicting_same_period_assets')
    for metric, field in [('TotalDebtVerified', 'total_debt'),
                         ('BorrowingsIncludingCapitalLeasesReviewed', 'borrowings_including_capital_leases'),
                         ('BorrowingsExcludingCapitalLeasesReviewed', 'borrowings_excluding_capital_leases')]:
        g = rows[(rows.metric == metric) & (rows.end == end)]
        if len(g) == 1 and not g.conflict.any():
            emit(field, [(g.iloc[0].to_dict(), 1)])
        else:
            emit(field, reason='no_reviewed_debt_scope_at_current_assets_period')
    return pd.DataFrame(result)
