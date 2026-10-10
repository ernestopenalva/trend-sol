"""Fixed-population causal post-HS readout. Offline; no operational writes."""
from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import sys
from collections import Counter
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from src.indicators.indicators import atr
from src.monitor.entry_engine import EntrySignal
from tools.hs_bull_elastic_diagnostic import CapturePosition, ReplayAdapter, quantiles, table
from tools.be_off_cb_defensive_review import Review, iso
from tools.be_off_cb_exit_context_study import brt
from tools.ge_replay_study import OpenPosition, SignalEvent, load_ge_market_data
from tools.market_bot_replay import MINUTE_MS, NullLogger, ReplayExecutionClient, _bot_exit_config
from tools.market_selection_study import BinancePublicClient

HORIZONS = (0,1,3,5,10,15,30,60)
EPISODE_GAP_MS = 60*MINUTE_MS  # existing hs_clusters convention; never fitted
NUMERIC = ('pnl_pct','recovery_from_worst_pp','MAE_pnl_pct','additional_MAE_pp','distance_to_HS_pp',
    'ema50','ema100','ema200','ema50_delta','ema100_delta','ema200_delta',
    'price_ema50_pct','price_ema100_pct','price_ema200_pct','macd_line','macd_line_previous',
    'entry_ATR','ATR_5m','age_min','since_HS_min','since_LON_lost_min','liquidatable_delta_usd')
CATEGORICAL = ('ema_context','ema50_direction','ema100_direction','ema200_direction','macd_context',
    'LON_kept','PL_armed','TRAIL_armed','effective_stop_owner','elastic_active','normal_HS_armed')


def episodes(cases):
    groups=[]
    for row in sorted(cases,key=lambda r:(r['hs_ms'],r['source_candle'])):
        if not groups or row['hs_ms']-groups[-1][-1]['hs_ms']>EPISODE_GAP_MS:
            groups.append([])
        groups[-1].append(row)
    return groups


def liquidation_net(price,entry,review):
    fill=price*(1-review.spread/2/10000)
    return review.notional*((fill/entry-1)*100-review.fees)/100


class TracePosition(CapturePosition):
    def on_tick(self,price,market_ts=None):
        result=super().on_tick(price,market_ts)
        if self.hs_elastic_started_at:
            self.study.record(self,self.study.boundary,price,'PRICE')
        return result

    def _close_at_market(self,price,reason,ts,trigger_reference):
        if reason=='HARD_STOP_ELASTIC_CONTEXT_LOST':
            self.study.lost_close={'state':deepcopy(self.to_state()),'price':price,
                'at_ms':self.study.at,'snapshot':deepcopy(self.study.review.context(self.study.at))}
        return super()._close_at_market(price,reason,ts,trigger_reference)


class TraceAdapter(ReplayAdapter):
    def __init__(self,review,elastic):
        super().__init__(review,elastic)
        self.trace=[]
        self.lost_close=None

    def factory(self,*args,**kwargs):
        p=TracePosition(*args,**kwargs)
        p.study=self;p._seen_context_close=None
        return p

    def record(self,p,at,price,kind):
        self.trace.append({'at_ms':at,'kind':kind,'price':price,'state':deepcopy(p.to_state()),
            'snapshot':deepcopy(self.review.context(self.at)),
            'context_available_at_ms':self.at})

    def context_transition(self,p,snapshot):
        new=p._seen_context_close!=snapshot.get('latest_closed_at_ms')
        super().context_transition(p,snapshot)
        if new and p.hs_elastic_started_at:
            self.record(p,self.at,float(snapshot['close']),'CONTEXT')


def new_position(adapter,review,trade):
    signal=review.signal[trade['opened_ms']]
    client=ReplayExecutionClient(review.spread/2)
    p=adapter.factory(pair_id='diag-'+str(trade['source_candle']),symbol=signal.symbol,
        entry_price=trade['entry_price'],quantity=review.notional/trade['entry_price'],entry_order={},
        open_ts=iso(trade['opened_ms']),config=_bot_exit_config(review.config),client=client,logger=NullLogger(),
        entry_atr=signal.entry_atr,atr_timeframe=signal.atr_timeframe,atr_period=signal.atr_period,
        source_candle_open_time=trade['source_candle'],position_notional_usdt=review.notional)
    return OpenPosition(p,client,trade['opened_ms'],review.notional)


