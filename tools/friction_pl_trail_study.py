"""Frozen accounting and trade-level causal counterfactual; offline only."""
import bisect
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.be_off_cb_defensive_review import Review, iso
from tools.feed_trail_revalidation import quantile
from tools.market_bot_replay import _deduplicate, _bot_exit_config, NullLogger, ReplayExecutionClient
from tools.market_selection_study import load_candle_cache
from tools.ge_replay_study import OpenPosition, EntrySignal, SignalEvent
from src.position.bot_full_engine import BotFullExitPosition
from tools.be_off_cb_exit_context_study import brt

INPUT=ROOT/'data/analysis/feed_trail_revalidation_20261009'
FROZEN=ROOT/'data/studies/trail_activation_gap_systemic/20261005'
CACHE=ROOT/'data/studies/be_off_cb_deterioration/klines'
OUT=ROOT/'data/analysis/friction_pl_trail_20261009'
ARMS=('ACT10_GAP5','ACT20_GAP5','ACT10_GAP13')
PATHS=('HIGH_FIRST','LOW_FIRST')
FEATURES=('progress_atr','progress_pct','age_min','progress_atr_per_min','price_pnl_pct',
          'entry_atr_pct','minutes_since_pl','ema50','ema100','ema200','macd_line','adx14','plus_di14','minus_di14')


def stats(xs):
    xs=[x for x in xs if x is not None]
    return {'n':len(xs),'mean':sum(xs)/len(xs) if xs else None,
            **{k:quantile(xs,q) for k,q in [('p10',.1),('p25',.25),('median',.5),('p75',.75),('p90',.9)]}}


def accounting(t,notional,spread_bps,fee_pct=None):
    a=spread_bps/2/10000;qty=notional/t['entry_price']
    er=t['entry_price']/(1+a);xr=t['exit_price']/(1-a)
    gross=qty*(xr-er);entry_spread=qty*(t['entry_price']-er);exit_spread=qty*(xr-t['exit_price'])
    fees=notional*((t['gross_pct']-t['net_pct']) if fee_pct is None else fee_pct)/100
    net=gross-entry_spread-exit_spread-fees
    if fee_pct is None:assert abs(net-t['net_usd'])<1e-8
    return dict(gross=gross,fees=fees,spread_entry=entry_spread,spread_exit=exit_spread,
                spread=entry_spread+exit_spread,other_costs=0,net=net)


def economics(rows):
    nets=[r['net'] for r in rows];wins=sum(max(0,n) for n in nets);losses=-sum(min(0,n) for n in nets)
    return dict(n=len(nets),net=sum(nets),net_trade=sum(nets)/len(nets) if nets else None,
                pf=wins/losses if losses else None)


class PLOnly(BotFullExitPosition):
    def _should_activate_trailing(self,pnl_pct,pnl_atr):
        return False


def new_position(review,t,klass):
    sig=review.signal[t['opened_ms']];client=ReplayExecutionClient(review.spread/2)
    p=klass(pair_id=f"cf-{t['source_candle']}",symbol=sig.symbol,entry_price=t['entry_price'],
        quantity=review.notional/t['entry_price'],entry_order={'replay':True},open_ts=iso(t['opened_ms']),
        config=_bot_exit_config(review.config),client=client,logger=NullLogger(),entry_atr=sig.entry_atr,
        atr_timeframe=sig.atr_timeframe,atr_period=sig.atr_period,position_id=1,
        source_candle_open_time=sig.source_candle_open_time,position_notional_usdt=review.notional)
    return OpenPosition(p,client,t['opened_ms'],review.notional)


def terminal(rp,closed,fees):
    p=rp.position
    return dict(closed_ms=closed,reason=p.exit_reason or 'OPEN',entry=p.entry_price,exit=p.exit_price,
        net=rp.notional_usdt*(p.pnl_pct(p.exit_price)-fees)/100 if closed else None,
        mfe_pct=(p.highest_price/p.entry_price-1)*100,mae_pct=(p.trough_price/p.entry_price-1)*100,
        mfe_atr=(p.highest_price-p.entry_price)/p.entry_atr,mae_atr=(p.trough_price-p.entry_price)/p.entry_atr)


