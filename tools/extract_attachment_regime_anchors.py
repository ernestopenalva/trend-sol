"""Extract preserved regime anchor points only for the user-supplied trade subset. No replay or trading writes."""
from pathlib import Path
from datetime import datetime,timezone,timedelta
from collections import Counter,defaultdict
import json,hashlib,math
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'data/analysis/extremos_trades_anexo_20261023';TZ=timezone(timedelta(hours=-3))
PHASES=[('trend_open','ENTRADA - MARE 72h','1h','ME'),('wave_open','ENTRADA - ONDA 12h','15min','OE'),('trend_close','SAIDA - MARE 72h','1h','MS'),('wave_close','SAIDA - ONDA 12h','15min','OS')]
POINTS=[('topo_1','TOPO 1'),('topo_2','TOPO 2'),('fundo_1','FUNDO 1'),('fundo_2','FUNDO 2')]
STATES=['BULL','BEAR','EXPA','COMP']
def read(n):return json.loads((OUT/n).read_bytes())
def stamp(t):
 if t is None:return 'NAO DISPONIVEL'
 if isinstance(t,str):d=datetime.fromisoformat(t.replace('Z','+00:00'))
 else:d=datetime.fromtimestamp(t/1000,timezone.utc)
 return d.astimezone(TZ).strftime('%d/%m/%Y %H:%M:%S')+' BRT'
