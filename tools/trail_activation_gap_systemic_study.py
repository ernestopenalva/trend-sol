"""Frozen four-arm activation versus width study; offline, no runtime writes."""
from __future__ import annotations
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools import trail_gap_systemic_study as g
from tools.be_off_cb_exit_context_study import brt

OUT=g.ROOT/'data/studies/trail_activation_gap_systemic/20261005'
ARMS={'ACT10_GAP5':(10,5),'ACT15_GAP5':(15,5),'ACT20_GAP5':(20,5),'ACT10_GAP13':(10,13)}
BASE='ACT10_GAP5'

def arm_config(config,arm):
    cfg=deepcopy(config)
    cfg['risk']['trailing']['activation_atr'],cfg['risk']['trailing']['gap_atr']=ARMS[arm]
    return cfg

class Position(g.GapPosition):
    def on_tick(self,price,ts=None):
        self.tick_index+=1
        old_peaks=len(self.peak_events);old_floors=len(self.floor_events)
        old_activation=self.activation;old_dom=self.first_dominance
        event=super().on_tick(price,ts)
        for row in self.peak_events[old_peaks:]+self.floor_events[old_floors:]:row['tick_index']=self.tick_index
        if old_activation is None and self.activation:self.activation['tick_index']=self.tick_index
        if old_dom is None and self.first_dominance:self.first_dominance['tick_index']=self.tick_index
        return event

class Instrumentation(g.Instrumentation):
    def factory(self,*args,**kwargs):
        p=Position(*args,**kwargs);p.study=self;p.tick_index=0
        p.activation=None;p.first_dominance=None;p.levels={};p.floor_events=[];p.peak_events=[];p.modeled_closed_ms=None
        p.floor_minutes=Counter();self.positions.append(p);return p

def intervals(d,end):
    """Last state at boundary governs the next minute; no intraminute duration claim."""
    states={r['at_ms']:r for r in d['floor_events']}
    stop=d['closed_ms'] if d['closed_ms'] is not None else end
    cursor=d['opened_ms'];owner='HARD_STOP';active=False;counts=Counter()
    dom=d['first_dominance'];dom_at=dom['at_ms'] if dom else None
    for at,row in sorted(states.items())+[(stop,None)]:
        if at>stop:break
        duration=max(0,(at-cursor)/g.MINUTE_MS)
        counts['total_'+owner]+=duration
        counts[('post_activation_' if active else 'pre_activation_')+owner]+=duration
        if dom_at is not None and cursor>=dom_at:counts['post_dominance_'+owner]+=duration
        cursor=at
        if row is not None:owner=row['owner'];active=row['trail_active']
    return dict(counts)

def dominance_groups(run,details,end,window='ALL',notional=20):
    by={d['source_candle']:d for d in details}
    trades=[t for t in run['trades'] if t['closed_ms'] is not None and (window=='ALL' or g.month(t['closed_ms'])==window)]
    def group(ts):
        rows=[]
        for t in ts:
            d=by[t['source_candle']];dom=d['first_dominance'];owners=intervals(d,end)
            new=[p for p in d['peaks'] if dom and p['tick_index']>dom['tick_index'] and p['price']>dom['peak']]
            after_peak=max([dom['peak']]+[p['price'] for p in new]) if dom else None
            rows.append({'source_candle':t['source_candle'],'exit_reason':t['exit_reason'],'exit_owner':d['terminal_owner'],
                'net':t['net_usd'],'giveback_pct':(t['peak_price']-t['exit_price'])/t['entry_price']*100,
                'age_min':(t['closed_ms']-t['opened_ms'])/g.MINUTE_MS,
                'dominance_to_exit_min':(t['closed_ms']-dom['at_ms'])/g.MINUTE_MS if dom else None,
                'dominance_peak_atr':dom['peak_atr'] if dom else None,'new_peak_after_dominance':bool(new),
                'post_dominance_giveback_pct':(after_peak-t['exit_price'])/t['entry_price']*100 if dom else None,
                'post_dominance_price_pnl_usd':notional*(t['exit_price']-dom['price'])/t['entry_price'] if dom else None,
                'post_dominance_net_residual_usd':t['net_usd']-notional*(dom['price']-t['entry_price'])/t['entry_price'] if dom else None,
                'post_dominance_owner_minutes':{k[15:]:v for k,v in owners.items() if k.startswith('post_dominance_')},
                'dominance_episodes':sum(r['owner']=='TRAIL' and (i==0 or d['floor_events'][i-1]['owner']!='TRAIL') for i,r in enumerate(d['floor_events']))})
        return {'n':len(rows),'net_trade':sum(r['net'] for r in rows)/len(rows) if rows else None,
            'reasons':dict(Counter(r['exit_reason'] for r in rows)),'exit_owners':dict(Counter(r['exit_owner'] for r in rows)),
            'new_peak_frequency':sum(r['new_peak_after_dominance'] for r in rows)/len(rows) if rows else None,
            **{k:g.dist([r[k] for r in rows if r[k] is not None]) for k in ('giveback_pct','age_min','dominance_to_exit_min','dominance_peak_atr','post_dominance_giveback_pct','post_dominance_price_pnl_usd','post_dominance_net_residual_usd')},
            'rows':rows}
    return {'all':group(trades),'dominated':group([t for t in trades if by[t['source_candle']]['first_dominance']]),
        'activated_never_dominated':group([t for t in trades if by[t['source_candle']]['activation'] and not by[t['source_candle']]['first_dominance']])}

