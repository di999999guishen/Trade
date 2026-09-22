"""Import S5/S6/S7 JSON evidence; reports failures and never places orders."""
import argparse
import json
from pathlib import Path

from quant_research.artifacts import file_hash, publication, run_id, write_json
from quant_research.input_import import schema, validate_file

PROJECT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--schema', action='store_true')
    parser.add_argument('--kind', choices=['s5', 's6', 's7'])
    parser.add_argument('--input', type=Path)
    args = parser.parse_args()
    if args.schema:
        print(json.dumps(schema(), indent=2))
        return 0
    if not args.kind or not args.input:
        parser.error('--kind and --input required')
    identifier = run_id('strategy_import')
    with publication(PROJECT/'outputs', identifier) as stage:
        try:
            report = validate_file(args.kind, json.loads(args.input.read_text(encoding='utf-8-sig')))
            report['input_sha256'] = file_hash(args.input)
        except (OSError, ValueError, TypeError) as exc:
            report = {'return_code': 2, 'strategy_ready': False, 'reason': str(exc)}
        write_json(stage/'report.json', report)
        write_json(stage/'manifest.json', {'files': {'report.json': file_hash(stage/'report.json')}})
    print(json.dumps({'output': str(PROJECT/'outputs'/identifier), 'return_code': report['return_code']}))
    return report['return_code']


if __name__ == '__main__':
    raise SystemExit(main())
