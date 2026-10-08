"""Post-trigger control paths: descriptive recovery/censoring, no new policy."""
import bisect
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.winner_trajectory_study import INPUT,CACHE,SIGNALS
from tools.fast_drop_v2_study import OUT as REPLAY
from tools.be_off_cb_defensive_review import Review,iso
from tools.be_off_cb_defensive_closure import ms
from tools.market_selection_study import load_candle_cache
from tools.market_bot_replay import _deduplicate
from tools.ge_replay_study import SignalEvent
from src.monitor.entry_engine import EntrySignal
from tools.be_off_cb_exit_context_study import brt

OUT=ROOT/'data/studies/fast_drop_post_trigger/20261007'


def distribution(values):
    values=sorted(values)
    def q(f):
        if not values:return None
        at=(len(values)-1)*f;i=int(at);j=min(i+1,len(values)-1)
        return values[i]+(values[j]-values[i])*(at-i)
    return {'N':len(values),'min':min(values) if values else None,'p05':q(.05),'p10':q(.1),
        'p25':q(.25),'p50':q(.5),'p75':q(.75),'p90':q(.9),'p95':q(.95),
        'max':max(values) if values else None,'all_sorted':values}


def recovery_stats(curve,trigger_price):
    """curve starts at trigger; labels have minute resolution and ordered subpoints."""
    worst=min(range(len(curve)),key=lambda i:curve[i]['price'])
    # Do not use an earlier recovery to produce a negative worst->recovery time.
    rec=next((i for i in range(worst+1,len(curve)-1) if curve[i]['price']>=trigger_price-1e-10),None)
    any_rec=next((i for i in range(1,len(curve)-1) if curve[i]['price']>=trigger_price-1e-10),None)
    later_rec=next((i for i in range(1,len(curve)-1) if curve[i]['at_ms']>curve[0]['at_ms'] and curve[i]['price']>=trigger_price-1e-10),None)
    running_min=trigger_price;deepest_returned=None
    for sample in curve[1:-1]:
        running_min=min(running_min,sample['price'])
        if sample['price']>=trigger_price-1e-10:deepest_returned=running_min
    return {'worst_index':worst,'recovery_after_worst_index':rec,'any_recovery_index':any_rec,
        'recovered_after_worst':rec is not None,'recovered_any_before_exit':any_rec is not None,
        'recovered_before_final_worst':any_rec is not None and any_rec<worst,
        'any_recovery_later_minute':later_rec is not None,
        'first_recovery_same_minute':any_rec is not None and curve[any_rec]['at_ms']==curve[0]['at_ms'],
        'censored_for_any_recovery':any_rec is None,
        'deepest_quote_before_observed_return':deepest_returned,
        'censored_for_recovery_after_worst':rec is None,
        'trigger_to_worst_min':(curve[worst]['at_ms']-curve[0]['at_ms'])/60000,
        'worst_to_recovery_min':(curve[rec]['at_ms']-curve[worst]['at_ms'])/60000 if rec is not None else None,
        'followup_after_worst_min':(curve[-1]['at_ms']-curve[worst]['at_ms'])/60000,
        'worst_is_terminal_observation':worst==len(curve)-1}