def finalize(manifest):
    start=g.ms(manifest['start_brt']);end=g.ms(manifest['end_brt']);s={};comparisons={};direct={};audit={}
    for path in g.PATHS:
        data={arm:json.loads((OUT/f'{path}_{arm}.json').read_text()) for arm in ARMS}
        for reference,gap in ((BASE,5),('ACT10_GAP13',13)):
            assert data[reference]['run']==json.loads((g.OUT/f'{path}_GAP_{gap}.json').read_text())['run']
        assert data[BASE]['run']==json.loads((g.INPUT/f'{path}_systemic.json').read_text())['BE_OFF_CB']
        s[path]={};comparisons[path]={};direct[path]={};audit[path]={}
        for arm,x in data.items():
            run=x['run'];ds=x['details'];by={t['source_candle']:t for t in run['trades']}
            for d in ds:
                assert d['closed_ms']==by[d['source_candle']]['closed_ms']
                assert sum(d['floor_minutes'].values())==((d['closed_ms'] or end)-d['opened_ms'])/g.MINUTE_MS
                d['interval_minutes']=intervals(d,end)
                assert abs(sum(v for k,v in d['interval_minutes'].items() if k.startswith('total_'))-sum(d['floor_minutes'].values()))<1e-8
            assert sum(c['slot_minutes'] for c in x['exposure'].values())==sum(sum(d['floor_minutes'].values()) for d in ds)
            s[path][arm]={}
            for mo in g.MONTHS:
                r=g.enrich_stats(run,ds,x['exposure'],mo,start,end,manifest['notional'])
                r['dominance_groups']=dominance_groups(run,ds,end,mo,manifest['notional'])
                r['delta_vs_control']=r['net']-g.metrics(data[BASE]['run'],mo)['net']
                r['other_exits']=r['closed']-r['HARD_STOP']-r['PROFIT_LOCK']-r['TRAILING']
                s[path][arm][mo]=r
            counts=Counter()
            for d in ds:counts.update(d['interval_minutes'])
            s[path][arm]['ALL']['owner_hours']={k:v/60 for k,v in counts.items()}
            if arm!=BASE:
                c=g.paired_analysis(data[BASE]['run'],run,ds,start,end)
                c['top_contribution_rows']=g.realized_contributions(data[BASE]['run'],run)[:20]
                baseline_details={d['source_candle']:d for d in data[BASE]['details']}
                c['additional_pl_hours_common']=sum(sum(v for k,v in d['interval_minutes'].items() if k.startswith('total_PL'))-
                    sum(v for k,v in baseline_details[d['source_candle']]['interval_minutes'].items() if k.startswith('total_PL')) for d in ds if d['source_candle'] in baseline_details)/60
                baseline_trades={t['source_candle']:t for t in data[BASE]['run']['trades']}
                c['new_peak_before_activation_n']=sum(any(p['price']>baseline_trades[d['source_candle']].get('peak_price',float('inf'))+1e-9 and
                    (d['activation'] is None or p['tick_index']<d['activation']['tick_index']) for p in d['peaks']) for d in ds if d['source_candle'] in baseline_trades)
                comparisons[path][arm]=c
                c['additional_duration']=g.dist([r['additional_min'] for r in c['paired_rows'] if r['resolved']])
                c['prolonged_common_n']=sum(r['resolved'] and r['additional_min']>0 for r in c['paired_rows'])
            audit[path][arm]={'entries':len(ds),'exposure_and_model_clock':'PASS',
                'full_prior_parity':'PASS' if arm in (BASE,'ACT10_GAP13') else 'N/A (new arm)'}
            (OUT/f'{path}_{arm}.json').write_text(json.dumps(g.safe_json(x),allow_nan=False),encoding='utf-8')
        for arm in ('ACT15_GAP5','ACT20_GAP5'):
            direct[path][arm]=g.paired_analysis(data['ACT10_GAP13']['run'],data[arm]['run'],data[arm]['details'],start,end)
            a=s[path][arm]['ALL']['dominance_groups']['dominated']['rows'];b=s[path]['ACT10_GAP13']['ALL']['dominance_groups']['dominated']['rows']
            left={r['source_candle']:r for r in b};right={r['source_candle']:r for r in a};common=sorted(set(left)&set(right))
            direct[path][arm]['both_dominated_common']={'n':len(common),'rows':[{'source_candle':source,
                'delta_net':right[source]['net']-left[source]['net'],
                'delta_giveback_pct':right[source]['giveback_pct']-left[source]['giveback_pct'],
                'delta_post_dominance_net_residual_usd':right[source]['post_dominance_net_residual_usd']-left[source]['post_dominance_net_residual_usd'],
                'delta_post_dominance_min':right[source]['dominance_to_exit_min']-left[source]['dominance_to_exit_min']} for source in common],
                'limit':'Different first-dominance times/prices even for common sources: descriptive conditional comparison, not pure causal width effect.'}
    for name,value in [('summary',s),('comparisons',comparisons),('direct_vs_gap13',direct),('audit',audit),('manifest',manifest)]:
        (OUT/f'{name}.json').write_text(json.dumps(g.safe_json(value),indent=2,allow_nan=False),encoding='utf-8')
    report(s,comparisons,manifest)

