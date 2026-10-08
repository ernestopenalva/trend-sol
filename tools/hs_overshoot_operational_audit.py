"""Read-only remote evidence capture for historical stop overshoot audit."""
import json
import subprocess
from datetime import datetime,timezone,timedelta
from collections import defaultdict
from pathlib import Path

OUT=Path(__file__).resolve().parents[1]/'data/analysis/hs_overshoot_20261008'
BRT=timezone(timedelta(hours=-3))
SOURCES=(1791333780000,1791334620000)
ARMS={'REAL_A':'trades_B','BE_OFF_CB':'trades_be_off_cb_shadow','FAST_DROP_EMA':'trades_be_off_cb_fast_drop_ema_shadow','HS_BULL':'trades_hs_bull_elastic_shadow','HS_BEAR':'trades_hs_bear_cluster_exit_shadow','CB_EXIT_ALL':'trades_cb_exit_all_shadow','MACD_BU_MINUS':'trades_be_off_cb_macd_bu_minus_shadow','ACT20_GAP5':'trades_be_off_cb_act20_gap5_shadow','ACT10_GAP13':'trades_be_off_cb_act10_gap13_shadow'}

def stamp(s):return datetime.fromisoformat(s)
def brt(v):
    if isinstance(v,(int,float)):v=datetime.fromtimestamp(v/1000,timezone.utc)
    if isinstance(v,str):v=stamp(v)
    return v.astimezone(BRT).strftime('%d/%m/%Y %H:%M:%S.%f')[:-3]

def economics(r):
    entry=float(r['entry_price']);exit=float(r['exit_price'])
    stop=r.get('hard_stop_price')
    if stop is None and r.get('hard_stop_pct') is not None:stop=entry*(1-float(r['hard_stop_pct'])/100)
    if stop is None:return None
    fees=float(r.get('estimated_fees_pct') or 0)
    qty=float(r['qty'])
    return {'stop':float(stop),'gross_pct':(exit/entry-1)*100,'expected_gross_pct':(float(stop)/entry-1)*100,'expected_net_pct':(float(stop)/entry-1)*100-fees,'net_pct':r.get('net_pnl_pct'),'overshoot_pp':(float(stop)-exit)/entry*100,'price_below_stop':float(stop)-exit,'additional_usd':max(0,(float(stop)-exit)*qty),'qty':qty,'fees_model_pct':fees}