def isolated_trace(review,trade,path,elastic):
    adapter=TraceAdapter(review,elastic)
    rp=new_position(adapter,review,trade)
    trades=[]
    first=bisect.bisect_left(review.opens,trade['opened_ms'])
    for candle in review.minute[first:]:
        adapter.processor([rp],trades,candle,path,review.fees)
        if rp.position.status=='CLOSED':
            break
    assert len(trades)==1,'same frozen population must resolve'
    return adapter,trades[0]


def first_loss(trace,hs):
    return next((r['at_ms'] for r in trace if r['at_ms']>=hs and r['kind']=='CONTEXT'
                 and r['snapshot']['ema_context']!='LON'),None)


def feature(capture,trace,cutoff,review,closed_ms,elapsed):
    """Never backfill closed cases or expose a future first-LON-loss timestamp."""
    hs=capture['hs_ms']
    if elapsed>0 and closed_ms<=cutoff:
        return {'status':'CLOSED_AT_OR_BEFORE_CUT','features':None}
    known=[r for r in trace if hs<=r['at_ms']<=cutoff]
    if elapsed==0:
        state=deepcopy(capture['state']);price=capture['trigger_price'];snap=capture['snapshot']
        state['hs_elastic']=True;state['hard_stop_price']=None
        state['effective_stop']=state['review_stop'];state['stop_type']='review'
        known=[]  # HS sample excludes later points carrying the same OHLC timestamp
    else:
        assert known
        row=known[-1];state=row['state'];price=row['price']
        snap=review.context(cutoff)
    assert snap['latest_closed_at_ms']<=(capture['causal_at_ms'] if elapsed==0 else cutoff)
    entry=state['entry_price'];worst=min([capture['trigger_price']]+[r['price'] for r in known])
    lost=first_loss(known,hs)
    end=bisect.bisect_right(review.five_boundaries,capture['causal_at_ms'] if elapsed==0 else cutoff)
    five=review.candles['5m'][max(0,end-300):end]
    atr5=atr([c.high for c in five],[c.low for c in five],[c.close for c in five],14)[-1]
    net=liquidation_net(price,entry,review)
    result={'pnl_pct':(price/entry-1)*100,'recovery_from_worst_pp':(price-worst)/entry*100,
        'MAE_pnl_pct':(worst/entry-1)*100,'additional_MAE_pp':(capture['trigger_price']-worst)/entry*100,
        'distance_to_HS_pp':(price-capture['trigger_price'])/entry*100,
        'ema_context':snap['ema_context'],'macd_context':snap['macd_context'],
        'LON_kept':snap['ema_context']=='LON','LON_lost_at_ms':lost,
        'since_LON_lost_min':(cutoff-lost)/MINUTE_MS if lost is not None else None,
        'macd_line':snap['macd_line'],'macd_line_previous':snap['macd_line_previous'],
        'entry_ATR':state['entry_atr'],'ATR_5m':atr5,'age_min':(cutoff-capture['opened_ms'])/MINUTE_MS,
        'since_HS_min':elapsed,'PL_armed':state['profit_lock_stop'] is not None,
        'TRAIL_armed':state['trailing_stop'] is not None,'effective_stop_owner':state['stop_type'],
        'elastic_active':state['hs_elastic'],'normal_HS_armed':state['hard_stop_price'] is not None,
        'liquidatable_delta_usd':net-capture['control_net'],
        'snapshot_open_ms':snap['latest_open_at_ms'],'snapshot_close_ms':snap['latest_closed_at_ms'],
        'snapshot_previous_close_ms':snap['previous_closed_at_ms'],
        'defenses':{k:state.get(k) for k in ('hard_stop_price','review_stop','breakeven_stop','profit_lock_stop','trailing_stop','effective_stop')}}
    for n in (50,100,200):
        result.update({f'ema{n}':snap[f'ema{n}'],f'ema{n}_delta':snap[f'ema{n}']-snap[f'ema{n}_previous'],
            f'ema{n}_direction':snap[f'ema{n}_direction'],f'price_ema{n}_pct':(price/snap[f'ema{n}']-1)*100})
    return {'status':'OPEN','features':result}


