"""Validate sourced financial records locally. Never uploads or backdates observations.

Input: schema_version=1 and snapshots as documented in docs/ops/ROE_FII_SCREEN.md.
Each incoming row also needs reference_price for the local verification report.
"""
from datetime import datetime, timezone
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from swing_core.fundamental_screen import evaluate_screen


def prepare(payload, observed_at):
    if payload.get('schema_version') != 1 or not isinstance(payload.get('snapshots'), list):
        raise ValueError('Expected schema_version=1 and a snapshots list')
    rows, report = [], []
    symbols = set()
    for original in payload['snapshots']:
        row = {**original, 'observed_at': observed_at}
        symbol = row.get('symbol')
        if not isinstance(symbol, str) or not symbol or symbol != symbol.strip().upper() or symbol in symbols:
            raise ValueError('Incoming symbols must be unique uppercase exchange symbols')
        symbols.add(symbol)
        result = evaluate_screen(symbol, row.get('reference_price'), observed_at,
                                 {'schema_version': 1, 'snapshots': [row]})
        if result['status'] == 'UNVERIFIED':
            raise ValueError(f'{symbol}: ' + ', '.join(result['reasons']))
        rows.append(row)
        report.append({'symbol': symbol, 'status': result['status'], 'reasons': result['reasons']})
    return {'schema_version': 1, 'snapshots': rows}, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--existing', type=Path, help='Previously archived snapshot store; append, never erase history')
    args = parser.parse_args()
    now = datetime.now(timezone.utc).isoformat()
    fresh, report = prepare(json.loads(args.input.read_text()), now)
    if args.existing:
        existing = json.loads(args.existing.read_text())
        if existing.get('schema_version') != 1 or not isinstance(existing.get('snapshots'), list):
            raise ValueError('Invalid existing store')
        fresh['snapshots'] = existing['snapshots'] + fresh['snapshots']
    # Exclusive create: do not overwrite an existing file or an archived vintage.
    with args.output.open('x', encoding='utf-8') as handle:
        json.dump(fresh, handle, indent=2, allow_nan=False)
        handle.write('\n')
    print(json.dumps({'observed_at': now, 'rows': report, 'uploaded': False}, indent=2))


if __name__ == '__main__':
    main()