def analyze():
    raw=json.loads((OUT/'remote_evidence.json').read_text());public=json.loads((OUT/'public_1m.json').read_text())
    ledgers={Path(p).stem:v for p,v in raw['ledgers'].items() if p.startswith('data/trades/')}
    ledgers['trades_B']=[r for r in ledgers['trades_B'] if not r.get('phantom') and r.get('position_type')=='BOT_EXIT']
    comparisons=[]
    for arm,file in ARMS.items():
        for source in SOURCES:
            matches=[r for r in ledgers[file] if r.get('source_candle_open_time')==source]
            comparisons.append({'arm':arm,'source':source,'opened':bool(matches),'trades':matches,'economics':[economics(r) for r in matches]})
    since=stamp('2026-09-19T00:00:00-03:00');summary=[];overs=[]
    for arm,file in ARMS.items():
        hs=[r for r in ledgers[file] if r.get('exit_reason')=='HARD_STOP' and stamp(r['closed_at'])>=since]
        seen=set();clean=[]
        for r in hs:
            key=(r.get('pair_id'),r['closed_at'])
            if key not in seen:clean.append(r);seen.add(key)
        items=[]
        for r in clean:
            e=economics(r)
            # Requested initial triage: >1.60% gross loss when expected ~1.50%.
            if e and -e['gross_pct']>1.60 and abs(e['expected_gross_pct']+1.5)<1e-6:
                item={'arm':arm,'source':r.get('source_candle_open_time'),'closed_at':r['closed_at'],'entry_price':r['entry_price'],'exit_price':r['exit_price'],'pair_id':r.get('pair_id'),**e};items.append(item);overs.append(item)
        summary.append({'arm':arm,'HS':len(clean),'overshoot':len(items),'pct':100*len(items)/len(clean) if clean else None,'additional_usd':sum(x['additional_usd'] for x in items)})
    candles=public['candles'];control=next(x for x in comparisons if x['arm']=='BE_OFF_CB' and x['source']==SOURCES[0])
    crossings=[]
    for source in SOURCES:
        c=next(x for x in comparisons if x['arm']=='BE_OFF_CB' and x['source']==source);r=c['trades'][0];e=c['economics'][0]
        first=next(x for x in candles if float(x[3])<=e['stop'])
        close=stamp(r['closed_at']).timestamp()*1000
        crossings.append({'source':source,'first_minute_open_ms':first[0],'first_minute_close_ms':first[6],'delay_lower_sec':(close-(first[0]+60000))/1000,'delay_upper_sec':(close-first[0])/1000,**e})
    logs=[]
    for p,obj in raw['logs'].items():
        for line in obj['hits']:
            try:r=json.loads(line)
            except:continue
            t=r.get('ts')
            if t and stamp('2026-10-07T01:45:00+00:00')<=stamp(t)<=stamp('2026-10-07T02:15:59+00:00'):logs.append({'file':p,'record':r})
    timeline=sorted([x for x in logs if x['file']=='logs/system.log'],key=lambda x:x['record']['ts'])
    extra=[]
    used=set(ARMS.values())
    for file,trades in ledgers.items():
        if file in used or 'top3' in file:continue
        hs=[r for r in trades if r.get('exit_reason')=='HARD_STOP' and r.get('symbol')=='SOLUSDT' and stamp(r['closed_at'])>=since]
        seen=set();rows=[]
        for r in hs:
            key=(r.get('pair_id'),r['closed_at'])
            if key in seen:continue
            seen.add(key);e=economics(r)
            if e and -e['gross_pct']>1.60 and abs(e['expected_gross_pct']+1.5)<1e-6:rows.append({'source':r.get('source_candle_open_time'),'closed_at':r['closed_at'],'exit_price':r['exit_price'],**e})
        if hs:extra.append({'file':file,'HS':len(seen),'overshoot':len(rows),'rows':rows,'additional_usd':sum(x['additional_usd'] for x in rows)})
    result={'captured_at':raw['captured_at'],'comparisons':comparisons,'crossings':crossings,'summary':summary,'overshoots':overs,'additional_SOLUSDT_arms':extra,'timeline':timeline,'internal_records':logs,'episode_classification':{'both_sources':['MARKET_DATA_GAP','RECONNECT_CATCHUP','NOT_DETERMINABLE'],'scope':'confirmed 60s disconnect and post-reconnect reaction; cause of preceding multi-minute coverage delay cannot be separated into network, consumer or socket backlog','PROCESSING_LAG':'end-to-end stale processing evidence; consumer CPU/IO attribution not determined','QUEUE_BACKLOG':'NOT_DETERMINABLE','SHADOW_PATH_LAG':'not supported as exclusive cause; REAL_A shared trigger','STOP_LOGIC':'no evidence of erroneous predicate; crossed received price triggers correct stop'}}
    (OUT/'audit.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    report=['# Auditoria operacional HARD_STOP — 06/10 23:05 BRT','',f"Captura read-only: {raw['captured_at']}. Mercado público coletado primeiro. Sem alteração de runtime/estado.",'','## Referência pública','', '| BRT open | open | high | low | close |','|---|---:|---:|---:|---:|']
    for x in candles:report.append('| '+brt(x[0])+' | '+' | '.join(x[1:5])+' |')
    report+=['','## Cruzamentos e perdas adicionais','', '| source BRT | stop | primeiro minuto comprovado | fechamento | atraso comprovável (s) | gross % | overshoot p.p. | adicional $ |','|---|---:|---|---|---:|---:|---:|---:|']
    for c in crossings:report.append(f"| {brt(c['source'])} | {c['stop']:.5f} | {brt(c['first_minute_open_ms'])}–+1m | 06/10 23:05:45.147 | {c['delay_lower_sec']:.3f}–{c['delay_upper_sec']:.3f} | {c['gross_pct']:.6f} | {c['overshoot_pp']:.6f} | {c['additional_usd']:.6f} |")
    report+=['','Cruzamento comprovado pelo low, NÃO tick exato. Valores adicionais = qty × (stop teórico − fill); referência idealizada de stop, não execução garantida. Fees modeladas constantes se cancelam na diferença; não usar spread histórico de replay como se aplicado ao phantom forward.','', '## Comparação braço a braço','', '| arm | source | abriu | entry BRT / preço | exit BRT / preço | motivo | gross % | net % | stop − saída | adicional $ |','|---|---|---|---|---|---|---:|---:|---:|---:|']
    for c in comparisons:
        if not c['opened']:report.append(f"| {c['arm']} | {brt(c['source'])} | NO | N/A | N/A | N/A | N/A | N/A | N/A | N/A |");continue
        for r,e in zip(c['trades'],c['economics']):report.append(f"| {c['arm']} | {brt(c['source'])} | YES | {brt(r['opened_at'])} / {r['entry_price']:.4f} | {brt(r['closed_at'])} / {r['exit_price']:.4f} | {r['exit_reason']} | {e['gross_pct']:.6f} | {e['net_pct']:.6f} | {e['price_below_stop']:.5f} | {e['additional_usd']:.6f} |")
    report+=['','## Timeline interna (relógio local dos logs system)','']
    for x in timeline:report.append(f"- {brt(x['record']['ts'])}: `{json.dumps(x['record'],ensure_ascii=False)}`")
    report+=['','## Recorrência desde 19/09 00:00 BRT','', '| arm | N HS | >1,60% | % | adicional $ |','|---|---:|---:|---:|---:|']
    for x in summary:report.append(f"| {x['arm']} | {x['HS']} | {x['overshoot']} | {x['pct']:.2f}% | {x['additional_usd']:.6f} |" if x['pct'] is not None else f"| {x['arm']} | 0 | 0 | N/A | 0 |")
    report+=['','## Todos os overshoots destacados','', '| arm | exit BRT | source BRT | stop | exit price | gross % | expected net % | actual net % | overshoot p.p. | adicional $ |','|---|---|---|---:|---:|---:|---:|---:|---:|---:|']
    for x in sorted(overs,key=lambda x:(x['closed_at'],x['arm'])):report.append(f"| {x['arm']} | {brt(x['closed_at'])} | {brt(x['source'])} | {x['stop']:.5f} | {x['exit_price']:.4f} | {x['gross_pct']:.6f} | {x['expected_net_pct']:.6f} | {x['net_pct']:.6f} | {x['overshoot_pp']:.6f} | {x['additional_usd']:.6f} |")
    report+=['','Não somar prejuízos de shadows como perda real da conta. Cada braço é trajetória sintética própria; um episódio compartilhado produz várias linhas correlacionadas. O cálculo é custo relativo ao preço ideal do stop, NÃO identificação causal integral do prejuízo. Casos históricos sem telemetria de atraso permanecem NOT_DETERMINABLE.','']
    report+=['## Outros braços SOLUSDT retidos (exclui TOP3 multi-market)','', '| arquivo | HS | overshoot >1,60% | adicional $ |','|---|---:|---:|---:|']
    for x in extra:report.append(f"| {x['file']} | {x['HS']} | {x['overshoot']} | {x['additional_usd']:.6f} |")
    report+=['','Linhas completas dos casos destes braços adicionais: audit.json → additional_SOLUSDT_arms.','']
    (OUT/'RELATORIO.md').write_text('\n'.join(report),encoding='utf8')
    print(json.dumps({'summary':summary,'crossings':crossings,'overshoot_dates':sorted(set(brt(x['closed_at'])[:10] for x in overs))},indent=2))

def supplement():
    data=remote("""
import json,hashlib,subprocess
from pathlib import Path
paths=['src/app.py','src/position/bot_full_engine.py','src/position/phantom_execution.py','src/monitor/ws_manager.py','src/monitor/circuit_breaker_shadow.py','src/logging_utils.py']
out={'sources':{p:Path(p).read_text() for p in paths},'git_head':subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip(),'archive_files':[str(p) for p in Path('data/trades/archive').rglob('*') if p.is_file()],'system_extra':[],'source_2157_events':[]}
for line in Path('logs/system.log').open():
 if any(x in line for x in ['2026-10-05T18:5','2026-10-05T18:4','2026-10-06T01:','20261005_1552']):out['system_extra'].append(line.strip())
for line in Path('logs/decisions.jsonl').open():
 if '1791334620000' in line:out['source_2157_events'].append(line.strip())
print(json.dumps(out))
""")
    (OUT/'supplement.json').write_text(json.dumps(data,indent=2),encoding='utf8')
    print('Supplement captured; source parity:',{p:(Path(__file__).resolve().parents[1]/p).read_text()==s for p,s in data['sources'].items()})

def remote(script):
    r=subprocess.run(['ssh','-o','BatchMode=yes','root@207.154.197.12','cd /root/trend-sol && venv/bin/python -'],input=script,text=True,capture_output=True)
    if r.returncode:raise RuntimeError(r.stderr[-4000:])
    return json.loads(r.stdout)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    # Independent public candles collected before internal evidence.
    public=remote("""
import json,urllib.request,urllib.parse,datetime
start=int(datetime.datetime.fromisoformat('2026-10-06T22:45:00-03:00').timestamp()*1000)
end=int(datetime.datetime.fromisoformat('2026-10-06T23:16:00-03:00').timestamp()*1000)-1
q=urllib.parse.urlencode(dict(symbol='SOLUSDT',interval='1m',startTime=start,endTime=end,limit=1000))
print(json.dumps({'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'url':'https://api.binance.com/api/v3/klines?'+q,'candles':json.load(urllib.request.urlopen('https://api.binance.com/api/v3/klines?'+q,timeout=30))}))
""")
    (OUT/'public_1m.json').write_text(json.dumps(public,indent=2),encoding='utf8')
    evidence=remote("""
import json,datetime
from pathlib import Path
result={'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'ledgers':{},'samples':{},'logs':{},'states':{}}
for p in list(Path('data/trades').glob('*.jsonl'))+[Path('logs/trades.jsonl')]:
 rows=[]
 for line in p.open():
  try:r=json.loads(line)
  except:continue
  rows.append(r)
 result['ledgers'][str(p)]=rows
for folder in ('data/telemetry','logs'):
 for p in Path(folder).glob('*'):
  if not p.is_file():continue
  if p.name=='trades.jsonl':continue
  hits=[];sample=None
  for line in p.open(errors='replace'):
   if sample is None:sample=line.strip()
   # UTC window plus wider wall-clock window; retain all records around incident.
   if any(x in line for x in ('2026-10-07T01:','2026-10-07T02:','2026-10-07 01:','2026-10-07 02:','2026-10-06 22:','2026-10-06 23:')):hits.append(line.strip())
  result['logs'][str(p)]={'bytes':p.stat().st_size,'hits':hits,'sample':sample}
for p in Path('data/state').glob('*.json'):
 if 'shadow' not in p.name and 'position' not in p.name:continue
 try:s=json.load(p.open())
 except:continue
 if not isinstance(s,dict):continue
 result['states'][str(p)]={k:v for k,v in s.items() if k not in ('closed_records','audit_events','positions')}
print(json.dumps(result))
""")
    (OUT/'remote_evidence.json').write_text(json.dumps(evidence),encoding='utf8')
    print('Public candles:',len(public['candles']))
    print('Ledger files:',len(evidence['ledgers']))
    print('Log files:',len(evidence['logs']))

if __name__=='__main__':
    import sys
    if '--analyze' in sys.argv:analyze()
    elif '--supplement' in sys.argv:supplement()
    else:main()
