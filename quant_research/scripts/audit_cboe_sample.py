"""Inspect original official ZIP without assuming sample data is executable."""
import io
import zipfile
from pathlib import Path

import pandas as pd

from quant_research.artifacts import file_hash, publication, run_id, write_json

PROJECT = Path(__file__).resolve().parents[1]


def main():
    source = PROJECT/'data/cboe_sample_20260919/response.raw'
    report = {'url': 'https://datashop.cboe.com/download/sample/215', 'source_sha256': file_hash(source),
              'kind': 'official_supplier_sample_not_full_chain_history', 'files': [],
              'missing_execution_evidence': ['historical_available_at', 'deliverable_and_multiplier_master',
                                            'assignment_events', 'roll_and_exit_coverage']}
    with zipfile.ZipFile(source) as outer:
        for name in outer.namelist():
            if not name.endswith('.zip'):
                continue
            with zipfile.ZipFile(io.BytesIO(outer.read(name))) as inner:
                for member in inner.namelist():
                    if not member.endswith('.csv'):
                        continue
                    frame = pd.read_csv(inner.open(member))
                    stamps = pd.to_datetime(frame.quote_datetime)
                    minute = stamps.dt.hour*60+stamps.dt.minute
                    dte = (pd.to_datetime(frame.expiration)-stamps.dt.normalize()).dt.days
                    valid = (frame.bid > 0) & (frame.ask >= frame.bid)
                    report['files'].append({'name': member, 'rows': len(frame), 'columns': list(frame),
                        'symbols': sorted(frame.underlying_symbol.unique().tolist()),
                        'first_quote': str(stamps.min()), 'last_quote': str(stamps.max()),
                        'quote_times': sorted(stamps.dt.strftime('%H:%M:%S').unique().tolist()),
                        'valid_bid_ask_rows': int(valid.sum()), 'has_delta': 'delta' in frame,
                        'entry_window_rows': int(minute.between(585, 599).sum()),
                        'entry_window_30_60dte_valid_quotes': int((minute.between(585, 599) & dte.between(30, 60) & valid).sum()),
                        'null_counts': {k: int(v) for k, v in frame.isna().sum().items() if v}})
    identifier = run_id('cboe_sample_audit')
    with publication(PROJECT/'outputs', identifier) as stage:
        write_json(stage/'report.json', report)
        write_json(stage/'manifest.json', {'files': {'report.json': file_hash(stage/'report.json')}})
    print(PROJECT/'outputs'/identifier)
    print([(f['rows'], f['symbols'], f['entry_window_rows']) for f in report['files']])


if __name__ == '__main__':
    main()
