"""Publish expanded, period-aligned SEC reconstruction and compare with frozen v1."""
import json
import shutil
from pathlib import Path

import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json
from quant_research.sec_financial_v2 import ALL_TAGS, enrich, reconstruct_v2
from quant_research.sec_reconstruction import public_versions

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT/'outputs/s4_history_20260918T134324919551Z_c1030411b50342269ddb113be180ac45'
PRIOR = PROJECT/'outputs/s4_reconstruction_20260919T002858920228Z_24cd231b345842d997895d3468770f4f'
POLICY = PROJECT/'data/sec_reviewed_mappings_v2.json'


def main():
    for path in (SOURCE, PRIOR):
        verify_files(path, json.loads((path/'manifest.json').read_text()))
    policy = json.loads(POLICY.read_text(encoding='utf-8'))
    identifier = run_id('s4_financial_v2')
    with publication(PROJECT/'outputs', identifier) as stage:
        report = {'kind': 'public_filing_reconstruction_not_complete_S4', 'version': 2,
                  'point_in_time_rule': 'strictly next XNYS open after later filing/acceptance local date',
                  'period_rule': 'all TTM endpoints match latest known assets period; conflicting paths fail closed',
                  'debt_rule': 'reviewed accession only; explicit lease scope; never zero-fill absent borrowings',
                  'freshness': 'last known report; age over 365 flagged, not a new trading strategy parameter',
                  'issuers': {}, 'arithmetic_rows_checked': 0, 'source_links_checked': 0}
        days = [*pd.date_range('2016-09-30', '2026-08-31', freq='ME'), pd.Timestamp('2026-09-17')]
        for label, cik in [('aapl', 320193), ('bbby_legacy', 886158), ('sears_legacy', 1310067)]:
            versions = public_versions(json.loads((SOURCE/f'{label}_facts.raw').read_bytes()),
                json.loads((SOURCE/f'{label}_submissions.raw').read_bytes()),
                pd.read_parquet(SOURCE/f'{label}_filings.parquet'), cik, tags=ALL_TAGS)
            expanded = enrich(versions, policy)
            expanded.to_parquet(stage/f'{label}_versions.parquet', index=False)
            assert expanded.available_at.isna().all()
            panel = pd.concat([reconstruct_v2(expanded, d.tz_localize('America/New_York')+pd.Timedelta(hours=18)) for d in days])
            for row in panel.itertuples():
                sources = json.loads(row.sources)
                if pd.notna(row.value):
                    assert row.value == sum(s['coefficient']*s['value'] for s in sources)
                    report['arithmetic_rows_checked'] += 1
                else:
                    assert row.missing_reason
                for source in sources:
                    assert pd.Timestamp(source['available_at']) <= pd.Timestamp(row.decision_at)
                    report['source_links_checked'] += 1
            panel.to_parquet(stage/f'{label}_monthly.parquet', index=False)
            old = pd.read_parquet(PRIOR/f'{label}_monthly.parquet')
            comparison = panel.merge(old[['decision_at', 'field', 'value', 'source_age_days']],
                                     on=['decision_at', 'field'], how='left', suffixes=('_v2', '_v1'))
            comparison.to_parquet(stage/f'{label}_comparison.parquet', index=False)
            report['issuers'][label] = {'raw_versions': len(versions), 'derived_versions': len(expanded)-len(versions),
                'fields': {k: {'present': int(g.value.notna().sum()), 'missing': int(g.value.isna().sum()),
                    'present_report_age_at_most_365': int((g.value.notna() & ~g.report_age_over_365_days.fillna(True)).sum()),
                    'missing_reasons': g.missing_reason.dropna().value_counts().to_dict()}
                    for k, g in panel.groupby('field')},
                'changed_comparable_values': int((comparison.value_v1.notna() & comparison.value_v2.notna() &
                                                (comparison.value_v1 != comparison.value_v2)).sum())}
            print(label, report['issuers'][label]['fields'], flush=True)
        for relative in ['scripts/reconstruct_s4_v2.py', 'src/quant_research/sec_financial_v2.py',
                         'src/quant_research/sec_reconstruction.py', 'src/quant_research/sec_evidence.py',
                         'src/quant_research/calendar.py', 'data/sec_reviewed_mappings_v2.json']:
            shutil.copyfile(PROJECT/relative, stage/Path(relative).name)
        write_json(stage/'report.json', report)
        write_json(stage/'manifest.json', {'source_manifest_hash': file_hash(SOURCE/'manifest.json'),
            'prior_manifest_hash': file_hash(PRIOR/'manifest.json'),
            'files': {p.name: file_hash(p) for p in stage.iterdir()}})
    print(PROJECT/'outputs'/identifier)


if __name__ == '__main__':
    main()
