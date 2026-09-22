"""Public filing reconstruction; never evidence of vendor historical arrival."""
import json

import pandas as pd

from .calendar import calendar
from .sec_evidence import extract_facts


def public_versions(facts, submissions, filings, cik, tags=None):
    metadata = dict(submissions)
    metadata['filings'] = {'recent': filings.to_dict('list')}
    rows = extract_facts(facts, metadata, cik, tags=tags)
    cal = calendar('1990-01-01', '2030-12-31')
    def available(row):
        day = pd.Timestamp(row.filed_date).date()
        accepted = pd.to_datetime(row.acceptance_timestamp_as_returned, utc=True, errors='coerce')
        if pd.notna(accepted):
            day = max(day, accepted.tz_convert('America/New_York').date())
        session = cal.date_to_session(pd.Timestamp(day)+pd.Timedelta(days=1), direction='next')
        return cal.session_open(session)
    rows['reconstructed_available_at'] = rows.apply(available, axis=1)
    rows['evidence_kind'] = 'public_filing_reconstruction'
    return rows


def reconstruct(versions, decision):
    """TTM via annual or annual + current YTD - prior YTD; ambiguity stays missing."""
    decision = pd.Timestamp(decision)
    if decision.tzinfo is None:
        raise ValueError('timezone required')
    rows = versions[(versions.reconstructed_available_at <= decision) & (versions.unit == 'USD')].copy()
    rows = rows[rows.form.isin(['10-K', '10-K/A', '10-Q', '10-Q/A'])]
    rows['end'] = pd.to_datetime(rows.period_end)
    rows['start'] = pd.to_datetime(rows.period_start)
    rows = rows[rows.end <= decision.tz_localize(None)]
    keys = ['metric', 'period_start', 'period_end', 'unit']
    # Resolve versions by filing availability, but never choose arbitrary concurrent values.
    rows = rows[rows.reconstructed_available_at == rows.groupby(keys, dropna=False).reconstructed_available_at.transform('max')]
    conflicts = rows.groupby(keys, dropna=False).value.transform('nunique') > 1
    ambiguous = rows[conflicts].copy()
    rows = rows[~conflicts].drop_duplicates(keys)
    out = []
    def emit(field, components, value=None, reason=None):
        out.append({'decision_at': decision.isoformat(), 'field': field, 'value': value,
                    'missing_reason': reason, 'evidence_kind': 'public_filing_reconstruction',
                    'latest_source_period_end': max((r.period_end for r in components), default=None),
                    'source_age_days': (decision.date()-pd.Timestamp(max(r.period_end for r in components)).date()).days if components else None,
                    'sources': json.dumps([{'metric': r.metric, 'start': r.period_start,
                        'end': r.period_end, 'accession': r.revision_id,
                        'available_at': r.reconstructed_available_at.isoformat(), 'value': r.value}
                        for r in components], sort_keys=True)})
    for tag, field in [('GrossProfit', 'gross_profit_ttm'),
                       ('NetIncomeLoss', 'net_income_ttm'),
                       ('NetCashProvidedByUsedInOperatingActivities', 'cash_flow_ttm')]:
        part = rows[rows.metric == tag].copy()
        blocked = ambiguous[ambiguous.metric == tag]
        if part.empty or (not blocked.empty and blocked.end.max() >= part.end.max()):
            emit(field, [], reason='missing_or_conflicting_duration_facts')
            continue
        end = part.end.max()
        annual = part[(part.end == end) & ((part.end-part.start).dt.days.between(350, 380))]
        candidates = []
        if len(annual) == 1:
            r = next(annual.itertuples())
            candidates.append((r.value, [r]))
        else:
            for cur in part[(part.end == end) & ((part.end-part.start).dt.days.between(60, 300))].itertuples():
                years = part[((part.end-part.start).dt.days.between(350, 380)) &
                             ((cur.start-part.end).dt.days == 1)]
                for year in years.itertuples():
                    prior = part[(part.start == year.start) &
                                 ((cur.end-part.end).dt.days.between(350, 380)) &
                                 (((part.end-part.start).dt.days-(cur.end-cur.start).days).abs() <= 7)]
                    if len(prior) == 1:
                        prev = next(prior.itertuples())
                        candidates.append((year.value+cur.value-prev.value, [year, cur, prev]))
        # Any conflicting component invalidates reconstruction rather than choosing another period.
        if len(candidates) == 1 and not any(
            ((ambiguous.metric == r.metric) & (ambiguous.period_end == r.period_end) &
             (ambiguous.period_start == r.period_start)).any() for r in candidates[0][1]):
            emit(field, candidates[0][1], float(candidates[0][0]))
        else:
            emit(field, [], reason='no_unique_annual_or_ytd_bridge')
    assets = rows[rows.metric == 'Assets']
    asset_end = assets.end.max() if not assets.empty else pd.NaT
    for field, selected in [('assets_end', assets[assets.end == asset_end]),
                            ('assets_start', assets[(asset_end-assets.end).dt.days.between(350, 380)])]:
        if len(selected) == 1 and not ((ambiguous.metric == 'Assets') & (ambiguous.end >= selected.end.max())).any():
            r = next(selected.itertuples())
            emit(field, [r], float(r.value))
        else:
            emit(field, [], reason='no_unique_assets_period')
    # Report debt components independently. Absent short-term debt is not zero,
    # and overlapping long-term debt concepts are not summed into a false total.
    for tag in ['LongTermDebtCurrent', 'LongTermDebtNoncurrent', 'LongTermDebt', 'ShortTermBorrowings', 'CommercialPaper']:
        selected = rows[(rows.metric == tag) & (rows.end == asset_end)]
        if len(selected) == 1 and not ((ambiguous.metric == tag) & (ambiguous.end == asset_end)).any():
            r = next(selected.itertuples())
            emit(tag, [r], float(r.value))
        else:
            emit(tag, [], reason='missing_or_conflicting_debt_component_at_assets_date')
    emit('total_debt', [], reason='issuer_specific_debt_taxonomy_reconciliation_required')
    return pd.DataFrame(out)