def reconstruct(control,fast,event,review,path):
    rp=review.new_position(control['opened_ms'],control['entry']);p=rp.position
    entry=p.entry_price;curve=[];found=False;previous=None
    for candle in review.minute[bisect.bisect_left(review.opens,control['opened_ms']):]:
        if candle.boundary_ms>control['closed_ms']:break
        points=_deduplicate((candle.open,candle.high,candle.low,candle.close) if path=='HIGH_FIRST' else (candle.open,candle.low,candle.high,candle.close))
        previous=None
        for index,point in enumerate(points):
            stop=p.effective_stop
            tick=stop if previous is not None and previous>stop and point<=stop else point
            trigger_here=candle.boundary_ms==event['label_boundary_ms'] and index==event['point_index']
            if trigger_here:
                trigger_price=event['target'] if previous is not None and previous>event['target'] and point<=event['target'] else point
                if abs(trigger_price*(1-review.spread/2/10000)-fast['exit'])>1e-8:
                    raise AssertionError('Trigger quote does not match actual FAST fill')
                found=True
                # An interpolated trigger precedes the endpoint; a gap/exact hit
                # is the same physical sample as that endpoint (not a recovery).
                ordinal=index-.5 if abs(tick-trigger_price)>1e-10 else index
                curve.append({'at_ms':candle.boundary_ms,'ordinal':ordinal,'price':trigger_price})
            rp.client.current_price=tick;p.on_tick(tick,iso(candle.boundary_ms))
            if found:
                current={'at_ms':candle.boundary_ms,'ordinal':index,'price':tick}
                if (current['at_ms'],current['ordinal'])>(curve[-1]['at_ms'],curve[-1]['ordinal']):curve.append(current)
            previous=point
            if p.status=='CLOSED':break
        if p.status=='CLOSED':break
    if not found or p.status!='CLOSED' or p.exit_reason!=control['reason'] or abs(p.exit_price-control['exit'])>1e-8:
        raise AssertionError('Paired control reconstruction parity failed')
    if candle.boundary_ms!=control['closed_ms']:raise AssertionError('Control exit time mismatch')
    stats=recovery_stats(curve,curve[0]['price']);worst=curve[stats['worst_index']]
    return {'path':path,'source':control['source'],'source_BRT':brt(control['source']),
        'opened_BRT':brt(control['opened_ms']),'FAST_BRT':brt(event['label_boundary_ms']),
        'FAST_label_ms':event['label_boundary_ms'],'FAST_quote':curve[0]['price'],
        'FAST_quote_PnL_pct':(curve[0]['price']/entry-1)*100,
        'FAST_filled_PnL_pct':(fast['exit']/entry-1)*100,
        'control_exit_reason':control['reason'],'control_exit_BRT':brt(control['closed_ms']),
        'worst_PnL_pct':(worst['price']/entry-1)*100,'worst_BRT':brt(worst['at_ms']),
        'control_final_filled_PnL_pct':control['net_pct']+review.fees,
        'control_final_quote_PnL_pct':(curve[-1]['price']/entry-1)*100,
        'deepest_drawdown_recovered_PnL_pct':(stats['deepest_quote_before_observed_return']/entry-1)*100 if stats['deepest_quote_before_observed_return'] is not None else None,
        **stats,'curve':curve}


def group(rows):
    return {'N':len(rows),'worst_PnL':distribution([r['worst_PnL_pct'] for r in rows]),
        'deepest_drawdown_followed_by_observed_return':distribution([r['deepest_drawdown_recovered_PnL_pct'] for r in rows if r['deepest_drawdown_recovered_PnL_pct'] is not None]),
        'trigger_to_worst':distribution([r['trigger_to_worst_min'] for r in rows]),
        'worst_to_recovery_resolved_only':distribution([r['worst_to_recovery_min'] for r in rows if r['worst_to_recovery_min'] is not None]),
        'censored_followup_after_worst':distribution([r['followup_after_worst_min'] for r in rows if r['censored_for_recovery_after_worst']]),
        'recovered_after_worst':sum(r['recovered_after_worst'] for r in rows),
        'recovered_any':sum(r['recovered_any_before_exit'] for r in rows),
        'recovered_before_final_worst':sum(r['recovered_before_final_worst'] for r in rows),
        'any_recovery_later_minute':sum(r['any_recovery_later_minute'] for r in rows),
        'first_recovery_same_minute':sum(r['first_recovery_same_minute'] for r in rows),
        'censored_for_any_recovery':sum(r['censored_for_any_recovery'] for r in rows),
        'censored':sum(r['censored_for_recovery_after_worst'] for r in rows),
        'terminal_worst':sum(r['worst_is_terminal_observation'] for r in rows)}


