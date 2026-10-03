"""Read-only audit of recorded forward exit snapshot recency, no reconstruction."""
import argparse
import hashlib
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from tools.forward_experiment_report import _recorded_exit_context, parse_time


def audit(rows):
    output=[]
    for r in rows:
        if not r.get('closed_at'):continue
        at=parse_time(r['closed_at']); c=(r.get('market_context_exit') or {}).get('tf_5m') or {}
        closed=c.get('latest_closed_at_ms')
        checked=_recorded_exit_context(r)
        status=checked.get('ema_context')
        status=status if status in ('STALE','UNAVAILABLE') else (
            'VALID' if status and checked.get('macd_context') not in (None,'UNAVAILABLE') else 'UNAVAILABLE')
        output.append({'pair_id':r.get('pair_id'),'source_candle':r.get('source_candle_open_time'),
            'opened_at':r.get('opened_at'),'closed_at':r['closed_at'],'reason':r.get('exit_reason'),
            'status':status,'snapshot_close_ms':closed,
            'snapshot_age_min':(at.timestamp()*1000-closed)/60000 if at and closed is not None else None,
            'recorded_ema':c.get('ema_context'),'recorded_macd':c.get('macd_context')})
    return output


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger',required=True,type=Path)
    parser.add_argument('--output-dir',required=True,type=Path)
    args=parser.parse_args();before=hashlib.sha256(args.ledger.read_bytes()).hexdigest()
    rows=audit([json.loads(l) for l in args.ledger.read_text(encoding='utf-8').splitlines() if l.strip()])
    summary={'ledger':str(args.ledger.resolve()),'sha256':before,'closed':len(rows),
             'counts':dict(Counter(r['status'] for r in rows)),
             'by_reason':{reason:dict(Counter(r['status'] for r in rows if r['reason']==reason))
                          for reason in sorted({r['reason'] for r in rows})},
             'first_opened_at':min((r['opened_at'] for r in rows if r['opened_at']),default=None),
             'last_closed_at':max((r['closed_at'] for r in rows),default=None)}
    for status in ('VALID','STALE','UNAVAILABLE'):
        ages=[r['snapshot_age_min'] for r in rows if r['status']==status and r['snapshot_age_min'] is not None]
        summary[status+'_age_min']={'n':len(ages),'min':min(ages) if ages else None,
                                    'median':statistics.median(ages) if ages else None,'max':max(ages) if ages else None}
    args.output_dir.mkdir(parents=True,exist_ok=True)
    (args.output_dir/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    (args.output_dir/'exits.jsonl').write_text('\n'.join(json.dumps(r) for r in rows)+'\n',encoding='utf-8')
    lines=['# Auditoria retroativa da telemetria de saída BE_OFF_CB','',
           f"Período dos registros disponíveis: {summary['first_opened_at']} → {summary['last_closed_at']} (timestamps UTC).",
           f'Ledger SHA256: {before}. Nenhum registro foi reescrito.',
           'Auditoria de telemetria, não estudo de performance: inclui todos os registros disponíveis; não declara válidas coortes operacionais invalidadas anteriormente.',
           'VALID = snapshot do último fechamento 5m elegível (boundary−1ms). STALE = mais antigo; UNAVAILABLE = ausente, futuro ou timestamp inválido.',
           'Validade temporal não atesta equivalência do vetor live com REST nem exatidão matemática dos labels. Sem reconstruir ou rerodar estudos.', '',
           '| exit reason | VALID | STALE | UNAVAILABLE |','|---|---:|---:|---:|']
    for reason,counts in summary['by_reason'].items():
        lines.append(f"| {reason} | {counts.get('VALID',0)} | {counts.get('STALE',0)} | {counts.get('UNAVAILABLE',0)} |")
    lines += ['',f"Total: {len(rows)}; {summary['counts']}", '',
              'Idades dos snapshots em minutos:',json.dumps({s:summary[s+'_age_min'] for s in ('VALID','STALE','UNAVAILABLE')},indent=2),
              '', 'Identidades, timestamps e idades individuais em exits.jsonl.']
    (args.output_dir/'audit.md').write_text('\n'.join(lines),encoding='utf-8')
    assert hashlib.sha256(args.ledger.read_bytes()).hexdigest()==before
    print(json.dumps(summary,indent=2))


if __name__=='__main__':main()
