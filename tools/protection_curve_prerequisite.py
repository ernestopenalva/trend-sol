"""Algebra and persisted-state checks only; never runs a trade engine."""
import hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.feed_trail_revalidation import quantile
from tools.be_off_cb_exit_context_study import brt
ROOT=Path(__file__).resolve().parents[1]
INPUT=ROOT/'data/analysis/feed_trail_revalidation_20261009'
OUT=ROOT/'data/analysis/protection_curve_20261009'
STEPS=((5,1.5),(8,3),(12,6))

def protection(peak,hs,floor,left=False):
    values=[-hs]
    eligible=lambda trigger:peak>trigger+1e-10 if left else peak>=trigger-1e-10
    for trigger,lock in STEPS:
        effective=max(lock,floor)
        if eligible(effective+trigger-lock):values.append(effective)
    if eligible(10):values.append(peak-5)
    return max(values)

def maximum_giveback(hs,floor,post_first=False):
    thresholds=sorted({10,*[max(lock,floor)+trigger-lock for trigger,lock in STEPS]})
    first=thresholds[0];candidates=[]
    for peak in thresholds:
        if not post_first or peak>first+1e-10:
            candidates.append((peak-protection(peak,hs,floor,True),peak,'LEFT_LIMIT'))
        if not post_first or peak>=first:
            candidates.append((peak-protection(peak,hs,floor),peak,'AT'))
    # After every PL threshold, trailing yields at most a five-ATR giveback.
    candidates.append((5,max(thresholds),'AT'))
    value,peak,side=max(candidates,key=lambda x:(x[0],-x[1]))
    return {'giveback_atr':value,'peak_atr':peak,'side':side,'first_protection_peak_atr':first}