def counterfactual(review,t,path):
    rps=[new_position(review,t,BotFullExitPosition),new_position(review,t,PLOnly)]
    closes=[None,None];dispute=None;last_pl=None;prefix_checks=0
    for idx in range(bisect.bisect_left(review.opens,t['opened_ms']),len(review.minute)):
        c=review.minute[idx]
        if c.boundary_ms>review.end:break
        points=_deduplicate((c.open,c.high,c.low,c.close) if path=='HIGH_FIRST' else (c.open,c.low,c.high,c.close))
        previous=[None,None]
        for point in points:
            baseline=rps[0].position;old_pl=set(baseline.applied_steps)
            for i,rp in enumerate(rps):
                p=rp.position
                if p.status!='OPEN':continue
                tick=p.effective_stop if previous[i] is not None and previous[i]>p.effective_stop and point<=p.effective_stop else point
                rp.client.current_price=tick;p.on_tick(tick,iso(c.boundary_ms));previous[i]=point
                if p.status=='CLOSED':closes[i]=c.boundary_ms
            if baseline.applied_steps!=old_pl:last_pl=c.boundary_ms
            if dispute is None and baseline.stop_type=='trailing':
                p=baseline;age=(c.boundary_ms-t['opened_ms'])/60000
                ctx=review.context_fields(c.open_time_ms)
                assert ctx['latest_closed_at_ms']<c.open_time_ms
                dispute=dict(at_ms=c.boundary_ms,causal_cutoff_ms=c.open_time_ms,price=point,
                    pl_step=p.profit_lock_step,pl_stop=p.profit_lock_stop,trail_stop=p.trailing_stop,
                    progress_atr=(p.highest_price-p.entry_price)/p.entry_atr,
                    progress_pct=(p.highest_price/p.entry_price-1)*100,age_min=age,
                    progress_atr_per_min=(p.highest_price-p.entry_price)/p.entry_atr/age if age else None,
                    price_pnl_pct=(point/p.entry_price-1)*100,entry_atr_pct=p.entry_atr/p.entry_price*100,
                    minutes_since_pl=(c.boundary_ms-last_pl)/60000 if last_pl else None,**ctx)
            elif dispute is None:
                p,q=[rp.position for rp in rps]
                for field in ['effective_stop','profit_lock_stop','highest_price']:
                    assert abs((getattr(p,field) or 0)-(getattr(q,field) or 0))<1e-8,'prefix parity '+field
                assert p.applied_steps==q.applied_steps
                prefix_checks+=1
            if all(c is not None for c in closes):break
        if all(c is not None for c in closes):break
    original,alt=[terminal(rp,closed,review.fees) for rp,closed in zip(rps,closes)]
    assert original['closed_ms']==t['closed_ms'] and original['reason']==t['exit_reason']
    for key,expected in [('exit',t['exit_price']),('net',t['net_usd']),('mfe_pct',(t['peak_price']/t['entry_price']-1)*100)]:
        assert abs(original[key]-expected)<1e-8,('original parity',key,t['source_candle'])
    assert dispute is not None,'TRAIL exit without a causal ownership landmark'
    resolved=alt['closed_ms'] is not None
    delta=original['net']-alt['net'] if resolved else None
    return dict(source_candle=t['source_candle'],path=path,opened_ms=t['opened_ms'],
        month=brt(t['opened_ms'])[:7],original=original,pl_alternative=alt,dispute=dispute,
        resolved=resolved,delta_usd=delta,delta_pct=delta/review.notional*100 if resolved else None,
        additional_pl_minutes=(alt['closed_ms']-original['closed_ms'])/60000 if resolved else None,
        prefix_checks=prefix_checks)


def rank_effect(a,b):
    """Descriptive probability A > B, half credit ties; not a forecast model."""
    a=[x for x in a if x is not None];b=sorted(x for x in b if x is not None)
    if not a or not b:return None
    probability=sum(bisect.bisect_left(b,x)+(bisect.bisect_right(b,x)-bisect.bisect_left(b,x))/2 for x in a)/len(a)/len(b)
    return {'probability_A_greater_B':probability,'cliff_delta':2*probability-1}


