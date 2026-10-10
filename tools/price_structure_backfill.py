"""Build an audit sidecar, never modify ledgers/state/production DB."""
from __future__ import annotations
import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.monitor.price_structure import Hour, StructureBuffer, fields, timestamp_ms

DEFAULT_SINCE = "2026-10-08T23:52:00-03:00"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_buffer(path):
    rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    hours = []
    for r in rows:
        at = int(r['open_time_ms'])
        if at % 3600000 or int(r['close_time_ms']) != at + 3599999:
            raise ValueError('Invalid hourly candle')
        hours.append(Hour(at, float(r['high']), float(r['low'])))
    if len({h.open_ms for h in hours}) != len(hours):
        raise ValueError('Duplicate hourly candles')
    return StructureBuffer(hours)


def verify_parity(buffer, study):
    """All qualified study snapshots, not just a favourable sample."""
    manifest = json.loads((study/'market_manifest.json').read_text(encoding='utf-8'))
    if digest(study/'market/SOLUSDT_1h.jsonl') != manifest['1h']['sha256']:
        raise ValueError('Study hourly hash mismatch')
    prior = json.loads((study/'context_hourly.json').read_text(encoding='utf-8'))['3']
    checked = 0
    for old in prior:
        if old['available_ms'] - old['window_start_ms'] != 72 * 3600000:
            continue
        new = buffer.snapshot(old['available_ms'], 'BACKFILL')
        if new['label'] != old['label']:
            raise ValueError(f"PARITY FAILED label at {old['available_ms']}")
        for oldkey, newkey in [('tops', 'highs'), ('bottoms', 'lows')]:
            expected = [(p['open_ms'], p['value'], p['confirmed_ms']) for p in old[oldkey]]
            actual = [(p['open_ms'], p['price'], p['confirmed_ms']) for p in new[newkey]]
            if actual != expected:
                raise ValueError(f"PARITY FAILED pivots at {old['available_ms']}")
            relation = 'UNDEFINED' if len(expected) < 2 else (
                ('HH' if oldkey == 'tops' else 'HL') if expected[1][1] > expected[0][1] else
                ('LH' if oldkey == 'tops' else 'LL') if expected[1][1] < expected[0][1] else 'EQUAL')
            if new[f'{newkey}_class'] != relation:
                raise ValueError('PARITY FAILED relation')
        checked += 1
    if not checked:
        raise ValueError('No study snapshots verified')
    return checked


def reconstruct(records, buffer, since):
    out = []
    for ledger, row in records:
        opened = row.get('opened_at') or row.get('open_ts')
        closed = row.get('closed_at') or row.get('close_ts')
        if not opened or not row.get('pair_id'):
            continue
        # Includes carry-over positions closed after the patch, preserving both causal timestamps.
        if timestamp_ms(opened) < since and (not closed or timestamp_ms(closed) < since):
            continue
        result = dict(ledger=ledger, pair_id=row['pair_id'], symbol=row.get('symbol'),
                      opened_at=opened, closed_at=closed)
        if row.get('symbol') != 'SOLUSDT':
            continue  # Other instruments require their own candles, never reuse SOL data.
        for phase, at in [('open', opened), ('close', closed)]:
            if at:
                snap = buffer.snapshot(at, 'BACKFILL')
                if snap['missing_hours']:
                    raise ValueError(f"Missing candles: {ledger}:{row['pair_id']}:{phase}")
                result.update(fields(snap, phase))
        out.append(result)
    return out


def extend_market(original, destination):
    """Copy audited history, append missing closed hours; never rewrite the study cache."""
    from tools.market_selection_study import BinancePublicClient
    if destination.exists():
        raise ValueError('Extended candle artifact already exists')
    rows = [json.loads(l) for l in original.read_text(encoding='utf-8').splitlines() if l.strip()]
    client = BinancePublicClient('https://api.binance.com', 30)
    end = int(client.get('/api/v3/time')['serverTime']) // 3600000 * 3600000
    cursor = max(r['open_time_ms'] for r in rows) + 3600000
    while cursor < end:
        batch = client.get('/api/v3/klines', dict(symbol='SOLUSDT', interval='1h', startTime=cursor,
                                                endTime=end-1, limit=1000))
        if not batch:
            raise ValueError('Missing public hourly candles')
        for k in batch:
            if int(k[6]) < end:
                rows.append(dict(open_time_ms=int(k[0]), close_time_ms=int(k[6]),
                                 open=float(k[1]), high=float(k[2]), low=float(k[3]), close=float(k[4])))
        next_cursor = int(batch[-1][0]) + 3600000
        if next_cursor <= cursor:
            raise ValueError('Public candle fetch did not advance')
        cursor = next_cursor
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('x', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row)+'\n')
    return destination


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--ledger-dir', type=Path, required=True)
    p.add_argument('--state-dir', type=Path, help='Optional copied JSON states; no DB access')
    p.add_argument('--candles', type=Path, required=True)
    p.add_argument('--extend-market', action='store_true', help='Append missing closed Binance 1h candles to a new local artifact')
    p.add_argument('--study-dir', type=Path, default=ROOT/'data/analysis/price_structure_72h_20261010')
    p.add_argument('--since', default=DEFAULT_SINCE, help='Timezone-aware ISO timestamp')
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    # Validate destinations before even preparing an optional market artifact.
    if args.output.exists() or args.output.with_suffix('.manifest.json').exists():
        raise ValueError('Output already exists; choose a new audit path')
    if args.output.resolve().is_relative_to((ROOT/'data/trades').resolve()) or args.output.resolve().is_relative_to((ROOT/'data/state').resolve()):
        raise ValueError('Backfill output must not be a ledger or state')
    candles = extend_market(args.candles, args.output.with_suffix('.candles.jsonl')) if args.extend_market else args.candles
    buffer = load_buffer(candles)
    checked = verify_parity(buffer, args.study_dir)  # BEFORE writing any output.
    records, hashes = [], {}
    for path in sorted(args.ledger_dir.glob('*.jsonl')):
        hashes[str(path)] = digest(path)
        for line in path.read_text(encoding='utf-8').splitlines():
            if line.strip():
                records.append((path.name, json.loads(line)))
    if args.state_dir:
        for path in sorted(args.state_dir.glob('*.json')):
            hashes[str(path)] = digest(path)
            value = json.loads(path.read_text(encoding='utf-8'))
            positions = value if isinstance(value, list) else value.get('positions', [])
            records.extend((path.name, r) for r in positions if r.get('status') == 'OPEN')
    rows = reconstruct(records, buffer, timestamp_ms(args.since))
    manifest = dict(since=args.since, parity_snapshots=checked, rows=len(rows),
                    candles_path=str(candles), candles_sha256=digest(candles), input_sha256=hashes,
                    excluded_other_symbols=sum(r.get('symbol') not in (None,'SOLUSDT') for _,r in records),
                    sources=['BACKFILL'], format='audit sidecar; originals untouched')
    # Never overwrite production ledger/state, or an earlier audit artifact.
    if args.output.exists() or args.output.with_suffix('.manifest.json').exists():
        raise ValueError('Output already exists; choose a new audit path')
    if args.output.resolve().is_relative_to((ROOT/'data/trades').resolve()) or args.output.resolve().is_relative_to((ROOT/'data/state').resolve()):
        raise ValueError('Backfill output must not be a ledger or state')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False)+'\n')
    args.output.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
