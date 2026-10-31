"""Local research audit; never connects to an exchange or modifies runtime files."""
import json
import sys
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'data/analysis/fast_drop_selectors_20261018'
BRT = timezone(timedelta(hours=-3))


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, BRT).isoformat()


def main():
    from tests.test_fast_drop_ema_shadow import FastDropTests, _snapshot
    OUT.mkdir(parents=True, exist_ok=True)
    witnesses = []
    for delay_reference in (False, True):
        with TemporaryDirectory(dir=OUT) as tmp:
            fixture = FastDropTests()
            shadow, _ = fixture.make(Path(tmp))
            try:
                fixture.open(shadow)
                shadow.on_closed_5m(_snapshot('SHO'))
                if not delay_reference:
                    fixture.reference(shadow)
                # Identical observed price sequence, OHLC and intrabar ordering.
                shadow.on_tick(100., '2026-10-01T00:06:01+00:00')
                shadow.on_tick(99.4, '2026-10-01T00:06:20+00:00')
                if delay_reference:
                    fixture.reference(shadow)
                shadow.on_tick(99.6, '2026-10-01T00:06:40+00:00')
                shadow.on_tick(99.6, '2026-10-01T00:06:59+00:00')
                witnesses.append({'reference_delayed': delay_reference,
                                  'ohlc': [100., 100., 99.4, 99.6],
                                  'closed': len(shadow.closed_records),
                                  'exit_reason': shadow.closed_records[0]['exit_reason'] if shadow.closed_records else None,
                                  'exit_price': shadow.closed_records[0]['exit_price'] if shadow.closed_records else None,
                                  'open': len(shadow.open_positions)})
            finally:
                shadow.close()
    assert witnesses[0]['closed'] == 1 and witnesses[1]['closed'] == 0
    assert abs(witnesses[0]['exit_price'] - 99.4) < 1e-9
    coverage = []
    paths = [ROOT / p for p in (
        'data/market_bot_replay/klines/SOLUSDT_1m.jsonl',
        'data/studies/ge_replay/klines/SOLUSDT_1m.jsonl',
        'data/studies/be_off_cb_deterioration/klines/SOLUSDT_1m.jsonl',
        'data/analysis/price_structure_72h_20261010/market/SOLUSDT_1m.jsonl')]
    for p in paths:
        stamps = []
        if not p.exists():
            continue
        for line in p.open(encoding='utf8'):
            if line.strip():
                row = json.loads(line)
                stamps.append(row['open_time_ms'])
        unique = sorted(set(stamps))
        coverage.append({'path': str(p.relative_to(ROOT)), 'rows': len(stamps),
                         'duplicates': len(stamps) - len(unique),
                         'first_open_brt': iso(unique[0]),
                         'last_close_brt': iso(unique[-1] + 59999),
                         'internal_missing_minutes': sum(max(0, (b-a)//60000-1) for a,b in zip(unique,unique[1:]))})
    remote = json.loads((OUT / 'remote_audit.json').read_text(encoding='utf-8-sig'))
    result = {'decision': 'STOP_MATERIAL_FIDELITY_LIMITATION',
              'witnesses': witnesses, 'local_candle_coverage': coverage,
              'paired_fast_direct_delta_usdt': sum(r['net_delta_usdt'] for r in remote['fast_trades']),
              'warning': 'Four observed paired exits only; not a portfolio effect or a four-selector replay.',
              'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in [ROOT/'src/monitor/forward_experiment_shadows.py', ROOT/'tools/be_off_cb_fast_drop_systemic_replay.py', Path(__file__)]}}
    (OUT / 'feasibility.json').write_text(json.dumps(result, indent=2), encoding='utf8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