def ms(t):return int(datetime.fromisoformat(t.replace('Z','+00:00')).timestamp()*1000)
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def npoints(x):return sum(bool(x['record'].get(phase+'_details',{}).get(k)) for phase,_,__,___ in PHASES for k,_ in POINTS)
def signature(r):return tuple((p,r.get(p),json.dumps(r.get(p+'_details'),sort_keys=True)) for p,_,__,___ in PHASES)
def main():
 rows=read('trades_correspondentes.json');raw=read('ledger_captura.json');assert len(rows)==54
 issues=[];pointrows=[];counts=Counter();checks=[]
 for x in rows:
  r=x['record'];a=x['anexo'];counts[a['trend_open'],a['wave_open']]+=1
  for phase,title,tf,code in PHASES:
   d=r.get(phase+'_details') or {};at=ms(d['at']) if d.get('at') else None;start=d.get('window_start_ms');middle=d.get('midpoint_ms');end=d.get('window_end_ms');have=[d.get(k) for k,_ in POINTS]
   assert d.get('label')==r.get(phase)==a[phase],(x['id'],phase,'label')
   if all(have):
    t1,t2,b1,b2=have;dh=t2['price']-t1['price'];dl=b2['price']-b1['price'];label='UNDEFINED' if dh==0 or dl==0 else 'BULL' if dh>0 and dl>0 else 'BEAR' if dh<0 and dl<0 else 'EXPA' if dh>0 and dl<0 else 'COMP'
    assert label==d['label'],(x['id'],phase,'geometry')
    for k,p in zip([z[0] for z in POINTS],have):
     half=1 if k.endswith('_1') else 2;left,right=(start,middle) if half==1 else (middle,end)
     assert left<=p['open_ms']<right,(x['id'],phase,k,'half')
     assert p['closed_ms']<at,(x['id'],phase,k,'causality')
     pointrows.append({'trade_id':x['id'],'phase':phase,'point':k,'candle_open_BRT':stamp(p['open_ms']),'price':p['price'],'open_ms':p['open_ms'],'closed_ms':p['closed_ms'],'snapshot_at_ms':at,'source':d.get('source'),'timeframe':tf})
   else:
    assert not any(have) and d['label']=='UNAVAILABLE',(x['id'],phase,'partial')
    issues.append({'trade_id':x['id'],'phase':phase,'status':d.get('status'),'reason':d.get('undefined_reason'),'missing_candles':d.get('missing_candles') or d.get('missing_hours') or []})
  checks.append({'trade_id':x['id'],'points':npoints(x),'all_original_labels_match':True,'geometry_and_closed_candle_causality':True})
 # One example per entry combination, prefer a full set of preserved anchors, then earliest input order.
 groups=defaultdict(list)
 for x in rows:groups[x['anexo']['trend_open'],x['anexo']['wave_open']].append(x)
 selected=[min(v,key=lambda x:(-npoints(x),x['id'])) for k,v in groups.items()];order={v:i for i,v in enumerate(STATES+['UNDEFINED','UNAVAILABLE'])};selected.sort(key=lambda x:(order[x['anexo']['trend_open']],order[x['anexo']['wave_open']]))
 absent=[(t,w) for t in STATES for w in STATES if (t,w) not in groups]
 header=['TREND-SOL - COORDENADAS PARA CONFERENCIA VISUAL NO TRADINGVIEW','', 'SOMENTE OS TRADES DO ANEXO fbf38df4-512f-4ab1-8d4f-0ebf36695f48.','Braco identificado por correspondencia dos 54 trades: EMA_MACD_HIST_1M_SHADOW.','Mercado: SOL/USDT Binance Spot (SOLUSDT). Nao sao fills REAL_A/Testnet.','Nao houve replay economico, alteracao do bot, de parametros ou da Memoria Cientifica.','', 'COMO PLOTAR','1. Ajuste o fuso do grafico para UTC-3/BRT. Todos os horarios abaixo estao em BRT.','2. MARE: extremos de candles 1h; ONDA: extremos de candles 15min.','3. O horario de cada extremo e a ABERTURA do candle. Nao e o segundo exato do high/low intrabar.','4. Para cada bloco, trace uma reta TOPO 1 -> TOPO 2 e outra FUNDO 1 -> FUNDO 2.','5. 1 = primeira metade, mais antiga; 2 = segunda metade, mais recente.','6. Entrada e saida possuem janelas independentes: 4 blocos x (2 topos + 2 fundos).','   Conjunto completo por trade: 8 topos e 8 fundos. Pontos repetidos entre blocos sao preservados.','7. MARE: 72 candles 1h, duas metades de 36h. ONDA: 48 candles 15min, duas metades de 6h.','   Maior high e menor low de cada metade; empate intrametade escolhe o candle mais antigo.','   HH+HL=BULL; LH+LL=BEAR; HH+LL=EXPA; LH+HL=COMP. Igualdade entre metades=UNDEFINED.','8. Janela e metades usam [inicio, fim): fim exclusivo. Somente candles fechados antes do snapshot.','9. Origem BACKFILL = extremos reconstruidos e preservados no ledger, com disponibilidade idealizada','   dos candles fechados; nao prova disponibilidade no runtime historico. LIVE = snapshot recebido ao vivo.','10. UNAVAILABLE por lacuna: os quatro extremos desse bloco nao existem no snapshot; NAO foram inventados','    nem substituidos por candles publicos. Os horarios faltantes sao listados para diagnostico.','11. EMA/MACD, horarios, precos e PnL da linha-resumo reproduzem literalmente o anexo, inclusive STALE.','    O PnL nao foi recalculado. Detalhes e identidade vem do ledger correspondente; abaixo conserva-se a','    precisao de segundos do anexo e informa-se separadamente o timestamp do snapshot.','', 'COBERTURA DO SUBCONJUNTO',f'Trades: {len(rows)}. Com 16 pontos completos: {sum(npoints(x)==16 for x in rows)}.',f'Combinacoes estruturais na entrada: {sum(t in STATES and w in STATES for t,w in groups)}/16.',f'Combinacoes adicionais com ONDA UNAVAILABLE: {sum(w=="UNAVAILABLE" for t,w in groups)}.', 'Ausentes no anexo: '+', '.join(t+' x '+w for t,w in absent)+'.','', 'Combinacao de entrada | IDs dos trades do anexo | Quantidade']
 for (t,w),v in sorted(groups.items(),key=lambda z:(order[z[0][0]],order[z[0][1]])):
  header.append(t+' x '+w+' | '+', '.join(f'{x["id"]:02d}' for x in v)+' | '+str(len(v)))
 header+=['', 'CAMPOS DA LINHA-RESUMO','source_candle | entry BRT | entry price | exit BRT | exit price | PnL % | net | entry EMA | entry MACD | exit EMA | exit MACD | exit reason | trend_open | wave_open | trend_close | wave_close','']
 def render(x):
  r=x['record'];a=x['anexo'];lines=['='*110,f'TRADE {x["id"]:02d} - ENTRADA {a["trend_open"]} x {a["wave_open"]}',f'pair_id: {r["pair_id"]}',f'Fonte: {x["file"]}',f'Pontos preservados: {npoints(x)}/16','', ' | '.join(a.values()),'']
  for phase,title,tf,code in PHASES:
   d=r[phase+'_details'];lines += [f'[{code}] {title} | regime: {r[phase]} | grafico: {tf}',f'Snapshot: {stamp(d.get("at"))} | origem: {d.get("source")} | status: {d.get("status")}',f'Regra: {d.get("rule_version") or d.get("version")}',f'Janela: {stamp(d.get("window_start_ms"))} ate {stamp(d.get("window_end_ms"))} (fim exclusivo)',f'Divisao das metades: {stamp(d.get("midpoint_ms"))}',f'Ultimo candle disponivel - fechamento: {stamp(d.get("latest_closed_ms"))}',f'Geometria: topos {d.get("highs_class")} | fundos {d.get("lows_class")}']
   if not d.get('topo_1'):
    lines.append('ATENCAO: EXTREMOS INDISPONIVEIS NESTE SNAPSHOT. Motivo: '+str(d.get('undefined_reason')))
    miss=d.get('missing_candles') or d.get('missing_hours') or []
    lines.append('Candles faltantes (abertura BRT): '+('; '.join(stamp(t) for t in miss) if miss else 'nao informados'))
   for key,name in POINTS:
    p=d.get(key);lines.append(f'{code} {name} | '+(f'{stamp(p["open_ms"])} | preco: {float(p["price"]):.4f}' if p else 'NAO DISPONIVEL | preco: NAO DISPONIVEL'))
   lines.append('')
  return '\n'.join(lines)
 footer=['', '='*110,'RASTREABILIDADE','Anexo original preservado em anexo_original.txt.','Correspondencias completas em trades_correspondentes.json; captura de leitura em ledger_captura.json.','O timestamp de cada extremo corresponde a open_ms; aliases highs/lows nao foram contados como novos pontos.','Nao foi usada cotacao futura para completar um snapshot UNAVAILABLE.','Sha256 do anexo: '+sha(OUT/'anexo_original.txt'),'Sha256 da captura: '+sha(OUT/'ledger_captura.json'),'Captura readonly da VPS (UTC): '+raw['captured_utc'],'Arquivo fonte: data/trades/trades_ema_macd_hist_1m_shadow.jsonl','Sha256 do ledger lido: '+raw['remote_files_readonly']['data/trades/trades_ema_macd_hist_1m_shadow.jsonl']['sha256']]
 (OUT/'TODOS_OS_54_TRADES_COM_EXTREMOS.txt').write_text('\n'.join(header)+ '\n\n'+ '\n\n'.join(render(x) for x in rows)+'\n'+'\n'.join(footer)+'\n',encoding='utf-8-sig')
 note=['AMOSTRA PARA INICIAR A CONFERENCIA','Um trade por combinacao observada na ENTRADA. Criterio: preferir 16 pontos completos;','em empate, primeiro trade em ordem cronologica do anexo. Sem selecao por PnL.','Sao 17 trades: 14 combinacoes estruturais e 3 combinacoes com ONDA UNAVAILABLE.','IDs referem-se a numeracao do conjunto original dos 54 trades.','']
 (OUT/'AMOSTRA_17_COMBINACOES_COM_EXTREMOS.txt').write_text('\n'.join(note+header)+'\n\n'+'\n\n'.join(render(x) for x in selected)+'\n'+'\n'.join(footer)+'\n',encoding='utf-8-sig')
 (OUT/'PONTOS_E_VALIDACAO.json').write_text(json.dumps({'trade_checks':checks,'missing_snapshots':issues,'points':pointrows,'selected_trade_ids':[x['id'] for x in selected],'selection_rule':'entry combination, complete16 preferred, then earliest; no PnL selection','absent_structural_combinations':absent},ensure_ascii=False,indent=2),encoding='utf-8')
 manifest={'attachment_sha256':sha(OUT/'anexo_original.txt'),'captured_utc':raw['captured_utc'],'matched_arm':'EMA_MACD_HIST_1M_SHADOW','trades':len(rows),'complete16_trades':sum(npoints(x)==16 for x in rows),'points_available':len(pointrows),'missing_snapshots':len(issues),'sample_trades':len(selected),'checks':'identity/price/time/reason/labels, geometry from preserved anchors, half membership, closed before snapshot','files':{p.name:sha(p) for p in OUT.iterdir() if p.is_file() and p.name!='MANIFESTO.json'}}
 (OUT/'MANIFESTO.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({k:v for k,v in manifest.items() if k!='files'},ensure_ascii=False));print('SELECTED',[(x['id'],x['anexo']['trend_open'],x['anexo']['wave_open'],npoints(x)) for x in selected])
if __name__=='__main__':main()