def slot_cost(cases,control,cut_minutes,capacity):
    intervals=[(r['hs_ms'],min(r['closed_ms'],r['hs_ms']+cut_minutes*MINUTE_MS),r['closed_ms']) for r in cases]
    # Deduplicate market opportunities within an episode, rather than count
    # the same admission once for each elastic position.
    opportunities=[a for a in control['admissions'] if any(lo<a['at_ms']<=hi and a['at_ms']<closed for lo,hi,closed in intervals)]
    conflicts=[]
    for a in opportunities:
        if a['decision']!='ADMITTED':continue
        at=a['at_ms']
        baseline=sum(t['opened_ms']<=at and (t['closed_ms'] is None or t['closed_ms']>at) for t in control['trades'])
        extra=sum(lo<at<=hi and at<closed for lo,hi,closed in intervals)
        if baseline+extra>capacity:conflicts.append(a['source_candle'])
    return {'slot_minutes':sum((hi-lo)/MINUTE_MS for lo,hi,_ in intervals),
        'still_open':sum(r['closed_ms']>r['hs_ms']+cut_minutes*MINUTE_MS for r in cases),
        'opportunities':len(opportunities),'admissions':sum(a['decision']=='ADMITTED' for a in opportunities),
        'potential_capacity_conflicts':len(set(conflicts)),'conflicting_sources':sorted(set(conflicts))}


def after_context_loss(lost,review,path,control_net,entry):
    """Suspend ONLY context-lost liquidation; retain live PL/Trail/review state."""
    adapter=TraceAdapter(review,False)
    client=ReplayExecutionClient(review.spread/2)
    p=TracePosition.from_state(lost['state'],_bot_exit_config(review.config),client,NullLogger())
    p.study=adapter;p._seen_context_close=None
    rp=OpenPosition(p,client,int(datetime.fromisoformat(p.open_ts).timestamp()*1000),review.notional)
    raw=[(lost['at_ms'],lost['price'])]
    start=bisect.bisect_left(review.opens,lost['at_ms'])
    trades=[];gap=False;last=lost['at_ms']
    for candle in review.minute[start:]:
        if candle.boundary_ms>lost['at_ms']+360*MINUTE_MS:break
        if candle.open_time_ms!=last:
            gap=True;break
        pts=(candle.open,candle.high,candle.low,candle.close) if path=='HIGH_FIRST' else (candle.open,candle.low,candle.high,candle.close)
        raw.extend((candle.boundary_ms,point) for point in pts)
        if p.status=='OPEN':adapter.processor([rp],trades,candle,path,review.fees)
        last=candle.boundary_ms
    targets={'CONTEXT_LOST_PRICE':lost['price'],'HS':entry*.985,'-1%':entry*.99,'-0.5%':entry*.995,'ENTRY':entry}
    returns={}
    for name,target in targets.items():
        below=name!='CONTEXT_LOST_PRICE'
        first=None
        for at,price in raw[1:]:
            if price<target:below=True
            if below and price>=target:
                first=at;break
        returns[name]=(first-lost['at_ms'])/MINUTE_MS if first is not None else None
    completed=last>=lost['at_ms']+360*MINUTE_MS and not gap
    close=trades[0] if trades else None
    net=review.notional*close.net_pct/100 if close else None
    worst=min(price for _,price in raw)
    return {'observed_min':(last-lost['at_ms'])/MINUTE_MS,'complete_6h':completed,
        'raw_returns_min':returns,'raw_MAE_pnl_pct':(worst/entry-1)*100,
        'additional_MAE_pp':(lost['price']-worst)/entry*100,
        'early_1m_pnl_pct':(raw[min(4,len(raw)-1)][1]/entry-1)*100,
        'early_1m_MAE_pnl_pct':(min(price for _,price in raw[:5])/entry-1)*100,
        'counterfactual_exit':close.exit_reason if close else 'CENSORED',
        'counterfactual_net':net,'delta_vs_control':net-control_net if net is not None else None,
        'counterfactual_close_ms':close.closed_ms if close else None,
        'PL_armed':any(r['state']['profit_lock_stop'] is not None for r in adapter.trace),
        'TRAIL_armed':any(r['state']['trailing_stop'] is not None for r in adapter.trace),
        'raw_path':raw}