def dist(xs):return {k:quantile(xs,q) for k,q in [('min',0),('median',.5),('p90',.9),('max',1)]}

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    cuts=json.loads((ROOT/'data/analysis/atr_units_20261009/summary.json').read_text())['quartile_cuts_atr_pct']
    summaries={};records={};differences={};hashes={}
    for path in ['HIGH_FIRST','LOW_FIRST']:
        p=INPUT/f'{path}_ACT10_GAP5.json';hashes[path]=hashlib.sha256(p.read_bytes()).hexdigest()
        data=json.loads(p.read_text());assert data['prior_full_parity'];rows=[];bad=[];diff=[];n=0
        for d in data['details']:
            entry=d['entry_price'];atr=d['entry_atr'];pct=100*atr/entry;hs=1.5/pct;floor=.25/pct
            row={'source_candle':d['source_candle'],'quartile':1+sum(pct>c for c in cuts),
                 'atr_pct':pct,'hs_atr':hs,'economic_floor_atr':floor,
                 'nominal':maximum_giveback(hs,0),'adjusted':maximum_giveback(hs,floor),
                 'nominal_post_first':maximum_giveback(hs,0,True),
                 'adjusted_post_first':maximum_giveback(hs,floor,True)}
            observed=[]
            for e in d['floor_events']:
                x=(e['peak']-entry)/atr;actual=(e['effective_stop']-entry)/atr
                expected=protection(x,hs,floor);nominal=protection(x,hs,0);n+=1
                witness={'source_candle':d['source_candle'],'quartile':row['quartile'],'at_ms':e['at_ms'],
                    'peak_atr':x,'actual_protection_atr':actual,'adjusted_protection_atr':expected,
                    'nominal_protection_atr':nominal,'actual_owner':e['owner'],
                    'economic_floor_atr':floor,'atr_pct':pct}
                if abs(entry+expected*atr-e['effective_stop'])>1e-8:bad.append(witness)
                if abs(entry+nominal*atr-e['effective_stop'])>1e-8:diff.append(witness)
                observed.append({'giveback_atr':x-actual,'peak_atr':x,'at_ms':e['at_ms']})
            row['observed_max']=max(observed,key=lambda r:r['giveback_atr']) if observed else None
            rows.append(row)
        quartiles={}
        for q in range(1,5):
            xs=[r for r in rows if r['quartile']==q];summary={'N':len(xs)}
            for kind in ['nominal','adjusted','nominal_post_first','adjusted_post_first','observed_max']:
                chosen=max(xs,key=lambda r:r[kind]['giveback_atr'])
                summary[kind]={'distribution':dist([r[kind]['giveback_atr'] for r in xs]),
                               'maximum_source':chosen['source_candle'],'maximum_case':chosen[kind]}
            summary['adjusted_first_protection_peak']=dist([r['adjusted']['first_protection_peak_atr'] for r in xs])
            quartiles[q]=summary
        summaries[path]={'states':n,'unexpected_adjusted_mismatches':len(bad),
                         'raw_nominal_differences':len(diff),'affected_sources':len({r['source_candle'] for r in diff}),
                         'quartiles':quartiles,'unexpected_exceptions':bad}
        records[path]=rows;differences[path]=diff
    for name,data in [('summary',summaries),('trade_curves',records),('nominal_differences',differences)]:
        (OUT/f'{name}.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
    fmt=lambda x:f'{x:.6f}' if isinstance(x,float) else str(x)
    table=lambda hs,rs:'\n'.join(['| '+' | '.join(hs)+' |','|'+'|'.join(['---']*len(hs))+'|']+
        ['| '+' | '.join(fmt(v) for v in r)+' |' for r in rs])
    lines=['# Curva de proteção — verificação prévia e parada',
        'ACT10/GAP5,01/06–02/10/2026 22:28 BRT. Somente álgebra e snapshots existentes; nenhum replay/engine executado. Quartis fixos do estudo anterior. Etapas1–4 não executadas, seguindo a leitura literal do guardrail de divergência da curva nominal.',
        '## Curva nominal ignorando piso econômico',
        'x=pico em ATR, h=distância HS em ATR. P(x): −h para0≤x<5;1,5 para5≤x<8;3 para8≤x<10;x−5 parax≥10. Em12ATR, PL3 arma em6ATR mas o TRAIL já está em7ATR. BE_OFF/no-progress desligado; review stop não domina HS. Stops têm ratchet, portanto não podem diminuir em preço. Em empate, o engine retém owner atual.',
        'Devolução nominal: x−P(x). Após primeira proteção: supremo7ATR quandox→10−. Antes disso, incluir HS muda o máximo global: supremo max(h+5,7). Na amostra h mínimo2,071966ATR, logo o máximo global nominal de cada trade é h+5, imediatamente antes de5ATR. Não confundir esse limite geométrico com perda efetivamente realizada.',
        '## Curva incluindo piso econômico',
        'F=0,25%/ATR% entrada. Locks efetivos L1=max(1,5,F), L2=max(3,F), L3=max(6,F). Triggers reais T1=L1+3,5; T2=L2+5; T3=L3+6. P(x)=max(−h,L1 sex≥T1,L2 sex≥T2,L3 sex≥T3,x−5 sex≥10). Não basta elevar o piso e manter triggers5/8/12: isso NÃO corresponde ao engine.',
        'A curva ajustada reproduz todos os snapshots a1e-8 em preço. Porém difere da curva ATR nominal sem piso, tanto em valores quanto em triggers. Não é bug novo: é a regra econômica já vigente. Listagem completa em nominal_differences.json.',
        table(['path','snapshots','desvios inexplicados vsajustada','diferenças vsnominal','sources afetados'],[[p,r['states'],r['unexpected_adjusted_mismatches'],r['raw_nominal_differences'],r['affected_sources']] for p,r in summaries.items()]),
        '## Devolução máxima por quartil',
        'Cada trade tem sua curva em trade_curves.json. A tabela mostra distribuições do SUPREMO geométrico (mediana/p90/máximo), e o pico onde o caso máximo do quartil se aproxima desse supremo. LEFT_LIMIT significa imediatamente antes do salto; não há máximo contínuo atingido exatamente no trigger. Coluna observada usa apenas snapshots históricos, não projeta pico futuro.',
        table(['path','Q','N','curva','mediana devolução ATR','p90','máximo','pico do caso máximo ATR','tipo','source máximo'],
            [[p,q,r['N'],kind,c['distribution']['median'],c['distribution']['p90'],c['distribution']['max'],
              c['maximum_case']['peak_atr'],c['maximum_case'].get('side','OBSERVED'),brt(c['maximum_source'])]
             for p,pr in summaries.items() for q,r in pr['quartiles'].items()
             for kind,c in r.items() if kind in ['nominal','adjusted','nominal_post_first','adjusted_post_first','observed_max']]),
        'Nos segmentos pós-primeira-proteção, máximo ajustado é7ATR seF≤3;10−F se3<F<5;5ATR seF≥5. Isso é álgebra dos parâmetros existentes, não varredura. O máximo global antes da primeira proteção é min(T1,10)+h nesta amostra: o primeiro suporte pode ser PL1 atrasado ou TRAIL em10ATR.',
        '## Onde a nominal diverge',
        'QuandoF>1,5, PL1 deixa de ter piso1,5 e trigger5; quandoF>3, PL2 deixa de ter piso3 e trigger8; quandoF>6, PL3 deixa de ter piso6 e trigger12. Dois degraus podem ter o mesmo piso econômico, mas triggers distintos. A faixa8–10ATR não é universalmente uma faixa de PL2=3ATR. Trailing armado também pode continuar abaixo do piso PL. Nenhuma arquitetura, família de unidade ou parâmetro é proposto.',
        '## Interrupção',
        'Não há divergência inexplicada entre engine e curva ajustada. A divergência que aciona a parada, sob a leitura literal do pedido, é em relação à nominal ATR sem piso. Se por “nominal” o pedido pretendia a geometria já ajustada ao piso e seus triggers deslocados, a verificação nessa referência passou; a continuação exige esclarecer essa referência antes das etapas seguintes. Não contamos arming redundante, sequências de propriedade, zona8–10 ou saltos nesta rodada.']
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'inputs_sha256':hashes,'scope':'prerequisite only, no replay',
        'quartile_cuts_atr_pct':cuts,'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},indent=2),encoding='utf-8')
    print(json.dumps({p:{k:v for k,v in r.items() if k!='quartiles'} for p,r in summaries.items()}),flush=True)

if __name__=='__main__':main()