def report(s,comparisons,m):
    lines=['# Ativação versus largura — replay sistêmico',f"Base congelada: {m['start_brt']} → {m['end_brt']}; ATR14 1m de entrada congelado.",
        'Quatro máquinas independentes. Controle e GAP13 reproduzidos integralmente. Net por mês de fechamento; CB contínuo, DD mensal inicia zero apenas como métrica. OPEN não recebe net hipotético.',
        'Dominância segue stop_type real e ratchet de empate. Durações no relógio OHLC1m, sem resolução intraminuto. Capital é nominal: sizing original fixo de $20, sem adicionar gate de solvência.',
        'Giveback pós-dominância = peak máximo desde primeira dominância menos exit, dividido pelo entry. Não é automaticamente causado pelo TRAIL: consultar owner final e minutos PL/TRAIL nas linhas JSON. PnL de preço pós-dominância é variação desde preço na primeira dominância, na quantidade original. Net residual após dominância = net completo menos ganho bruto acumulado até primeira dominância; aloca TODOS os custos reais ao residual, não representa nova execução. Net/trade é econômico completo.']
    for path in g.PATHS:
        lines += [f'## {path}',g.table(['mês','arm','entries','closed','open fim/cens','net','delta','net/trade','PF','DD','win%','age mean/median','sim mean/max','slot-h','capital mean/max','capacity','HS','PL','TRAIL','other'],[
            [mo,a,r['entries'],r['closed'],str(r['open_at_window_end'])+'/'+str(r['censored_at_data_end']),g.fmt(r['net']),g.fmt(r['delta_vs_control']),g.fmt(r['net_trade']),g.fmt(r['pf']),g.fmt(r['dd']),g.fmt(100*r['win_rate']) if r['win_rate'] is not None else 'N/A',g.fmt(r['mean_age'])+'/'+g.fmt(r['median_age']),g.fmt(r['mean_simultaneous'])+'/'+str(r['max_sim']),g.fmt(r['slot_hours']),g.fmt(r['capital_mean'])+'/'+g.fmt(r['capital_max']),r['blocked_capacity'],r['HARD_STOP'],r['PROFIT_LOCK'],r['TRAILING'],r['other_exits']] for mo in g.MONTHS for a,ws in s[path].items() for r in [ws[mo]]]),
            '### Dominância agregada',g.table(['arm','ativou','dominou','ativou nunca dominou','peak primeira dom med ATR','latência med min','PL total h','PL pré-ativ h','PL pós-ativ h','TRAIL h'],[
                [a,r['activated'],r['first_dominance']['n'],r['activated_never_dominated'],g.fmt(r['first_dominance']['peak_atr']['median']),g.fmt(r['first_dominance']['minutes_from_activation']['median']),*[g.fmt(sum(v for k,v in r['owner_hours'].items() if k.startswith(prefix))) for prefix in ('total_PL','pre_activation_PL','post_activation_PL','total_TRAIL')]] for a,ws in s[path].items() for r in [ws['ALL']]]),
            '### Giveback por dominância (agregado)',g.table(['arm','grupo','N','GB mean/median/p75/p90/max %','net/trade','age mean/median/p90 min','dom→exit med min','peak dom med ATR','novo peak %','price PnL após dom mean $','exits','owner final'],[
                [a,k,x['n'],'/'.join(g.fmt(x['giveback_pct'][v]) for v in ('mean','median','p75','p90','max')),g.fmt(x['net_trade']),'/'.join(g.fmt(x['age_min'][v]) for v in ('mean','median','p90')),g.fmt(x['dominance_to_exit_min']['median']),g.fmt(x['dominance_peak_atr']['median']),g.fmt(100*x['new_peak_frequency']) if x['new_peak_frequency'] is not None else 'N/A',g.fmt(x['post_dominance_price_pnl_usd']['mean']),json.dumps(x['reasons']),json.dumps(x['exit_owners'])] for a,ws in s[path].items() for k,x in ws['ALL']['dominance_groups'].items()])]
        lines += ['### Depois da primeira dominância real',g.table(['arm','N','giveback mean/median/p75/p90/max %','net residual mean $','tempo mean/median/p90 min','novo peak %'],[
            [a,x['n'],'/'.join(g.fmt(x['post_dominance_giveback_pct'][v]) for v in ('mean','median','p75','p90','max')),g.fmt(x['post_dominance_net_residual_usd']['mean']),'/'.join(g.fmt(x['dominance_to_exit_min'][v]) for v in ('mean','median','p90')),g.fmt(100*x['new_peak_frequency']) if x['new_peak_frequency'] is not None else 'N/A'] for a,ws in s[path].items() for x in [ws['ALL']['dominance_groups']['dominated']]]),
            '### Comparação direta com ACT10_GAP13',g.table(['arm','delta net vs GAP13','delta DD','delta slot-h','delta capacity','delta GB mean %','prolongados vs controle N'],[
                [a,g.fmt(r['net']-s[path]['ACT10_GAP13']['ALL']['net']),g.fmt(r['dd']-s[path]['ACT10_GAP13']['ALL']['dd']),g.fmt(r['slot_hours']-s[path]['ACT10_GAP13']['ALL']['slot_hours']),r['blocked_capacity']-s[path]['ACT10_GAP13']['ALL']['blocked_capacity'],g.fmt(r['giveback_pct']['mean']-s[path]['ACT10_GAP13']['ALL']['giveback_pct']['mean']),comparisons[path][a]['prolonged_common_n']] for a,ws in s[path].items() if a in ('ACT15_GAP5','ACT20_GAP5') for r in [ws['ALL']]])]
        for arm,c in comparisons[path].items():
            lines += [f'### {arm} vs controle',json.dumps(c['decomposition']),f"PL adicional pares: {g.fmt(c['additional_pl_hours_common'])}h; novo peak superior ao controle antes de ativar (inclui nunca ativados): {c['new_peak_before_activation_n']}.",
                g.table(['grupo','N','delta total','delta med','novos peaks','exit final','idade adicional med min'],[[k,c[k]['n'],g.fmt(c[k]['delta']),g.fmt(c[k]['delta_dist']['median']),c[k]['new_peak_n'],json.dumps(c[k]['reasons']),g.fmt(c[k]['additional_min']['median'])] for k in ('all_improved','all_worsened')]),
                g.table(['transição','N','delta','idade adicional med','GB adicional med','novo peak/recuperação'],[[k,r['n'],g.fmt(r['delta']),g.fmt(r['additional_min']['median']),g.fmt(r['giveback_additional_pct']['median']),str(r['new_peak_n'])+'/'+str(r['recovery_n'])] for k,r in c['transitions'].items()]),
                g.table(['top','contrib','share delta%','delta sem top','tipos'],[[n,g.fmt(r['contribution']),g.fmt(100*r['share_systemic_delta']) if r['share_systemic_delta'] is not None else 'N/A',g.fmt(r['systemic_delta_excluding_top']),json.dumps(r['kinds'])] for n,r in c['concentration'].items()]),
                g.table(['rank','source BRT','contrib $','tipo'],[[i,brt(r['source_candle']),g.fmt(r['contribution']),r['kind']] for i,r in enumerate(c['top_contribution_rows'],1)]),
                f"Capacidade: {len(c['capacity_blocked'])}; sources admitidos no controle: {c['capacity_control_admitted_n']}; net observado controle {g.fmt(c['capacity_control_observed_net'])}. Não é oportunidade causal recuperável garantida."]
    lines+=['## Limites causais','ACT15/20 vs controle isolam ativação no GAP5. Não há fatorial ACT15/20_GAP13: não permite porcentagens causais nem provar que largura13 é universalmente necessária. Top removido é sensibilidade contábil, não outro replay. Base in-sample e dois paths OHLC hipotéticos; nenhuma mudança forward. Dados completos mensais e por trade em summary/comparisons/direct_vs_gap13 e oito arquivos de trajetória.']
    (OUT/'report.md').write_text('\n\n'.join(lines),encoding='utf-8')

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    m=json.loads((g.INPUT/'manifest.json').read_text());cfg=m['config'];start=g.ms(m['start_brt']);end=g.ms(m['end_brt'])
    assert end==g.ms('2026-10-02T22:28:00-03:00')
    for tf,sha in m['cache_hashes'].items():assert g.digest(g.CACHE/f'SOLUSDT_{tf}.jsonl')==sha
    assert g.digest(g.PRIOR/'signals.json')==m['signals_sha256']
    candles=g.load_candle_cache(g.CACHE/'SOLUSDT_1m.jsonl')
    signals=[g.SignalEvent(r['boundary_ms'],g.EntrySignal(**r['signal'])) for r in json.loads((g.PRIOR/'signals.json').read_text())]
    notional=cfg['capital']['operational_balance_usdt']*cfg['capital']['trade_size_pct']/100
    spread=cfg.get('instrumentation',{}).get('market_bot_replay',{}).get('round_trip_spread_bps',5)
    for path in g.PATHS:
        for arm,(act,gap) in ARMS.items():
            print(path,arm,flush=True);inst=Instrumentation(start,end,notional)
            with patch.object(g.systemic,'BotFullExitPosition',inst.factory),patch.object(g.systemic,'process_candle_systemic',inst.processor):
                run=g.systemic.run_systemic(name=arm,config=arm_config(cfg,arm),signals=signals,candles=candles,contexts=[],start_ms=start,end_ms=end,path=path,spread_bps=spread,fast_enabled=False)
            serialized=g.serialize(run,signals,notional)
            if act==10:
                expected=json.loads((g.OUT/f'{path}_GAP_{gap}.json').read_text())['run']
                assert serialized==expected,'Full prior-run parity failed'
                print('FULL PRIOR PARITY PASS',flush=True)
            (OUT/f'{path}_{arm}.json').write_text(json.dumps({'run':serialized,'details':inst.details(),'exposure':{k:dict(v) for k,v in inst.exposure.items()}},allow_nan=False),encoding='utf-8')
    manifest={**m,'arms':ARMS,'notional':notional,'spread_bps':spread,'tool_sha256':g.digest(Path(__file__)),
        'source_hashes':{f:g.digest(g.ROOT/f) for f in ('tools/trail_gap_systemic_study.py','tools/be_off_cb_fast_drop_systemic_replay.py','src/position/bot_full_engine.py')},
        'parity':'ACT10_GAP5 and ACT10_GAP13 full serialized runs exactly match prior frozen runs HIGH/LOW'}
    finalize(manifest);print('DONE',OUT,flush=True)

if __name__=='__main__':main()
