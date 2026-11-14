"""Bind provider/adjustment/unit metadata to the exact price file bytes."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def write_price_metadata(path, *, provider, adjustment, volume_unit, amount_status, source_url):
    path = Path(path)
    metadata = {
        'schema_version': 1, 'status': 'provider_declared',
        'provider': provider, 'adjustment': adjustment, 'volume_unit': volume_unit,
        'amount_unit': 'CNY' if amount_status == 'available' else None,
        'amount_status': amount_status, 'source_url': source_url,
        'retrieved_at_utc': datetime.now(timezone.utc).isoformat(),
        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
        'pit_corporate_actions_certified': False,
    }
    target = path.with_suffix('.meta.json')
    temporary = target.with_suffix('.tmp')
    temporary.write_text(json.dumps(metadata,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(target)
    return metadata


def read_price_metadata(path, sha256):
    sidecar = Path(path).with_suffix('.meta.json')
    if not sidecar.exists():
        return None
    try:
        metadata = json.loads(sidecar.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'status': 'metadata_unreadable'}
    if not isinstance(metadata,dict) or metadata.get('sha256') != sha256:
        return {'status': 'metadata_hash_mismatch'}
    return metadata
