"""Nominal REAL_A admission audit against the actual PositionRegistry predicate, no constructor/network."""
import sys,json,collections,types
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];S=ROOT/'data/analysis/replay_12bracos_20261021';sys.path.insert(0,str(S/'frozen'));from src.monitor.position_registry import PositionRegistry
from src.monitor.entry_engine import EntrySignal

def category(reason):
 if reason in ['BLOCKED_MAX_POSITIONS','ENTRY_BLOCKED_SHADOW_CAPACITY']:return 'CAPACITY'
 if reason in ['BLOCKED_SPACING','ENTRY_BLOCKED_SPACING']:return 'SPACING'
 if reason=='ENTRY_BLOCKED_SAME_5M_CANDLE':return 'BUCKET'
 return reason

def main():
 cfg=json.loads((S/'config.json').read_bytes());sig={x['signal']['source_candle_open_time']:EntrySignal(**x['signal']) for x in json.loads((S/'signals.json').read_bytes())['REAL_A']};out=[];differences=[]
 for p in sorted((S/'runs').glob('REAL_A_*.json')):
  r=json.loads(p.read_bytes());registry=object.__new__(PositionRegistry);registry.config=cfg;registry.positions=[];registry._entries_by_admission_bucket={};registry._last_admission_details={};active={};by={x['source']:x for x in r['records']};details=[]
  for a in r['admissions']:
   at=a['at'];active={k:v for k,v in active.items() if v['close_ms'] is None or v['close_ms']>=at};registry.positions=[types.SimpleNamespace(status='OPEN',pair_id=x['pair_id'],position_id=x['source'],entry_price=x['entry']) for x in active.values()];s=sig[a['source']];reason=registry._admission_block_reason(s);expected=reason is None
   if expected!=a['accepted']:differences.append({'case':p.name,'source':a['source'],'registry_allowed':expected,'model_allowed':a['accepted'],'registry_reason':reason,'model_reason':a['reason']})
   details.append({'source':a['source'],'accepted':a['accepted'],'registry_reason':reason or 'ADMITTED','proxy_reason':a['reason']})
   if a['accepted']:
    active[a['source']]=by[a['source']];bucket=registry._admission_bucket(a['source']);registry._entries_by_admission_bucket[bucket]=registry._entries_by_admission_bucket.get(bucket,0)+1
  out.append({'case':p.name,'N':len(details),'allowed_decisions_match':not any(x['case']==p.name for x in differences),'registry_reasons':dict(collections.Counter(x['registry_reason'] for x in details)),'label_spelling_differences':sum(not x['accepted'] and x['registry_reason']!=x['proxy_reason'] for x in details),'constraint_priority_differences':sum(not x['accepted'] and category(x['registry_reason'])!=category(x['proxy_reason']) for x in details),'details':details})
 result={'cases':out,'mismatches':differences,'complete_6_cases':len(out)==6,'scope':'nominal max positions, 5m bucket, spacing and valid ATR. Instant successful fills assumed; no historical EXIT_PENDING/manual review/exchange balance or lot filters reproduced.'};(S/'AUDITORIA_ADMISSAO_REAL_A.json').write_text(json.dumps(result,separators=(',',':')),encoding='utf-8');print(json.dumps({'cases':len(out),'boolean_decision_mismatches':len(differences),'label_spelling_differences':sum(x['label_spelling_differences'] for x in out),'constraint_priority_differences':sum(x['constraint_priority_differences'] for x in out)}));assert not differences
if __name__=='__main__':main()
