"""Read-only staged release checks. Never deploys, restarts, or invokes Git."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.config_profiles import effective_config
from tools.keyed_vps_check import NEW


def differences(left,right,prefix=''):
    if isinstance(left,dict) and isinstance(right,dict):
        return [item for key in sorted(left.keys()|right.keys())
                for item in differences(left.get(key),right.get(key),prefix+'.'+key if prefix else key)]
    return [] if left==right else [prefix]


def verify(root, live_config=None, candidate_config=None, fallback=False):
    manifest=json.loads((root/'RELEASE_MANIFEST.json').read_text(encoding='utf8'))
    for name,digest in manifest['files_sha256'].items():
        file=(root/name).resolve()
        if not file.is_relative_to(root.resolve()) or hashlib.sha256(file.read_bytes()).hexdigest()!=digest:
            raise ValueError('Staged release file hash mismatch: '+name)
    receipt_path=root/'evidence/keyed_20261101/LIBERACAO_PREPARO.json'
    receipt=json.loads(receipt_path.read_text(encoding='utf8'))
    if receipt.get('gate_passed') is not True:
        raise ValueError('Approved parity receipt missing')
    for name,digest in receipt['economic_source_sha256'].items():
        file=root/name
        if hashlib.sha256(file.read_bytes()).hexdigest()!=digest:
            raise ValueError('Economic source differs from approved parity: '+name)
    raw=yaml.safe_load((candidate_config or root/'config/config.yaml').read_text(encoding='utf8'));cfg=effective_config(raw)
    if candidate_config:
        staged=yaml.safe_load((root/'config/config.yaml').read_text(encoding='utf8'))
        authorized={f'instrumentation.{key}.enabled' for key in
                    ('dmi15_trajectory_context_shadow','hs_bull_elastic_shadow')} if fallback else set()
        if any(path not in authorized for path in differences(staged,raw)):
            raise ValueError('Candidate config differs from prepared release beyond authorized fallback flags')
    for key in NEW:
        arm=cfg['instrumentation'][key]
        if arm['enabled'] is not True or arm['accept_new_entries'] is not True:
            raise ValueError('New arm not prepared for activation: '+key)
    for key in ('dmi15_trajectory_context_shadow','hs_bull_elastic_shadow'):
        arm=cfg['instrumentation'][key]
        if arm['enabled'] is not (not fallback) or arm['accept_new_entries'] is not False:
            raise ValueError('Unexpected legacy enabled/draining flags for selected release phase: '+key)
    paths=[]
    for key,arm in cfg['instrumentation'].items():
        if isinstance(arm,dict):
            for field in ('state_file','ledger_file','events_file'):
                if arm.get(field):
                    path=(root/arm[field]).resolve()
                    if key in NEW and not path.is_relative_to(root.resolve()/'data'):
                        raise ValueError('New arm path outside data: '+key)
                    paths.append(str(path))
    if len(paths)!=len(set(paths)):
        raise ValueError('State/ledger/event paths are not independent')
    if cfg['execution']['testnet_url'].rstrip('/')!='https://testnet.binance.vision':
        raise ValueError('Execution endpoint must remain Spot Testnet')
    if cfg['market_data']['rest_url'].rstrip('/')!='https://api.binance.com':
        raise ValueError('Market endpoint must remain real public Binance')
    if cfg['instrumentation']['keyed_validation']['minimum_available_mib']!=192:
        raise ValueError('Operator memory criterion must be 192 MiB')
    if cfg['instrumentation']['keyed_validation']['minimum_uptime_seconds']<1800:
        raise ValueError('Warmup must be at least 30 minutes')
    config_changes=[]
    if live_config:
        live=yaml.safe_load(live_config.read_text(encoding='utf8'))
        config_changes=differences(live,raw)
        allowed_prefixes=tuple('instrumentation.'+key for key in
            (*NEW,'keyed_policy','structure_repair','keyed_validation'))
        allowed_exact={f'instrumentation.{key}.accept_new_entries' for key in
                       ('dmi15_trajectory_context_shadow','hs_bull_elastic_shadow')}
        if fallback:
            allowed_exact.update(f'instrumentation.{key}.enabled' for key in
                                 ('dmi15_trajectory_context_shadow','hs_bull_elastic_shadow'))
        forbidden=[path for path in config_changes if path not in allowed_exact
                   and not any(path==prefix or path.startswith(prefix+'.') for prefix in allowed_prefixes)]
        if forbidden:
            raise ValueError('Unapproved live config differences (values omitted): '+', '.join(forbidden))
    return {'passed':True,'economic_sources_match_approved_parity':True,
            'new_arms_prepared':list(NEW),'legacy_drain_preserved':not fallback,'fallback_selected':fallback,
            'live_config_compared':bool(live_config),'config_changed_paths':config_changes,
            'deployment_performed':False,'vps_measurements':'PENDING_OPERATOR_EXECUTION'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--compare-live-config',type=Path)
    p.add_argument('--candidate-config',type=Path)
    p.add_argument('--fallback',action='store_true',help='Validate separately prepared, evidence-conditioned fallback')
    a=p.parse_args()
    try:
        result=verify(a.root.resolve(),a.compare_live_config,a.candidate_config,a.fallback)
    except Exception as exc:
        result={'passed':False,'error_type':type(exc).__name__,'error':str(exc)}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['passed'] else 2


if __name__=='__main__':raise SystemExit(main())
