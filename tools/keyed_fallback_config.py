"""Prepare a separate fallback YAML after a valid failed BEFORE/AFTER memory window.

Never applies it, stops a process, or changes state/ledger/history files.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import yaml

KEYS=('dmi15_trajectory_context_shadow','hs_bull_elastic_shadow')


def fallback(source, evidence):
    if (evidence.get('mode')!='resources' or evidence.get('phase') not in ('before','after')
        or evidence.get('passed') is not False or not evidence.get('warmed_up')
        or not evidence.get('same_processes_throughout') or not evidence.get('sufficient_samples')):
        raise ValueError('Requires a complete, warmed, failed BEFORE/AFTER memory observation; FALLBACK failure must be reported')
    settings=evidence['settings']
    failed=(evidence['minimum_memory_available_mib'] < settings['minimum_available_mib']
            or evidence['longest_swap_growth_intervals'] >= settings['continuous_swap_intervals']
            or evidence['longest_paging_intervals'] >= settings['continuous_swap_intervals'])
    if not failed:
        raise ValueError('No authorized memory/swap criterion failed; report other failures')
    result=deepcopy(source)
    for key in KEYS:
        arm=result['instrumentation'][key]
        if arm['enabled'] is not True or arm['accept_new_entries'] is not False:
            raise ValueError(f'{key}: expected enabled, draining baseline; do not overwrite unexpected config')
        arm['enabled']=False
    return result


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True,help='New evidence folder, outside the live config')
    a=p.parse_args()
    source=yaml.safe_load(a.config.read_text(encoding='utf8'))
    evidence=json.loads(a.evidence.read_text(encoding='utf8'))
    result=fallback(source,evidence)
    a.output.mkdir(parents=True,exist_ok=False)
    content=yaml.safe_dump(result,sort_keys=False,allow_unicode=True)
    with (a.output/'config_fallback.yaml').open('x',encoding='utf8') as handle:handle.write(content)
    receipt={'applied':False,'changed_flags':{key+'.enabled':False for key in KEYS},
             'source_config_sha256':hashlib.sha256(a.config.read_bytes()).hexdigest(),
             'resource_evidence_sha256':hashlib.sha256(a.evidence.read_bytes()).hexdigest(),
             'prepared_config_sha256':hashlib.sha256(content.encode('utf8')).hexdigest(),
             'state_ledger_history_modified':False,
             'open_shadow_positions':'Not liquidated: disabled arms stop managing/restoring positions; checkpoint retained',
             'operator_action':'Apply only the two approved flags during a clean stop, then restart and measure again after 30 min. If still failing, report.'}
    with (a.output/'RECEIPT.json').open('x',encoding='utf8') as handle:json.dump(receipt,handle,indent=2)
    print(str(a.output/'config_fallback.yaml'))


if __name__=='__main__':main()
