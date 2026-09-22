"""Reconstruct monthly public SEC evidence from frozen downloads, entirely offline."""
import json
import shutil
from pathlib import Path

import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, verify_files, write_json
from quant_research.sec_reconstruction import public_versions, reconstruct

PROJECT = Path(__file__).resolve().parents[1]
SOURCE = PROJECT / 'outputs/s4_history_20260918T134324919551Z_c1030411b50342269ddb113be180ac45'


def main():
    verify_files(SOURCE, json.loads((SOURCE/'manifest.json').read_text()))
    identifier = run_id('s4_reconstruction')
    with publication(PROJECT/'outputs', identifier) as stage:
        report = {'evidence_kind': 'public_filing_reconstruction', 'vendor_arrival_verified': False,
                  'rule': 'next XNYS session open strictly after later filing/acceptance local date',
                  'historical_universe_complete': False, 'issuers': {}}
        for label, cik in [('aapl', 320193), ('bbby_legacy', 886158), ('sears_legacy', 1310067)]:
            versions = public_versions(json.loads((SOURCE/f'{label}_facts.raw').read_bytes()),
                json.loads((SOURCE/f'{label}_submissions.raw').read_bytes()),
                pd.read_parquet(SOURCE/f'{label}_filings.parquet'), cik)
            versions.to_parquet(stage/f'{label}_versions.parquet', index=False)
            days = [*pd.date_range('2016-09-30', '2026-08-31', freq='ME'), pd.Timestamp('2026-09-17')]
            panel = pd.concat([reconstruct(versions, d.tz_localize('America/New_York')+pd.Timedelta(hours=18)) for d in days])
            panel['security_id'] = f'CIK{cik:010d}'
            panel.to_parquet(stage/f'{label}_monthly.parquet', index=False)
            report['issuers'][label] = {'versions': len(versions), 'decisions': len(days),
                'fields': {k: {'present': int(g.value.notna().sum()), 'missing': int(g.value.isna().sum()),
                              'source_age_over_365_days': int((g.source_age_days > 365).sum()),
                              'max_source_age_days': int(g.source_age_days.max()) if g.source_age_days.notna().any() else None,
                              'missing_reasons': g.missing_reason.dropna().value_counts().to_dict()}
                           for k, g in panel.groupby('field')}}
        report['freshness_warning'] = 'Values are last-known source periods, not necessarily current TTM at decision. Inspect source_age_days; stale data is not trading-ready.'
        write_json(stage/'report.json', report)
        shutil.copyfile(__file__, stage/'reconstruct_s4.py')
        shutil.copyfile(PROJECT/'src/quant_research/sec_reconstruction.py', stage/'sec_reconstruction.py')
        write_json(stage/'manifest.json', {'source_manifest_hash': file_hash(SOURCE/'manifest.json'),
                   'files': {p.name: file_hash(p) for p in stage.iterdir()}})
    print(PROJECT/'outputs'/identifier)


if __name__ == '__main__':
    main()
