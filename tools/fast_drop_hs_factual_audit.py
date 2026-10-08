"""Read-only factual audit. Unknown historical inputs are never inferred as rejects."""
import hashlib
import json
import subprocess
from collections import Counter
from datetime import datetime,timezone,timedelta
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.monitor.fast_drop_semantics import fast_drop_boundary,fast_drop_values,loss_reached

OUT=ROOT/'data/analysis/fast_drop_hs_audit_20261007'
SINCE=datetime.fromisoformat('2026-10-06T12:00:00-03:00')
BRT=timezone(timedelta(hours=-3))


def stamp(value):return datetime.fromisoformat(value)
def brt(value):
    if isinstance(value,(float,int)):value=datetime.fromtimestamp(value/1000,timezone.utc)
    if isinstance(value,str):value=stamp(value)
    return value.astimezone(BRT).strftime('%d/%m/%Y %H:%M:%S.%f')[:-3]


def remote_inventory():
    # All remote actions below are reads. No writes, imports of runtime, recovery or restart.
    script=r'''
import json,collections,hashlib
from pathlib import Path
files=['src/app.py','src/monitor/forward_experiment_shadows.py','src/monitor/fast_drop_semantics.py','src/monitor/circuit_breaker_shadow.py','src/trade_ledger.py']
sources={p:Path(p).read_text() for p in files}
result={'captured_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),'sources':sources}
for name in ('market_shadow_events','trough_events'):
    counts=collections.Counter(); samples={}
    with open('data/telemetry/'+name+'.jsonl') as f:
        for line in f:
            if '2026-10-06' not in line and '2026-10-07' not in line:continue
            r=json.loads(line)
            if name=='market_shadow_events' and r.get('symbol')!='SOLUSDT':continue
            kind=r.get('event') or r.get('event_type') or 'NO_EVENT'
            counts[kind]+=1;samples.setdefault(kind,r)
    result[name]={'counts':dict(counts),'samples':samples}
print(json.dumps(result))
'''
    result=subprocess.run(['ssh','-o','BatchMode=yes','root@207.154.197.12',
        'cd /root/trend-sol && venv/bin/python -'],input=script,text=True,capture_output=True,check=True)
    data=json.loads(result.stdout)
    (OUT/'remote_inventory.json').write_text(json.dumps(data,indent=2),encoding='utf8')
    return data


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--remote-inventory',action='store_true')
    args=parser.parse_args()
    if args.remote_inventory:remote_inventory()
    raw=OUT/'raw';control=json.loads((raw/'be_off_cb_shadow.json').read_text())
    fast=json.loads((raw/'be_off_cb_fast_drop_ema_shadow.json').read_text())
    hs=[r for r in control['closed_records'] if r['exit_reason']=='HARD_STOP' and stamp(r['closed_at'])>=SINCE]
    fast_by={r['source_candle_open_time']:r for r in fast['closed_records']}
    points=[(stamp(t),p) for t,p in fast.get('market_points',[])]
    rows=[]
    for r in hs:
        paired=fast_by.get(r['source_candle_open_time']);opened=stamp(r['opened_at']);closed=stamp(r['closed_at'])
        samples=[(t,p) for t,p in points if opened<=t<=closed and loss_reached(r['entry_price'],p)]
        sample=samples[0] if samples else None
        rows.append({'source_candle':brt(r['source_candle_open_time']),'source_ms':r['source_candle_open_time'],
            'entry':brt(opened),'entry_price':r['entry_price'],'HS':brt(closed),
            'control_pair':r['pair_id'],'fast_pair':paired['pair_id'] if paired else None,
            'same_entry_exit':bool(paired and all(r[k]==paired[k] for k in ('opened_at','closed_at','entry_price','exit_price','exit_reason'))),
            'first_crossing':None,'pnl_at_first_crossing':None,'EMA_at_evaluation':None,
            'EMA_freshness':None,'reference_in_buffer_at_evaluation':None,
            'speed_at_evaluation':None,'evaluated_flag_at_close':paired.get('fast_drop_evaluated') if paired else None,
            'retry_after_missing_buffer':None,'first_failed_predicate':None,
            'final_classification':'OTHER','classification_reason':'Historical evaluation evidence not persisted; cannot distinguish rejection, unavailable buffer or stale context.',
            'first_retained_sample_below_loss_NOT_FIRST_CROSSING':None if sample is None else {
                'at':brt(sample[0]),'price':sample[1],'pnl_pct':(sample[1]/r['entry_price']-1)*100,
                'reference_boundary_REQUIRED_NOT_PROVEN_AVAILABLE':brt(fast_drop_boundary(int(sample[0].timestamp()*1000))-300000)},
            'predicates':{'loss_ever_reached':True,'not_previously_evaluated_at_first_crossing':None,
                'reference_available':None,'normal_stop_does_not_precede':None,
                'speed_lte_minus_010':None,'EMA_in_SHO_BEA':None,'MACD':'NOT_APPLICABLE'}})
    inventory_path=OUT/'remote_inventory.json';inventory=json.loads(inventory_path.read_text()) if inventory_path.exists() else {}
    source_match={p:hashlib.sha256(code.encode()).hexdigest()==hashlib.sha256((ROOT/p).read_bytes()).hexdigest()
        for p,code in inventory.get('sources',{}).items()}
    # CRLF is irrelevant to code parity: normalize only for comparison.
    for p,code in inventory.get('sources',{}).items():source_match[p]=code.replace('\r\n','\n')==(ROOT/p).read_text().replace('\r\n','\n')
    result={'since':SINCE.isoformat(),'control_updated_at':control['updated_at'],'fast_updated_at':fast['updated_at'],
        'remote_source_matches_local':source_match,'HS_count':len(rows),
        'FAST_DROP_events_since':sum(e['event']=='FAST_DROP' and stamp(e['ts'])>=SINCE for e in fast['audit_events']),
        'classification_counts':dict(Counter(r['final_classification'] for r in rows)),
        'actually_evaluated':None,'rejected_EMA':None,'rejected_speed':None,'rejected_multiple':None,
        'not_evaluable_buffer':None,'used_stale':None,'would_satisfy_with_correct_infrastructure':None,
        'rows':rows,'raw_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in raw.iterdir() if p.is_file()}}
    (OUT/'audit.json').write_text(json.dumps(result,indent=2),encoding='utf8')
    report=[DOCUMENT,'\n## Sete pares confirmados\n',
        '| source_candle BRT | entry BRT | entry price | HS BRT | pareamento idêntico |',
        '|---|---|---:|---|---|']
    for r in rows:report.append(f"| {r['source_candle']} | {r['entry']} | {r['entry_price']:.4f} | {r['HS']} | {r['same_entry_exit']} |")
    report += ['\n## Tabela factual consolidada\n',
        '| source_candle | cruzamento | EMA | EMA freshness | speed 5m | buffer ok? | predicados falsos | classificação final |',
        '|---|---|---|---|---|---|---|---|']
    for r in rows:report.append(f"| {r['source_candle']} | NÃO DETERMINÁVEL | NÃO DETERMINÁVEL | NÃO DETERMINÁVEL | NÃO DETERMINÁVEL | NÃO DETERMINÁVEL | NÃO DETERMINÁVEIS | OTHER |")
    report += ['\n## Amostras retidas: somente limites, NÃO cruzamentos exatos\n']
    for r in rows:
        report.append(f"- {r['source_candle']}: {r['first_retained_sample_below_loss_NOT_FIRST_CROSSING']}")
    report += ['\n## Proveniência e reprodução\n',f"Checkpoints: controle {control['updated_at']}; FAST {fast['updated_at']}. Snapshot não simultâneo; pares fechados imutáveis foram comparados. Fontes da VPS: revisão 3bbb4d4, hashes/conteúdo em remote_inventory.json. Comparação com fontes locais: {source_match}.",
        '\n`python tools/fast_drop_hs_factual_audit.py` reconstrói relatório a partir das cópias locais; `--remote-inventory` consulta somente leitura as fontes/telemetria remotas. audit.json conserva valores desconhecidos como null e hashes das evidências. Nenhum código de trading, estado ou .pending alterado; nenhum deploy/restart/commit/push.']
    (OUT/'RELATORIO.md').write_text('\n'.join(report),encoding='utf8')
    print(json.dumps({k:v for k,v in result.items() if k not in ('rows','raw_sha256')},indent=2))


DOCUMENT='''# FAST_DROP nos sete HARD_STOP — auditoria factual

**Resultado: os sete trades existem nos dois braços e fecharam no mesmo HARD_STOP; não existe evidência histórica suficiente para atribuir a cada um o predicado que impediu FAST_DROP.** OTHER significa causa não determinável, não uma causa operacional adicional provada. Não substituí ausência de prova por RULE_REJECTED ou NOT_EVALUABLE_BUFFER.

## Regra efetiva e ordem causal

1. Braço habilitado; input tick aceito temporalmente; posição OPEN.
2. `fast_drop_evaluated=False` e PnL do tick <= -0,50% (tolerância 1e-12).
3. Referência disponível: fechamento 1m cuja boundary é `floor(tick/minuto)+1 minuto−5 minutos`.
4. Se faltar referência: `continue`, sem marcar avaliado. Próximos ticks podem tentar novamente; chegada do candle de referência sozinha não reavalia a posição.
5. Com referência: marca `fast_drop_evaluated=True`, ANTES de verificar stop/EMA/speed. Uma reprovação válida nessa tentativa impede novas tentativas posteriores, mesmo que EMA/speed mudem.
6. Stop normal >= preço teórico entry×0,995 precede FAST_DROP.
7. Velocidade = `(entry×0,995 / reference_price −1)×100/5`, <= -0,10%/min. O denominador é nominal 5, e o preço final da fórmula é teórico, NÃO o tick que cruzou. PnL do cruzamento, por outro lado, usa o tick real.
8. EMA deve estar em {SHO,BEA}. Na função allowed o teste de speed vem antes do pertencimento EMA por short-circuit. MACD NÃO participa. Se speed e EMA falharem, ambos são falsos, mas o teste de speed impede primeiro essa conjunção.
9. Contexto: maior close 5m < timestamp do tick entre histórico de seis snapshots e latest. A implementação verifica elegibilidade temporal, mas NÃO impõe idade máxima: pode usar snapshot STALE. FRESH nesta auditoria exigiria candle 5m mais recente já fechado no instante, não apenas close antigo < tick.

## Por que não se pode reconstruir a decisão factual

- FAST_DROP só gera evento detalhado quando fecha por FAST_DROP. Não existe evento das avaliações reprovadas, referência ausente, marcação avaliado ou retry.
- `fast_drop_evaluated` persiste em posições OPEN do checkpoint, mas NÃO no closed_record nem no log CLOSE. As sete posições já foram removidas do estado aberto.
- `fast_drop_minute_closes` conserva apenas os últimos dez minutos relativamente ao maior boundary recebido. As referências dos sete trades já saíram desse checkpoint. Ausência hoje NÃO prova ausência na avaliação.
- O histórico de contexto FAST conserva seis snapshots e latest; não existe checkpoint completo de cada avaliação passada. Telemetria global de contexto prova snapshots gerados, não qual deles o FAST selecionou em uma avaliação sem timestamp registrado.
- `.pending` é um único input mais recente, não journal histórico de todos os inputs. Não foi apagado/reprocessado.
- `market_points` é amostrado no máximo uma vez por >=60s e retido 13h: pode fornecer ponto abaixo do nível, mas NÃO o primeiro tick que cruzou nem a hora da avaliação com referência válida.
- Trough completo no ledger tem somente o mínimo final e seu timestamp. Telemetria contínua trough_events é emitida pelo registry titular/phantoms, não pelo CircuitBreakerShadow. Não se transfere trajetória de outro braço para este sem prova.
- Logs OPEN/CLOSE não conservam resultados dos predicados; contexto de EXIT não foi usado como substituto. Candles públicos ou indicadores reconstruídos não provariam recebimento, buffer, flags e ordem interna históricos.

## Resumo solicitado — limites factuais

- Pares HARD_STOP confirmados: 7; FAST_DROP registrado no período: 0.
- Realmente avaliados: NÃO DETERMINÁVEL (não significa zero).
- Reprovados por EMA / speed / múltiplos: NÃO DETERMINÁVEL.
- NOT_EVALUABLE_BUFFER: NÃO DETERMINÁVEL; nenhum caso pode ser afirmado só porque referência não está no checkpoint atual.
- Usaram STALE: NÃO DETERMINÁVEL; possibilidade do código não prova ocorrência nos sete.
- Marcados avaliado sem cálculo válido por ausência de referência: o caminho atual NÃO faz isso. Ocorrência por trade/retry permanece sem registro.
- Satisfariam regra completa com infraestrutura correta: NÃO DETERMINÁVEL. Não há first crossing/inputs recebidos necessários para essa contagem.

Todos recebem exatamente uma categoria, OTHER, pela insuficiência de evidência. Não é possível apontar primeiro predicado falso, preço inicial/final de janela, idade de contexto ou instante do primeiro cruzamento de maneira factual usando estas fontes. Nenhuma alteração ou melhoria de regra é proposta.
'''


if __name__=='__main__':main()
