"""File evidence gates only; accepted records are not completed strategies."""
from dataclasses import fields

from .execution_evidence import BorrowEvidence, validate_option_entry
from .fundamental_signals import EarningsEvent, event_execution_times, event_signal
from .option_payoffs import VanillaLeg


def schema():
    return {'envelope': ['evidence_kind=real_data|synthetic_fixture', 'source_id', 'records'],
            's5_record': {'event': [f.name for f in fields(EarningsEvent)],
                          'history': 'array of EarningsEvent', 'decision': 'timezone-aware timestamp'},
            's6_record': {'evidence': [f.name for f in fields(BorrowEvidence)],
                          'at': 'timezone-aware timestamp', 'requested_shares': 'nonnegative number'},
            's7_record': {'legs': [f.name for f in fields(VanillaLeg)],
                          'quotes': ['contract_id', 'quote_at', 'available_at', 'kind', 'strike',
                                     'expiry', 'multiplier', 'deliverable_shares', 'bid', 'ask', 'delta'],
                          'at': 'timezone-aware timestamp', 'nav': 'positive number', 'cash': 'number',
                          'underlying_shares': 'number', 'underlying_price': 'positive number',
                          'variant': 'collar|put_spread|covered_call'},
            'return_codes': {'0': 'all real-data records pass gate; not independently authenticated',
                             '2': 'empty/malformed/failed or partially failed validation',
                             '3': 'synthetic fixture only, never real-data readiness'}}


def validate_file(kind, document):
    if kind not in {'s5', 's6', 's7'}:
        raise ValueError('unknown input kind')
    if (not isinstance(document, dict) or set(document) != {'evidence_kind', 'source_id', 'records'}
            or document['evidence_kind'] not in {'real_data', 'synthetic_fixture'}
            or not isinstance(document['source_id'], str) or not document['source_id'].strip()
            or not isinstance(document['records'], list) or not document['records']):
        raise ValueError('nonempty records, source_id and explicit evidence_kind required')
    results = []
    identities = set()
    for index, row in enumerate(document['records']):
        try:
            if not isinstance(row, dict):
                raise TypeError('record must be an object')
            if kind == 's5':
                if set(row) != {'event', 'history', 'decision'}:
                    raise ValueError('s5 requires event/history/decision')
                event = EarningsEvent(**row['event'])
                identity = event.event_id
                result = event_signal(event, [EarningsEvent(**old) for old in row['history']], row['decision'])
                result.update(event_execution_times(event))
            elif kind == 's6':
                if set(row) != {'evidence', 'at', 'requested_shares'}:
                    raise ValueError('s6 requires evidence/at/requested_shares')
                evidence = BorrowEvidence(**row['evidence'])
                identity = (evidence.security_id, evidence.source_id, row['at'])
                result = evidence.check(row['at'], row['requested_shares'])
            else:
                if set(row) != {'legs', 'quotes', 'at', 'nav', 'cash', 'underlying_shares', 'underlying_price', 'variant'}:
                    raise ValueError('s7 requires legs/quotes/at/nav/cash/underlying_shares/underlying_price/variant')
                legs = [VanillaLeg(**leg) for leg in row['legs']]
                identity = (row['at'], tuple(q['contract_id'] for q in row['quotes']))
                if len({q['contract_id'] for q in row['quotes']}) != len(row['quotes']):
                    raise ValueError('duplicate option contract')
                result = validate_option_entry(**{**row, 'legs': legs})
            if identity in identities:
                raise ValueError('duplicate input identity')
            identities.add(identity)
            results.append({'row': index, 'passed': True, 'result': result})
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
            results.append({'row': index, 'passed': False, 'reason': str(exc)})
    failed = sum(not r['passed'] for r in results)
    code = 2 if failed else (3 if document['evidence_kind'] == 'synthetic_fixture' else 0)
    return {'kind': kind, 'evidence_kind': document['evidence_kind'], 'source_id': document['source_id'],
            'source_authenticity': 'user_asserted_not_independently_verified',
            'strategy_ready': False, 'accepted': len(results)-failed, 'rejected': failed,
            'return_code': code, 'rows': results}