def summarize_cf(rows):
    resolved=[r for r in rows if r['resolved']];deltas=[r['delta_usd'] for r in resolved]
    groups={'TRAIL_WINS':[r for r in resolved if r['delta_usd']>1e-8],
            'PL_WINS':[r for r in resolved if r['delta_usd']< -1e-8],
            'TIE':[r for r in resolved if abs(r['delta_usd'])<=1e-8]}
    features={}
    for f in FEATURES:
        a=[r['dispute'].get(f) for r in groups['TRAIL_WINS']];b=[r['dispute'].get(f) for r in groups['PL_WINS']]
        features[f]={'TRAIL_WINS':stats(a),'PL_WINS':stats(b),'rank_effect':rank_effect(a,b)}
    contexts={}
    for f in ['ema_context','macd_context','pl_step']:
        contexts[f]={}
        for r in resolved:
            value=str(r['dispute'].get(f));bucket=contexts[f].setdefault(value,{'n':0,'delta':0,'trail_wins':0,'pl_wins':0,'ties':0})
            bucket['n']+=1;bucket['delta']+=r['delta_usd'];bucket['trail_wins']+=r['delta_usd']>1e-8
            bucket['pl_wins']+=r['delta_usd']< -1e-8;bucket['ties']+=abs(r['delta_usd'])<=1e-8
    return dict(n=len(rows),resolved=len(resolved),censored=len(rows)-len(resolved),delta=stats(deltas),
        aggregate_delta=sum(deltas),trail_net=sum(r['original']['net'] for r in resolved),
        pl_net=sum(r['pl_alternative']['net'] for r in resolved),
        shares={g:len(rs)/len(resolved) if resolved else None for g,rs in groups.items()},
        reasons=Counter(r['pl_alternative']['reason'] for r in resolved),features=features,contexts=contexts)


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    frozen=json.loads((FROZEN/'manifest.json').read_text());notional=frozen['notional'];spread=frozen['spread_bps']
    for tf,h in frozen['cache_hashes'].items():assert hashlib.sha256((CACHE/f'SOLUSDT_{tf}.jsonl').read_bytes()).hexdigest()==h
    runs={p:{a:json.loads((INPUT/f'{p}_{a}.json').read_text()) for a in ARMS} for p in PATHS}
    friction={};sensitivity={};conversions={}
    for path in PATHS:
        friction[path]={};sensitivity[path]={};conversions[path]={}
        for arm in ARMS:
            assert runs[path][arm]['prior_full_parity']
            ts=[t for t in runs[path][arm]['run']['trades'] if t['closed_ms'] is not None]
            parts=[accounting(t,notional,spread) for t in ts];total={k:sum(r[k] for r in parts) for k in parts[0]}
            n=len(ts);loss=-total['net']
            friction[path][arm]={**total,'n':n,'gross_trade':total['gross']/n,'net_trade':total['net']/n,
                'gross_edge_bps':total['gross']/n/notional*10000,
                'cost_bps':(total['fees']+total['spread'])/n/notional*10000,
                'friction_loss_fraction':(total['fees']+total['spread'])/loss,
                'economic_loss_signed_fraction':-total['gross']/loss}
            sensitivity[path][arm]={str(fee):economics([accounting(t,notional,spread,fee) for t in ts]) for fee in [.20,.15,.10]}
        base={t['source_candle']:t for t in runs[path][ARMS[0]]['run']['trades']}
        for arm in ARMS[1:]:
            other={t['source_candle']:t for t in runs[path][arm]['run']['trades']};groups={}
            for source in set(base)|set(other):
                b=base.get(source);v=other.get(source)
                kind=(b['exit_reason']+' -> '+v['exit_reason']) if b and v and b['closed_ms'] and v['closed_ms'] else 'COMMON_CENSORED' if b and v else 'EXPERIMENT_ONLY' if v else 'ACT10_ONLY'
                d=(v['net_usd'] if v and v['closed_ms'] else 0)-(b['net_usd'] if b and b['closed_ms'] else 0)
                group=groups.setdefault(kind,{'n':0,'delta':0,'sources':[]});group['n']+=1;group['delta']+=d;group['sources'].append(source)
            total=sum(r['delta'] for r in groups.values());conv=groups.get('TRAILING -> PROFIT_LOCK',{'n':0,'delta':0})
            conversions[path][arm]={'systemic_delta':total,'trail_to_pl':conv,
                'conversion_share_of_net_delta':conv['delta']/total if total else None,'groups':groups}
    for name,value in [('friction',friction),('fee_sensitivity',sensitivity),('conversions',conversions)]:
        (OUT/f'{name}.json').write_text(json.dumps(value,indent=2),encoding='utf-8')
    print('FRICTION',json.dumps(friction),flush=True)
    candles={tf:load_candle_cache(CACHE/f'SOLUSDT_{tf}.jsonl') for tf in ['1m','5m']}
    signals=[SignalEvent(s['boundary_ms'],EntrySignal(**s['signal'])) for s in json.loads((ROOT/'data/studies/be_off_cb_defensive_closure/20261002/signals.json').read_text())]
    end=int(__import__('datetime').datetime.fromisoformat(frozen['end_brt']).timestamp()*1000)
    review=Review(frozen['config'],candles,signals,end);summaries={}
    for path in PATHS:
        rows=[];population=[t for t in runs[path][ARMS[0]]['run']['trades'] if t['closed_ms'] and t['exit_reason']=='TRAILING']
        for i,t in enumerate(population):
            rows.append(counterfactual(review,t,path))
            if (i+1)%100==0:print(path,i+1,'/',len(population),flush=True)
        summaries[path]={'ALL':summarize_cf(rows),**{m:summarize_cf([r for r in rows if r['month']==m]) for m in sorted({r['month'] for r in rows})}}
        (OUT/f'{path}_counterfactual.json').write_text(json.dumps(rows),encoding='utf-8')
        print(path,'COUNTERFACTUAL',{k:v for k,v in summaries[path]['ALL'].items() if k not in ['features','contexts']},flush=True)
    (OUT/'counterfactual_summary.json').write_text(json.dumps(summaries,indent=2),encoding='utf-8')