def main():
    OUT.mkdir(parents=True,exist_ok=True);frozen=json.loads((INPUT/'manifest.json').read_text())
    digest=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    for tf,expected in frozen['cache_hashes'].items():
        if digest(CACHE/f'SOLUSDT_{tf}.jsonl')!=expected:raise ValueError('Frozen candles changed')
    engine=ROOT/'src/position/bot_full_engine.py'
    if digest(engine)!=frozen['source_hashes']['src/position/bot_full_engine.py']:raise ValueError('Frozen engine changed')
    if digest(SIGNALS)!=frozen['signals_sha256']:raise ValueError('Signals changed')
    candles={tf:load_candle_cache(CACHE/f'SOLUSDT_{tf}.jsonl') for tf in ('1m','5m','15m')}
    signals=[SignalEvent(s['boundary_ms'],EntrySignal(**s['signal'])) for s in json.loads(SIGNALS.read_text())]
    review=Review(frozen['config'],candles,signals,ms(frozen['end_brt']));output={};all_rows={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        control=json.loads((REPLAY/f'{path}_CONTROL_trades.json').read_text())['closed']
        fast=json.loads((REPLAY/f'{path}_ONE_SHOT_trades.json').read_text())['closed']
        by={r['source']:r for r in control};selected={r['source']:r for r in fast if r['reason']=='FAST_DROP' and r['source'] in by}
        events={}
        with (REPLAY/f'{path}_ONE_SHOT_evaluations.jsonl').open() as stream:
            for line in stream:
                if '"fired": true' not in line:continue
                event=json.loads(line)
                if event['source'] in selected:events[event['source']]=event
        if set(events)!=set(selected):raise AssertionError('Missing exact trigger event')
        rows=[reconstruct(by[source],r,events[source],review,path) for source,r in selected.items()]
        all_rows[path]=rows
        output[path]={name:group([r for r in rows if test(r)]) for name,test in {
            'HARD_STOP':lambda r:r['control_exit_reason']=='HARD_STOP',
            'PL_TRAIL':lambda r:r['control_exit_reason'] in ('PROFIT_LOCK','TRAILING'),
            'OTHER':lambda r:r['control_exit_reason'] not in ('HARD_STOP','PROFIT_LOCK','TRAILING')}.items()}
        output[path]['excluded_no_closed_control']=sum(r['reason']=='FAST_DROP' and r['source'] not in by for r in fast)
        (OUT/f'{path}_trades.json').write_text(json.dumps(rows,indent=2),encoding='utf8')
    (OUT/'summary.json').write_text(json.dumps(output,indent=2),encoding='utf8')
    (OUT/'manifest.json').write_text(json.dumps({'start':frozen['start_brt'],'end':frozen['end_brt'],
        'selection':'ONE_SHOT FAST exits paired only by source with known closed control',
        'parity':'all selected control trades exit price/reason/time and FAST trigger fill passed',
        'recovery':'first quote >= trigger strictly after first global post-trigger worst and strictly before control close',
        'censoring':'no recovery by control exit is censored, never inferred impossible',
        'time':'minute-end replay labels; ordinal subpoints preserve same-minute order, not real tick timing',
        'PnL':'gross quote/entry; final filled PnL separately includes exit spread, never fees',
        'source_sha256':{'tool':digest(Path(__file__)),'engine':digest(engine),'signals':digest(SIGNALS)},
        'cache_sha256':frozen['cache_hashes']},indent=2),encoding='utf8')
    render(all_rows,output)
    print(json.dumps({path:{name:{'N':data['N'],'recovered_after_worst':data['recovered_after_worst'],
        'recovered_before_final_worst':data['recovered_before_final_worst'],'censored':data['censored']}
        for name,data in groups.items() if isinstance(data,dict)} for path,groups in output.items()}))


def render(rows,summary):
    lines=[INTRO,'\n## Distribuição do pior PnL posterior (%)\n',
        '| caminho | destino controle | N | min | p05 | p10 | p25 | mediana | p75 | p90 | p95 | max | recuperação após mínimo | censurados | recuperação anterior ao mínimo |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    fmt=lambda x:'N/A' if x is None else f'{x:.4f}'
    for path,s in summary.items():
        for name in ('HARD_STOP','PL_TRAIL','OTHER'):
            g=s[name];d=g['worst_PnL'];q=[fmt(d[k]) for k in ('min','p05','p10','p25','p50','p75','p90','p95','max')]
            lines.append(f"| {path} | {name} | {g['N']} | "+' | '.join(q)+f" | {g['recovered_after_worst']} | {g['censored']} | {g['recovered_before_final_worst']} |")
    for path,rr in rows.items():
        lines.append(f'\n## {path} — distribuição completa por trade\n')
        for name,reasons in (('Futuros HARD_STOP',('HARD_STOP',)),('Futuros PL/TRAIL',('PROFIT_LOCK','TRAILING')),('Outros',())):
            selected=[r for r in rr if r['control_exit_reason'] in reasons]
            if not selected:continue
            lines += [f'\n### {name}\n',
                '| source BRT | destino | FAST BRT | PnL FAST % | pior posterior % | final controle % (fill) | FAST→mínimo min | recuperou após mínimo | mínimo→rec min | recuperou antes do mínimo | censura após mínimo |',
                '|---|---|---|---:|---:|---:|---:|---|---:|---|---|']
            for r in sorted(selected,key=lambda r:r['source']):
                lines.append(f"| {r['source_BRT']} | {r['control_exit_reason']} | {r['FAST_BRT']} | {fmt(r['FAST_quote_PnL_pct'])} | {fmt(r['worst_PnL_pct'])} | {fmt(r['control_final_filled_PnL_pct'])} | {fmt(r['trigger_to_worst_min'])} | {r['recovered_after_worst']} | {fmt(r['worst_to_recovery_min'])} | {r['recovered_before_final_worst']} | {r['censored_for_recovery_after_worst']} |")
    lines += ['\n## Checagem adicional: profundidades que efetivamente precederam recuperação\n',
        'Mínimo GLOBAL é terminal nos HS. Para verificar se os percursos se sobrepõem antes desse término, medimos também a menor cotação anterior a uma volta observada ao nível do FAST. É um rótulo retrospectivo de trajetória, sem threshold pesquisado ou uso como input de decisão.\n',
        '| caminho | destino | N com volta | min | p25 | p50 | p75 | max |',
        '|---|---|---:|---:|---:|---:|---:|---:|']
    for path,s in summary.items():
        for name in ('HARD_STOP','PL_TRAIL'):
            d=s[name]['deepest_drawdown_followed_by_observed_return']
            lines.append(f"| {path} | {name} | {d['N']} | "+' | '.join(fmt(d[k]) for k in ('min','p25','p50','p75','max'))+' |')
    lines.append(END)
    (OUT/'RELATORIO.md').write_text('\n'.join(lines),encoding='utf8')


INTRO='''# Pós-FAST_DROP one-shot — profundidade, recuperação e censura

Janela original 01/06–02/10/2026 22:28 BRT. Apenas source comuns com destino fechado conhecido no controle BE_OFF_CB. HIGH_FIRST e LOW_FIRST separados; não duplicam N. Não há procura de threshold nem regra nova.

PnL no disparo e mínimo posterior são variação bruta do preço de mercado modelado sobre entry (que já inclui spread de compra), sem fees. O mínimo inclui o ponto de disparo e todos os pontos efetivamente visitados até saída do controle, nunca a mínima restante do candle depois de um stop. PnL final é o fill efetivo de saída, com spread de venda: pode ser ~0,025 ponto percentual pior que a última cotação. Ambos ficam separados no JSON. Trigger não é confundido com preço low do endpoint onde o replay detectou o cruzamento.

Recuperação principal: voltar à cotação/PnL do disparo **depois do primeiro mínimo global pós-disparo e antes da saída**. Também registramos qualquer recuperação anterior ao mínimo global, para não esconder trades que recuperaram e mais tarde perderam novamente. Não usamos recuperação anterior para gerar tempo mínimo→recuperação negativo. Ausência de recuperação até saída é censura do tempo até recuperação, não prova de impossibilidade.

Tempos são labels de fechamento 1m, não timestamps intraminuto reais. Ordem dos subpontos OHLC é preservada; eventos no mesmo minuto podem ter tempo 0 sem serem simultâneos. Curve dos trades e índices do trigger/mínimo/recuperação estão nos JSONs. O futuro destino só separa populações retrospectivas; nunca entra numa decisão.
'''

END='''
## Respostas às perguntas

1. **Distribuições:** os 26 HS HIGH e 27 HS LOW têm mínimo pós-disparo exatamente -1,50% de cotação (diferenças ~1e-14 são arredondamento). Nos 8 PL/TRAIL de cada caminho, os mínimos completos são: -0,9805; -0,9653; -0,7432; -0,6087; -0,6043; -0,5914; -0,5385; -0,5251%. Os mesmos oito source sobreviventes aparecem nos dois caminhos; não são 16 observações independentes. Mediana -0,6065%, p10 -0,9698%, p90 -0,5345%. Outros destinos: zero. Um FAST em cada caminho foi excluído por falta de controle fechado pareado.

2. **Faixa sem recuperação observada:** depois do mínimo de -1,50%, não se observa recuperação em nenhum HS, porque TODOS são encerrados e censurados exatamente nesse mínimo; o seguimento depois dele é zero minutos. Não há casos de mínimos entre -0,9805% e -1,50% nesta seleção: é região sem amostra, não região demonstrada sem recuperação. Os 8 sobreviventes recuperam o nível do disparo depois do mínimo, com tempos mínimo→recuperação min/p50/p90/max = 0/1/13,3/14 minutos. Tempos zero são ordem intraminuto modelada, não recuperação instantânea comprovada.

3. **Sobreposição:** as distribuições de mínimo GLOBAL final não se sobrepõem numericamente nesta população. Isso não demonstra separabilidade causal dos destinos: o HS impõe fronteira e encerra a trajetória; o mínimo global usa futuro e a população é selecionada por destino final. Há sobreposição do percurso: 25/26 futuros HS HIGH e 27/27 LOW voltaram ao PnL do disparo ANTES do mínimo terminal. Nos sobreviventes, 4/8 HIGH e 5/8 LOW recuperaram uma vez antes de voltar a um mínimo mais profundo e recuperar novamente.

Na checagem de menor profundidade anterior a uma recuperação efetivamente observada, os futuros HS recuperaram desde perdas de até -1,4974% em ambos os caminhos, antes de acabar posteriormente no stop. A mediana dessas profundidades nos HS é -0,8696%; nos PL/TRAIL é -0,6065%. Há 14/25 HS HIGH e 15/27 HS LOW com profundidade recuperada dentro do intervalo completo dos oito PL/TRAIL (-0,9805% a -0,5251%): sobreposição material dos percursos recuperáveis nesta amostra. Isso não é um threshold de aprovação, nem estimativa de probabilidade de recuperação fora do suporte observado.

4. **Ponto de não retorno:** não fica sustentado quando a censura é explicitada, em nenhum caminho. A ausência de recuperação após -1,50% coincide mecanicamente com a saída do controle; não há observação posterior para julgar o preço. Mesmo ignorando o restante do candle de FAST, 25/26 HS HIGH e 26/27 LOW apresentaram pelo menos uma cotação posterior recuperada em outro minuto, antes da queda terminal. Os 8 PL/TRAIL também têm recuperação em minuto posterior. Assim, não se pode chamar o disparo original de início de uma queda necessariamente irreversível.

Sensibilidade OHLC: primeira recuperação no próprio minuto do FAST ocorreu em 17/26 HS HIGH e 27/27 LOW; nos PL/TRAIL, 5/8 HIGH e 8/8 LOW. Essas primeiras recuperações dependem da ordem modelada. A checagem de recuperação em minutos posteriores acima não transforma os candles em ticks reais, mas evita depender exclusivamente desse restante de candle.

Tempo FAST→mínimo nos HS: HIGH min/p10/p50/p90/max = 1/3,5/71/299,5/459 minutos; LOW = 1/3,6/72/295,8/459. PL/TRAIL, ambos: 0/0/2/22,2/53 minutos. Quantis completos e todas as linhas ficam nos arquivos, sem threshold otimizado ou regra proposta. N=8 na população PL/TRAIL é LOW_N / EXPLORATORY, insuficiente para generalizar qualquer fronteira.

## Limites e interpretação

O mínimo global é informação futura. Classificar por esse mínimo ou pelo destino não fornece um input causal de trading. Em particular, HS no controle encerra a observação ao alcançar sua fronteira: um PL/TRAIL sobrevivente normalmente não pode ter ultrapassado esse HS antes. Separação entre mínimos terminais pode ser imposta pelo próprio stop, não por um fenômeno de mercado "sem retorno".

Todas as trajetórias param na saída do controle. Não observamos/reconstruímos preço depois dela para transformar censura em fracasso. Não se estima probabilidade de nunca recuperar, nem Kaplan–Meier como se saída por HS fosse censura independente: a saída é informativa. População PL/TRAIL pequena exige cautela; HIGH/LOW não são amostras independentes.

Reprodução: `python tools/fast_drop_post_trigger_study.py`. Saídas: manifest.json, summary.json com quantis/valores completos/tempos/censura, HIGH_FIRST_trades.json e LOW_FIRST_trades.json com trajetórias, e este relatório. Ferramenta offline apenas; nenhum runtime/YAML/estado/shadow alterado, nenhum deploy/restart/commit/push.

Verificação: quatro testes focados passaram (censura terminal versus recuperação anterior, recuperação no exit excluída, ordem intraminuto, mínimo empatado e quantis). Todas as 34 reconstruções HIGH e 35 LOW tiveram paridade de preço/motivo/horário de saída do controle e confirmação do preço de disparo contra o fill FAST. Não foi executada a suíte completa do runtime.
'''

if __name__=='__main__':main()
