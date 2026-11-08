"""Validate the prepared K1 release inputs without Git, deploy, or restart."""
import hashlib
import json
from pathlib import Path
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
K1 = 'ema_macd_cluster_act10_k1_shadow'
BASE = 'be_off_cb_ema_macd_cluster_act10_shadow'
DISABLED = ('dmi15_trajectory_context_shadow', 'hs_bull_elastic_shadow')
PARITY = ROOT / 'data/analysis/ema_macd_cluster_act10_k1/PARIDADE_COMPLETA.json'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate(root=ROOT):
    root = Path(root).resolve()
    config = yaml.safe_load((root / 'config/config.yaml').read_text(encoding='utf8'))
    instrumentation = config['instrumentation']
    k1 = instrumentation[K1]
    base = instrumentation[BASE]
    if (k1.get('enabled') is not True or k1.get('accept_new_entries') is not True
            or k1.get('initial_capital_usdt') != base.get('initial_capital_usdt')
            or k1.get('max_open_positions') != 5 or k1.get('trail_activation_atr') != 10
            or k1.get('trail_gap_atr') != 13 or k1.get('spacing_queda_atr') != 1):
        raise ValueError('K1 settings differ from the approved base/one-rule spec')
    if config['entry'].get('max_entries_per_candle') != 1 or config['entry'].get('entry_spacing_atr') != 1:
        raise ValueError('Base admission/bucket/spacing settings changed')
    for key in DISABLED:
        if instrumentation[key].get('enabled') is not False:
            raise ValueError('DMI15 and HS_BULL must be disabled in this release')
    paths = []
    for key, settings in instrumentation.items():
        if not isinstance(settings, dict):
            continue
        for field in ('state_file', 'ledger_file', 'events_file'):
            if settings.get(field):
                paths.append(str(settings[field]))
    if len(paths) != len(set(paths)):
        raise ValueError('Shadow state, ledger, and event paths must be unique')
    if config['execution']['testnet_url'].rstrip('/') != 'https://testnet.binance.vision':
        raise ValueError('Execution endpoint is not Spot Testnet')
    if config['market_data']['rest_url'].rstrip('/') != 'https://api.binance.com':
        raise ValueError('Market data endpoint is not public Binance')
    proof = json.loads((root / PARITY.relative_to(ROOT)).read_text(encoding='utf8'))
    cases = {row['path']: row for row in proof.get('cases', [])}
    if proof.get('passed') is not True or set(cases) != {'LOW_FIRST', 'HIGH_FIRST'} or not all(x['passed'] for x in cases.values()):
        raise ValueError('Both complete K1 parity paths have not passed')
    for path, values in {'LOW_FIRST': (942, -31.597134, 186),
                         'HIGH_FIRST': (943, -38.111394, 184)}.items():
        row = cases[path]
        actual = row['candidate']
        if actual['trades'] != values[0] or actual['k1_blocks'] != values[2] or abs(actual['net_total']-values[1]) > 0.0000005:
            raise ValueError('K1 reference totals differ: '+path)
        if not (row.get('all_economic_trade_fields_equal_except_pair_id')
                and row.get('stats_equal') and row.get('admissions_equal')):
            raise ValueError('Trade/equity/admission parity is incomplete: '+path)
    if proof.get('runtime_source_sha256') != digest(root/'src/monitor/keyed_shadow.py'):
        raise ValueError('K1 runtime source changed after parity')
    if proof.get('context_gate_source_sha256') != digest(root/'src/monitor/context_shadow.py'):
        raise ValueError('Admission gate source changed after parity')
    return {'passed': True, 'parity_cases': 2,
            'k1_enabled': True, 'dmi15_enabled': False, 'hs_bull_enabled': False,
            'trade_records_economic_parity': True,
            'reference_fast_evaluated_audit_field_differences': {
                path: cases[path]['reference_only_fast_evaluated_audit_differences']
                for path in ('LOW_FIRST', 'HIGH_FIRST')},
            'VPS_memory_validation': 'PENDING_OPERATOR_MEASUREMENT',
            'git_commands_performed': False, 'deployment_performed': False,
            'restart_performed': False}


if __name__ == '__main__':
    try:
        print(json.dumps(validate(), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'passed': False, 'error_type': type(error).__name__, 'error': str(error)},
                         ensure_ascii=False, indent=2))
        raise SystemExit(2)