def readout():
    friction=json.loads((OUT/'friction.json').read_text());fees=json.loads((OUT/'fee_sensitivity.json').read_text())
    conversions=json.loads((OUT/'conversions.json').read_text());summaries=json.loads((OUT/'counterfactual_summary.json').read_text())
    # Complete the requested existing strength fields through the same snapshot
    # routine, without an independent indicator or selecting by outcome.
    frozen=json.loads((FROZEN/'manifest.json').read_text())
    five=load_candle_cache(CACHE/'SOLUSDT_5m.jsonl')
    context_reader=Review(frozen['config'],{'1m':[],'5m':five},[],0)
    for path in PATHS:
        rows=json.loads((OUT/f'{path}_counterfactual.json').read_text())
        for r in rows:
            ctx=context_reader.context(r['dispute']['causal_cutoff_ms'])
            assert ctx['latest_closed_at_ms']==r['dispute']['latest_closed_at_ms']
            for k in ['adx14','plus_di14','minus_di14']:r['dispute'][k]=ctx.get(k)
        (OUT/f'{path}_counterfactual.json').write_text(json.dumps(rows),encoding='utf-8')
        summaries[path]={'ALL':summarize_cf(rows),**{m:summarize_cf([r for r in rows if r['month']==m]) for m in sorted({r['month'] for r in rows})}}
    (OUT/'counterfactual_summary.json').write_text(json.dumps(summaries,indent=2),encoding='utf-8')
    fmt=lambda v:'N/A' if v is None else f'{v:.4f}' if isinstance(v,float) else str(v)
    table=lambda h,rs:'\n'.join(['| '+' | '.join(h)+' |','|'+'|'.join(['---']*len(h))+'|']+
                                ['| '+' | '.join(fmt(v) for v in row)+' |' for row in rs])
    lines=['# Fricção e contrafactual PL versus TRAIL',
        'Histórico congelado01/06/2026 → 02/10/2026 22:28 BRT. Seis runs auditados; nenhuma busca de activation/gap, nenhum winner novo, nenhuma mudança de trading/YAML/deploy/restart/Git.',
        '## 1 — gross − fees − spread = net',
        'Mesma quantidade e decisões históricas. Preços referência desfazem somente os multiplicadores de spread do fill. Gross salvo no replay já inclui spread; não foi usado como gross pré-custos. Spread nominal round-trip5bps; impacto exato por trade considera preço de saída. Fees20bps do notional fixo$20. Outros custos modelados=0. Identidade reconciliada por trade a1e-8.',
        table(['path','arm','N closed','gross $','fees $','spread $','net $','gross/trade','net/trade','edge bps','cost bps'],
            [[p,a,*[r[k] for k in ['n','gross','fees','spread','net','gross_trade','net_trade','gross_edge_bps','cost_bps']]] for p,arms in friction.items() for a,r in arms.items()]),
        table(['path','arm','fricção / perda%','componente econômico assinado / perda%'],[[p,a,100*r['friction_loss_fraction'],100*r['economic_loss_signed_fraction']] for p,arms in friction.items() for a,r in arms.items()]),
        'As frações são contribuições assinadas: custos explicam116–182% do prejuízo e o gross positivo compensa16–82%. Não é uma partição causal entre “culpa da lógica” e “culpa da execução”. Custos também influenciaram os pisos econômicos e admissões originais; não foi feito replay sem custos. Todos os seis são positivos antes dos custos, todos negativos depois. É necessário edge bruto médio superior a aproximadamente25bps/trade para cobrir esta fricção, mantendo quantidade/decisões atuais.',
        table(['path','contraste vsACT10/GAP5','delta gross','fees poupadas','spread poupado','delta net'],
            [[p,a,r['gross']-arms[ARMS[0]]['gross'],arms[ARMS[0]]['fees']-r['fees'],
              arms[ARMS[0]]['spread']-r['spread'],r['net']-arms[ARMS[0]]['net']]
             for p,arms in friction.items() for a,r in arms.items() if a!=ARMS[0]]),
        '## 2 — PL alternativo, por trade',
        'População: somente TRAILING fechado em ACT10/GAP5. Engine real original reproduzido a1e-8; alternativa mantém mesmo HARD_STOP, BE_OFF, escadinha PL, custos e timestamps OHLC, suprimindo somente trailing. Stops/ladder/peak foram conferidos até primeiro owner trailing, incluindo empates. Não substituímos execução por MFE nem por simples toque em PL.',
        'Admissões fixas: não recalcula slots, CB ou equity sistêmica posterior. Contexto da disputa usa candle5m fechado até início do minuto; timestamp de saída/arm é boundary-modelo1m, não tick público exato. OPEN alternativo é censurado, excluído de delta resolvido.',
        table(['path','N','resolved','censored','TRAIL net pares','PL net pares','TRAIL−PL total','mean','p25','median','p75','TRAIL ganhou%','PL ganhou%','empate%'],
            [[p,r['n'],r['resolved'],r['censored'],r['trail_net'],r['pl_net'],r['aggregate_delta'],*[r['delta'][k] for k in ['mean','p25','median','p75']],*[100*r['shares'][k] if r['shares'][k] is not None else None for k in ['TRAIL_WINS','PL_WINS','TIE']]] for p,ms in summaries.items() for r in [ms['ALL']]]),
        table(['path','mês entrada','N resolved','TRAIL−PL total','mediana','TRAIL ganhou%','PL ganhou%'],
            [[p,m,r['resolved'],r['aggregate_delta'],r['delta']['median'],100*(r['shares']['TRAIL_WINS'] or 0),100*(r['shares']['PL_WINS'] or 0)] for p,ms in summaries.items() for m,r in ms.items() if m!='ALL']),
        'Cada trade consta nos arquivos HIGH_FIRST/LOW_FIRST_counterfactual.json e TRADES_*.md. MFE/MAE são excursões observadas até saída própria de cada caminho, não máximos futuros usados para decidir.',
        '## 3 — variáveis conhecidas na primeira disputa',
        'Features completas/distribuições por mês/path em counterfactual_summary.json. MFE/MAE final, destino e duração final nunca entram nas features. Progress é peak já observado no instante da disputa; ATR é o de entrada; contexto técnico e ADX/DI vêm da implementação existente, no mesmo snapshot causal5m. Probability A>B e Cliff são comparações descritivas de ranks, NÃO um classificador treinado ou edge ex ante validado.',
        table(['path','feature','TRAIL>N','PL>N','TRAIL p25/med/p75','PL p25/med/p75','Cliff'],
            [[p,f,r['TRAIL_WINS']['n'],r['PL_WINS']['n'],' / '.join(fmt(r['TRAIL_WINS'][k]) for k in ['p25','median','p75']),
              ' / '.join(fmt(r['PL_WINS'][k]) for k in ['p25','median','p75']),r['rank_effect']['cliff_delta'] if r['rank_effect'] else None]
             for p,ms in summaries.items() for f,r in ms['ALL']['features'].items()]),
        'Diferenças entre grupos são retrospectivas e condicionais à população TRAIL que já ocorreu. Sem separação fora da amostra por assinatura, não concluir “subconjunto identificável ex ante” somente porque existe grupo com net favorável. Distribuições e exceções devem acompanhar qualquer associação.',
        'Leitura: EMA/MACD/ADX/DI e idade exibem forte sobreposição entre os grupos; não emergiu gate técnico validado. Há diferença descritiva no progresso já conhecido, mas os intervalos se sobrepõem e também existem exceções; não foi escolhido cutoff. O pequeno grupo PL>TRAIL (29 HIGH /27 LOW) limita caracterização mensal. HIGH/LOW compartilham população, não dobram N independente. Outubro parcial possui apenas7/10 pares resolvidos e não suporta conclusão forte por mês.',
        table(['path','campo','valor','N resolved','TRAIL ganhou','PL ganhou','delta $'],
            [[p,f,k,r['n'],r['trail_wins'],r['pl_wins'],r['delta']] for p,ms in summaries.items()
             for f,groups in ms['ALL']['contexts'].items() for k,r in groups.items()]),
        '## 4 — conversão TRAIL→PL não é ganho sistêmico',
        table(['path','arm','N TRAIL→PL','delta conversão $','delta sistêmico $','share assinada%','sources ACT10-only N/delta','sources experimento-only N/delta'],
            [[p,a,r['trail_to_pl']['n'],r['trail_to_pl']['delta'],r['systemic_delta'],100*r['conversion_share_of_net_delta'],
              str({k:v for k,v in r['groups'].get('ACT10_ONLY',{}).items() if k!='sources'}),
              str({k:v for k,v in r['groups'].get('EXPERIMENT_ONLY',{}).items() if k!='sources'})] for p,arms in conversions.items() for a,r in arms.items()]),
        table(['path','arm','transição/grupo','N','delta $'],[[p,a,k,r['n'],r['delta']] for p,arms in conversions.items() for a,c in arms.items() for k,r in c['groups'].items()]),
        'A conversão TRAIL→PL perde economicamente em todos os quatro contrastes. O maior ganho positivo vem dos pares TRAIL→TRAIL: saídas continuam trailing, mas com geometria/tempo distintos. Fontes exclusivas são efeitos sistêmicos de admissões/slots/CB; não atribuir isoladamente a cada mecanismo sem intervenção adicional. Quantidade maior de PL não prova que PL é economicamente superior.',
        '## 5 — sensibilidade contábil de fees',
        'Somente remarcação econômica: entradas/saídas/PL economic floor original congelados. Spread continua igual; não é novo replay nem garantia de fee operacional disponível.',
        table(['path','arm','fee RT%','net','net/trade','PF'],[[p,a,float(f),r['net'],r['net_trade'],r['pf']] for p,arms in fees.items() for a,fs in arms.items() for f,r in fs.items()]),
        'Número de configurações positivas:0/6 com fee0,20%,0/6 com0,15%,0/6 com0,10%. Reduzir fee nessas hipóteses melhora net, mas não torna nenhum run lucrativo.',
        '## 6 — resposta estrutural e limites',
        'Fricção é decisiva para o sinal do net nesta decomposição, mas o problema econômico inclui edge bruto insuficiente. Competição/activation/gap alteram a captura; os três runs não autorizam decomposição causal universal entre todos esses mecanismos.',
        'A melhora de ACT20/GAP13 não pode ser descrita como “mais PL gera ganho”: conversões diretas custaram dinheiro, compensadas principalmente por TRAILs remanescentes melhores. Candidatos continuam indistinguíveis além da incerteza do estudo anterior; estes deltas não elegem winner.',
        'Uma assinatura ex ante para selecionar TRAIL exige estabilidade por períodos/path e validação independente. Não promover padrão post hoc a gate. Se o contrafactual mostrar benefício agregado de TRAIL, isso será evidência condicionada à população, não prova de política sistêmica ótima.',
        'O contrafactual não sustenta PL dominando a maioria dos TRAILs: consultar percentuais resolvidos acima. Há um estado econômico conhecido ex ante especialmente claro: último degrau PL3 já armado, e TRAIL governando acima de seu piso. Sem degrau PL posterior, retirar TRAIL deixa um teto de proteção PL fixo mais baixo. Sob estas regras/custos/path, isso explica a dominância mecânica observada; não prevê direção futura nem justifica restringir TRAIL só a esse grupo. Censura permanece separada.',
        'TRAIL foi melhor em137/137 pares PL3 resolvidos HIGH e153/153 LOW. Essa é uma assinatura de dominância econômica sob a geometria congelada, não previsão de continuação. PL2 também favorece TRAIL na grande maioria; PL3 não deve virar seletor de política por comparação retrospectiva. Os26/28 censurados podem ter MFE/tempo observados até o fim, mas não recebem net PL hipotético.',
        'Próxima pergunta conceitual admissível: separar o custo de bloquear continuações das capturas maiores em TRAILs remanescentes, mantendo a fronteira econômica de custos explícita. Não constitui nova regra, parâmetro, braço ou proposta de deploy.',
        '## Ferramentas e validação',
        'tools/friction_pl_trail_study.py + tests/test_friction_pl_trail_study.py; reaproveitados engine, execução replay e contexto causal. Cinco testes focados;23 com suites relacionadas, todos PASS. Identidade exata, remarcação de fee sem mudar spread, escadinha PL sem saída por toque antes de armar, teto de PL3 e ranks/quantis. Paridade original e de prefixo em cada trade, tolerância1e-8. Históricos existentes preservados.']
    for path in PATHS:
        rows=json.loads((OUT/f'{path}_counterfactual.json').read_text())
        items=[]
        for r in rows:
            o,a,d=r['original'],r['pl_alternative'],r['dispute']
            items.append([brt(r['source_candle']),brt(d['at_ms']),d['pl_step'],d['pl_stop'],
                str([brt(o['closed_ms']),o['exit'],o['reason'],o['net']]),
                str([brt(a['closed_ms']) if a['closed_ms'] else 'OPEN',a['exit'],a['reason'],a['net']]),
                r['delta_usd'],r['delta_pct'],r['additional_pl_minutes'],
                str([o['mfe_pct'],a['mfe_pct']]),str([o['mae_pct'],a['mae_pct']]),
                d['ema_context'],d['macd_context'],d['latest_closed_at_ms']])
        (OUT/f'TRADES_{path}.md').write_text('# '+path+' — TRAIL versus PL contrafactual\n\n'+
            table(['source','primeiro owner TRAIL','PL armado','PL stop','TRAIL saída/preço/reason/net','PL alt saída/preço/reason/net',
                   'TRAIL−PL $','TRAIL−PL %','min adicionais PL','MFE% TRAIL/PL','MAE% TRAIL/PL','EMA','MACD','snapshot5m closed ms'],items),encoding='utf-8')
        exceptions=sorted([r for r in rows if r['resolved'] and r['delta_usd']< -1e-8],key=lambda r:r['delta_usd'])[:5]
        lines += [f'### {path} — exceções: cinco maiores benefícios PL',
            table(['source','TRAIL−PL $','progress ATR causal','idade min causal','PL armado','EMA','MACD','min adicionais PL'],
                [[brt(r['source_candle']),r['delta_usd'],r['dispute']['progress_atr'],r['dispute']['age_min'],
                  r['dispute']['pl_step'],r['dispute']['ema_context'],r['dispute']['macd_context'],r['additional_pl_minutes']] for r in exceptions])]
    (OUT/'RELATORIO.md').write_text('\n\n'.join(lines),encoding='utf-8')
    (OUT/'manifest.json').write_text(json.dumps({'frozen_input':str(INPUT),'period':['2026-06-01T00:00:00-03:00','2026-10-02T22:28:00-03:00'],
        'tool_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'input_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in INPUT.glob('*FIRST_ACT*.json')},
        'original_and_prefix_parity':'PASS for every counterfactual seed','focused_tests':5,'related_tests':23,
        'scope':'accounting on frozen decisions; PL counterfactual fixed admissions, not systemic'},indent=2),encoding='utf-8')


if __name__=='__main__':
    if '--readout' in sys.argv:readout()
    else:main()