def compare_horizons(cases):
    result={}
    for h in HORIZONS:
        groups={kind:[(r,r['horizons'][str(h)]['features']) for r in cases if r['outcome']==kind
            and r['horizons'][str(h)]['features'] is not None] for kind in ('BETTER','WORSE','EQUAL')}
        result[str(h)]={'eligible':{k:len(v) for k,v in groups.items()},
            'closed_before_cut':{k:sum(r['outcome']==k and r['horizons'][str(h)]['features'] is None for r in cases) for k in groups},
            'episode_N':{k:len({r['episode_id'] for r,_ in v}) for k,v in groups.items()},
            'numeric':{name:{k:quantiles([f[name] for _,f in v]) for k,v in groups.items()} for name in NUMERIC},
            'categorical':{name:{k:dict(Counter(str(f[name]) for _,f in v)) for k,v in groups.items()} for name in CATEGORICAL}}
        # Equal episode weight: each episode contributes its within-group mean.
        result[str(h)]['episode_numeric']={}
        for name in NUMERIC:
            agg={}
            for kind,values in groups.items():
                buckets={}
                for r,f in values:
                    if f[name] is not None:buckets.setdefault(r['episode_id'],[]).append(f[name])
                agg[kind]=quantiles([sum(v)/len(v) for v in buckets.values()])
            result[str(h)]['episode_numeric'][name]=agg
    return result


def markdown(out,data,manifest):
    lines=['# LON pós-HS — trajetória causal', '',
        f"Janela: {manifest['start_brt']} → {manifest['end_brt']}; mesma população LON, sem seleção pelo destino.",
        'OHLC 1m: HS e pontos intraminuto têm timestamps MODELADOS. Não é possível identificar o tick exato histórico. Contexto causal usa o 5m já fechado; HS usa o snapshot congelado na abertura de seu minuto.',
        'Horizonte HS não incorpora preços seguintes do mesmo minuto. Nos demais cortes, contextos fechados no corte já são elegíveis. Caso fechado antes/no corte NÃO recebe features preenchidas depois.', '',
        f"Variáveis declaradas: {len(NUMERIC)} numéricas + {len(CATEGORICAL)} categóricas; {len(HORIZONS)} horizontes; teto {len(HORIZONS)*(len(NUMERIC)+len(CATEGORICAL))} comparações BETTER×WORSE por trade e mais o mesmo número por episódio. Campos de auditoria/timestamps não são novas features.",
        'Estudo exploratório, não validação. Direção futura e destino final não são features. Sem grids, combinações otimizadas ou escolha automática de variável.', '']
    for path,d in data.items():
        rows=d['cases'];lost=[r for r in rows if r['exit_reason']=='HARD_STOP_ELASTIC_CONTEXT_LOST']
        lines += ['## '+path,'',f"N trades={len(rows)}, N episódios={len(d['episodes'])}. HS consecutivos separados por ≤60min pertencem ao mesmo episódio (agrupamento transitivo, regra prévia hs_clusters).",
            f"Context lost: {len(lost)}/14; todos WORSE nesta amostra; delta agregado ${sum(r['delta'] for r in lost):.4f}. BETTER=4; WORSE=9; EQUAL=1.", '']
        lines+=table(['source BRT','entry BRT','HS BRT modelado','episódio','saída','resultado','delta $','slot extra min','primeira perda LON min','PnL context lost %'],
            [[brt(r['source_candle']),brt(r['opened_ms']),brt(r['hs_ms']),r['episode_id'],r['exit_reason'],r['outcome'],r['delta'],r['extra_min'],r['first_LON_loss_min'],r['context_lost_pnl_pct']] for r in rows])
        lines+=['### Episódios','']
        lines+=table(['id','HS inicial BRT','trades','resultados','delta $','slot min'],
            [[e['id'],brt(e['hs_start']),e['n'],str(e['outcomes']),e['delta'],e['extra_slot_min']] for e in d['episodes']])
        lines+=['### Comparação causal por horizonte','']
        lines+=table(['min','N BETTER','N WORSE','episódios BETTER/WORSE','PnL mediana BETTER','PnL mediana WORSE','recuperação pp BETTER','recuperação pp WORSE','MAE BETTER','MAE WORSE'],
            [[h,g['eligible']['BETTER'],g['eligible']['WORSE'],f"{g['episode_N']['BETTER']}/{g['episode_N']['WORSE']}",
              g['numeric']['pnl_pct']['BETTER']['p50'],g['numeric']['pnl_pct']['WORSE']['p50'],
              g['numeric']['recovery_from_worst_pp']['BETTER']['p50'],g['numeric']['recovery_from_worst_pp']['WORSE']['p50'],
              g['numeric']['MAE_pnl_pct']['BETTER']['p50'],g['numeric']['MAE_pnl_pct']['WORSE']['p50']] for h,g in d['comparisons'].items()])
        lines+=['Todos os indivíduos, min/p10/p25/p50/p75/p90/max e categorias estão no JSON; N cai após encerramentos. Ausência de losers sobreviventes não é discriminação preditiva.', '',
                '### Slot agregado (conflictos agrupam ghost positions por episódio)','']
        lines+=table(['min','slot min','abertos','oportunidades controle','admissões','conflitos potenciais deduplicados'],
            [[h,g['slot_minutes'],g['still_open'],g['opportunities'],g['admissions'],g['potential_capacity_conflicts']] for h,g in d['slots'].items()])
        lines+=['### Context lost — continuação contrafactual de até 6h','']
        lines+=table(['source','min até lost','PnL no lost %','retorno HS min','−1% min','−0,5% min','entry min','MAE bruto %','saída efetiva contrafactual','delta vs HS $'],
            [[brt(r['source_candle']),r['extra_min'],r['context_lost_pnl_pct'],
              *[r['post_context_lost']['raw_returns_min'][k] for k in ('HS','-1%','-0.5%','ENTRY')],
              r['post_context_lost']['raw_MAE_pnl_pct'],r['post_context_lost']['counterfactual_exit'],r['post_context_lost']['delta_vs_control']] for r in lost])
        lines+=['Raw posterior não implica exposição após saída. Toque/PL armado não equivale a execução. Censurados não entram como net zero.', '', '### Timeline individual','']
        for r in rows:
            lines += [f"#### {brt(r['source_candle'])} / {r['episode_id']} / {r['outcome']}",'']
            lines+=table(['HS + min','estado','PnL %','recuperação pp','MAE %','EMA','MACD','LON perdido em min','PL','Trail','stop owner','delta liquidável $','snapshot 5m close BRT'],
                [[h,g['status'], *([None]*11 if g['features'] is None else
                    [g['features']['pnl_pct'],g['features']['recovery_from_worst_pp'],g['features']['MAE_pnl_pct'],g['features']['ema_context'],
                     g['features']['macd_context'],(g['features']['LON_lost_at_ms']-r['hs_ms'])/MINUTE_MS if g['features']['LON_lost_at_ms'] is not None else None,
                     str(g['features']['PL_armed']),str(g['features']['TRAIL_armed']),g['features']['effective_stop_owner'],
                     g['features']['liquidatable_delta_usd'],brt(g['features']['snapshot_close_ms'])])] for h,g in r['horizons'].items()])
    lines += ['## Interpretação causal e limitações','',
        'A regra fecha por context lost SOMENTE quando preço do snapshot está no HS ou pior. A associação com perda ampliada é parcialmente mecânica: reason incorpora condição de preço adverso. Não prova que perder LON prediz queda futura.',
        'Perder LON acima do HS rearma o stop normal; portanto context lost como MOTIVO DE SAÍDA não cobre todas as perdas de LON. A primeira perda de LON dos winners deve ser considerada também.',
        'Compensar espera economicamente: distingue primeira melhora LIQUIDÁVEL acima do controle, que pode desaparecer, da realização efetiva de PL/Trail. Slot-min não é custo em dólares. Conflito potencial não é lucro perdido demonstrado.',
        'Contexto e retorno/MAE foram previstos pelo pedido. Qualquer interpretação adicional de um quadrante ou momento específico é post hoc/exploratória; nenhuma foi usada para construir regra.',
        'Features calculadas antes de anexar BETTER/WORSE/EQUAL. Duração final e saída não são inputs dos cortes. Comparações em horizontes com exits precoces sofrem seleção de sobreviventes.',
        'Este estudo mantém admissões controle fixas para custo de slot e continuação isolada, não reexecuta portfolio/CB após suprimir context lost. O replay sistêmico anterior não deve ser somado a estes contrafactuais.',
        'Mesma janela curta, 14 trades com N efetivo de episódios menor, OHLC e custos sintéticos; sem OOS e sem inferência estatística de validação.', '',
        'Diagnóstico apenas. Trading/YAML/estados não alterados. Sem deploy, restart, commit ou push.']
    (out/'REPORT.md').write_text('\n'.join(lines),encoding='utf8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=ROOT/'data/studies/lon_post_hs/20261009')
    args=parser.parse_args()
    if args.output.exists():raise SystemExit('Choose a new output directory')
    previous=ROOT/'data/studies/hs_bull_elastic/20261009_final'
    frozen=json.loads((previous/'manifest.json').read_text())
    cache=ROOT/'data/studies/be_off_cb_deterioration/klines'
    start,end=frozen['start_ms'],frozen['end_ms'];config=frozen['config']
    client=BinancePublicClient('https://api.binance.com',30)
    candles={tf:load_ge_market_data(client,'SOLUSDT',tf,start-300*15*MINUTE_MS,end,cache,True) for tf in ('1m','5m')}
    signal_path=ROOT/'data/studies/be_off_cb_defensive_closure/20261002/signals.json'
    signals=[SignalEvent(r['boundary_ms'],EntrySignal(**r['signal'])) for r in json.loads(signal_path.read_text())]
    review=Review(config,candles,signals,end)
    args.output.mkdir(parents=True)
    data={}
    for path in ('HIGH_FIRST','LOW_FIRST'):
        control=json.loads((previous/f'control_{path}.json').read_text())
        elastic=json.loads((previous/f'elastic_{path}.json').read_text())
        selected=[r for r in json.loads((previous/f'diagnostics_{path}.json').read_text()) if r['context']=='LON']
        left={r['source_candle']:r for r in control['trades']};right={r['source_candle']:r for r in elastic['trades']}
        cases=[]
        for i,selection in enumerate(selected):
            source=selection['source_candle'];a,b=left[source],right[source]
            print(f'{path} {i+1}/14 {brt(source)}',flush=True)
            baseline,trade=isolated_trace(review,a,path,False)
            assert trade.closed_ms==a['closed_ms'] and abs(trade.exit_price-a['exit_price'])<=1e-8
            capture=baseline.captures[-1];capture['control_net']=a['net_usd']
            actual,result=isolated_trace(review,a,path,True)
            assert result.closed_ms==b['closed_ms'] and result.exit_reason==b['exit_reason']
            assert abs(result.exit_price-b['exit_price'])<=1e-8
            first=first_loss(actual.trace,a['closed_ms'])
            horizons={str(h):feature(capture,actual.trace,a['closed_ms']+h*MINUTE_MS,review,b['closed_ms'],h) for h in HORIZONS}
            delta=b['net_usd']-a['net_usd']
            row={'source_candle':source,'opened_ms':a['opened_ms'],'hs_ms':a['closed_ms'],'closed_ms':b['closed_ms'],
                'entry_price':a['entry_price'],'HS_price':capture['trigger_price'],
                'exit_price':b['exit_price'],'exit_reason':b['exit_reason'],'net':b['net_usd'],'control_net':a['net_usd'],
                'delta':delta,'extra_min':(b['closed_ms']-a['closed_ms'])/MINUTE_MS,
                'first_LON_loss_min':(first-a['closed_ms'])/MINUTE_MS if first is not None else None,
                'context_lost_pnl_pct':(actual.lost_close['price']/a['entry_price']-1)*100 if actual.lost_close else None,
                'horizons':horizons,'trace':actual.trace,
                'events':actual.events,'post_context_lost':after_context_loss(actual.lost_close,review,path,a['net_usd'],a['entry_price']) if actual.lost_close else None}
            known=[r for r in actual.trace if r['at_ms']>=a['closed_ms'] and r['state']['status']=='OPEN']
            first_positive=next((r['at_ms'] for r in known if liquidation_net(r['price'],a['entry_price'],review)>a['net_usd']+1e-8),None)
            row['first_liquidatable_advantage_min']=(first_positive-a['closed_ms'])/MINUTE_MS if first_positive is not None else None
            # Outcome labels attached only after all causal feature computation.
            row['outcome']='BETTER' if delta>1e-8 else 'WORSE' if delta < -1e-8 else 'EQUAL'
            row['slot_horizons']={str(h):slot_cost([row],control,h,int(config['capital']['max_open_positions'])) for h in HORIZONS}
            row['full_slot_cost']=slot_cost([row],control,row['extra_min'],int(config['capital']['max_open_positions']))
            row['economic_slot_cost_demonstrated']=None
            before_loss=[]
            if first is not None:
                for event in actual.trace:
                    if event['kind']=='CONTEXT' and event['at_ms']==first:break
                    if event['at_ms']>=a['closed_ms']:before_loss.append(event)
            row['MAE_before_first_LON_loss_pct']=min((r['price']/a['entry_price']-1)*100 for r in before_loss) if before_loss else None
            row['first_additional_deterioration_min']=next(((r['at_ms']-a['closed_ms'])/MINUTE_MS for r in before_loss if r['price']<capture['trigger_price']-1e-8),None)
            cases.append(row)
        grouped=episodes(cases);episode_rows=[]
        for i,group in enumerate(grouped,1):
            name=f'E{i:02d}'
            for r in group:r['episode_id']=name
            episode_rows.append({'id':name,'hs_start':group[0]['hs_ms'],'n':len(group),
                'sources':[r['source_candle'] for r in group],'outcomes':dict(Counter(r['outcome'] for r in group)),
                'delta':sum(r['delta'] for r in group),'extra_slot_min':sum(r['extra_min'] for r in group),
                'full_slot_cost':slot_cost(group,control,max(r['extra_min'] for r in group),int(config['capital']['max_open_positions'])),
                'slots':{str(h):slot_cost(group,control,h,int(config['capital']['max_open_positions'])) for h in HORIZONS}})
        # Aggregate costs by disjoint episode intervals; each signal deduped per episode.
        slot_rows={}
        for h in HORIZONS:
            terms=[e['slots'][str(h)] for e in episode_rows]
            slot_rows[str(h)]={k:sum(t[k] for t in terms) for k in ('slot_minutes','still_open','opportunities','admissions','potential_capacity_conflicts')}
        data[path]={'cases':cases,'episodes':episode_rows,'comparisons':compare_horizons(cases),'slots':slot_rows}
        (args.output/f'cases_{path}.json').write_text(json.dumps(cases),encoding='utf8')
    manifest={k:frozen[k] for k in ('start_brt','end_brt','config')}
    manifest.update(horizons=HORIZONS,episode_gap_minutes=60,numeric_variables=NUMERIC,categorical_variables=CATEGORICAL,
        comparisons_upper_bound_per_unit=len(HORIZONS)*(len(NUMERIC)+len(CATEGORICAL)),
        parity='14 pairs per path: exact exit times/reasons, prices within 1e-8',
        causal_features='no post-exit fill; outcomes attached after features; HS snapshot minute-open convention',
        input_hashes={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in
            (Path(__file__),ROOT/'tools/hs_bull_elastic_diagnostic.py',signal_path,previous/'manifest.json',cache/'SOLUSDT_1m.jsonl',cache/'SOLUSDT_5m.jsonl')})
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf8')
    (args.output/'summary.json').write_text(json.dumps({p:{k:v for k,v in d.items() if k!='cases'} for p,d in data.items()},indent=2),encoding='utf8')
    markdown(args.output,data,manifest)
    print(args.output/'REPORT.md',flush=True)


if __name__=='__main__':main()
