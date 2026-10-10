"""Secondary forward description only; no inferred replay or projected MTM."""
import json,sys,hashlib
from pathlib import Path
from collections import Counter
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.price_structure_readout import context_series,at_entry,LABELS
from tools.price_structure_study import OUT,g,a
SPECS={
 'REAL_A':('trades_B.jsonl','open_positions.json','2026-08-28T22:56:32-03:00'),
 'BE_OFF_CB_SHADOW':('trades_be_off_cb_shadow.jsonl','be_off_cb_shadow.json','2026-09-19T19:36:33-03:00'),
 'BE_OFF_CB_MACD_BU_MINUS_SHADOW':('trades_be_off_cb_macd_bu_minus_shadow.jsonl','be_off_cb_macd_bu_minus_shadow.json','2026-09-27T20:07:29-03:00'),
 'BE_OFF_CB_EMA_MACD_SHADOW':('trades_be_off_cb_ema_macd_shadow.jsonl','be_off_cb_ema_macd_shadow.json','2026-09-27T20:07:29-03:00'),
 'EMA_MACD_HIST_1M_SHADOW':('trades_ema_macd_hist_1m_shadow.jsonl','ema_macd_hist_1m_shadow.json','2026-09-30T22:03:09-03:00'),
 'DMI15_TRAJECTORY_CONTEXT_SHADOW':('trades_dmi15_trajectory_context_shadow.jsonl','dmi15_trajectory_context_shadow.json','2026-08-28T22:56:32-03:00'),
 'BE_OFF_CB_ACT20_GAP5_SHADOW':('trades_be_off_cb_act20_gap5_shadow.jsonl','be_off_cb_act20_gap5_shadow.json','2026-10-05T02:34:39-03:00'),
 'BE_OFF_CB_ACT10_GAP13_SHADOW':('trades_be_off_cb_act10_gap13_shadow.jsonl','be_off_cb_act10_gap13_shadow.json','2026-10-05T02:34:39-03:00')}
PERF=g.ms('2026-10-08T23:52:00-03:00');FREEZE=g.ms('2026-09-29T13:25:12-03:00');RESTART=g.ms('2026-09-30T22:03:09-03:00')
WARM=g.ms('2026-09-28T11:52:14-03:00');COMMON=g.ms('2026-10-05T02:34:39-03:00')

def main():
    end=json.loads((OUT/'market_manifest.json').read_text())['end_ms']
    series=context_series(g.load_candle_cache(OUT/'market/SOLUSDT_1h.jsonl'),3);bounds=[x['available_ms'] for x in series]
    raw=OUT/'forward_raw';results={};rows_all={};hashes={}
    for arm,(ledger,state,start) in SPECS.items():
        file=raw/ledger;hashes[ledger]=hashlib.sha256(file.read_bytes()).hexdigest()
        records=[json.loads(l) for l in file.read_text(encoding='utf-8').splitlines() if l.strip()]
        if arm=='REAL_A':records=[r for r in records if r.get('position_type')=='BOT_EXIT' and not r.get('shadow_kind') and not r.get('phantom')]
        current=json.loads((raw/state).read_text(encoding='utf-8'));positions=current if isinstance(current,list) else current.get('positions',[])
        if arm=='REAL_A':positions=[r for r in positions if r.get('position_type','BOT_EXIT')=='BOT_EXIT' and not r.get('shadow_kind') and not r.get('phantom')]
        seen={r.get('pair_id') for r in records};records += [{**r,'opened_at':r.get('opened_at') or r.get('open_ts'),'closed_at':None} for r in positions if r.get('pair_id') not in seen]
        rows=[]
        for r in records:
            opened=r.get('opened_at') or r.get('open_ts')
            if not opened:continue
            at=g.ms(opened)
            if not g.ms(start)<=at<=end:continue
            closed=g.ms(r['closed_at']) if r.get('closed_at') else None
            resolved=closed is not None and closed<=end
            context=at_entry(series,bounds,at);status='VALID_DESCRIPTIVE'
            cb=arm not in ['REAL_A','DMI15_TRAJECTORY_CONTEXT_SHADOW']
            if cb and at<RESTART and (closed is None or closed>=FREEZE) and at<=RESTART:status='FREEZE_INVALID_OR_CROSSING'
            elif arm in ['BE_OFF_CB_SHADOW','BE_OFF_CB_MACD_BU_MINUS_SHADOW','BE_OFF_CB_EMA_MACD_SHADOW'] and g.ms('2026-09-27T20:07:29-03:00')<=at<WARM:status='WARMUP_NON_COMPARABLE'
            pct=r.get('net_pnl_pct');net=float(pct)*20/100 if resolved and pct is not None else None
            rows.append(dict(source_candle=r.get('source_candle_open_time'),pair_id=r.get('pair_id'),opened_ms=at,closed_ms=closed if resolved else None,
                net_normalized_20=net,resolved=resolved,exit_reason=r.get('exit_reason') if resolved else 'OPEN',context=context['label'],episode=context['episode'],
                status=status,phase='AFTER_08OCT_2352' if at>=PERF else 'BEFORE_08OCT_2352',carry_over=at<PERF and (closed is None or closed>=PERF)))
        rows_all[arm]=rows
        def summary(rs):
            resolved=[r for r in rs if r['resolved'] and r['net_normalized_20'] is not None]
            return dict(entries=len(rs),closed=len(resolved),open_unresolved=len(rs)-len(resolved),
                net_normalized_20=sum(r['net_normalized_20'] for r in resolved),
                net_trade_normalized_20=sum(r['net_normalized_20'] for r in resolved)/len(resolved) if resolved else None,
                exits=dict(Counter(r['exit_reason'] for r in resolved)),episodes=len({r['episode'] for r in rs}),
                carry_over=sum(r['carry_over'] for r in rs),validity=dict(Counter(r['status'] for r in rs)))
        results[arm]=dict(start_brt=start,own_cohort={phase:{name:summary([r for r in rows if r['phase']==phase and (name=='TOTAL' or r['context']==name)]) for name in LABELS} for phase in ['BEFORE_08OCT_2352','AFTER_08OCT_2352']},
            common_calendar={phase:{name:summary([r for r in rows if r['opened_ms']>=COMMON and r['phase']==phase and (name=='TOTAL' or r['context']==name)]) for name in LABELS} for phase in ['BEFORE_08OCT_2352','AFTER_08OCT_2352']})
    (OUT/'forward_summary.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    (OUT/'forward_classified_trades.json').write_text(json.dumps(rows_all,indent=2),encoding='utf-8')
    (OUT/'forward_manifest.json').write_text(json.dumps(dict(hashes=hashes,cutoff_ms=end,snapshot='non-atomic read-only copies; no completeness guarantee between ledger/state snapshots'),indent=2),encoding='utf-8')
    lines=['# Forward secundário — exclusivamente descritivo',
        'Coortes não têm início idêntico. Antes/depois08/10 23:52 separa entradas, não reseta CB/slots. Carry-over pertence à fase de entrada e é identificado; não significa dado sem lag após o marco. Net normalizado a20USDT usa net% persistido; REAL_A vem de fills Testnet, shadows de execução phantom. Não impor spread5bps retroativamente aos ledgers. Sem conclusão, IC ou winner forward.',
        'Há snapshots não atômicos de ledger/estado, posteriores ao corte de mercado. Eles não certificam integridade completa de admissões. Flags de freeze/warm-up são exibidas, não usadas como evidência econômica. Contexto72h reconstruído dos candles, nunca do exit-context persistido.',
        '## Calendário comum desde05/10 02:34:39 — não equaliza estados herdados',
        g.table(['arm','fase','context','entries','closed','open/unresolved','net /20','net/trade /20','carry','episódios','status','exits'],
            [[arm,phase,name,x['entries'],x['closed'],x['open_unresolved'],g.fmt(x['net_normalized_20']),g.fmt(x['net_trade_normalized_20']),x['carry_over'],x['episodes'],str(x['validity']),str(x['exits'])] for arm,v in results.items() for phase,cs in v['common_calendar'].items() for name,x in cs.items()]),
        '## Inícios individuais',g.table(['arm','coorte BRT'],[[arm,v['start_brt']] for arm,v in results.items()]),
        'As tabelas completas de cada coorte individual e todos os trades/flags constam em forward_summary.json e forward_classified_trades.json. Resultados warm-up/freeze não sustentam comparação. O calendário comum apenas restringe datas, não prova condições de admissão equivalentes. Nenhum braço sem replay participa da conclusão primária.']
    (OUT/'FORWARD.md').write_text('\n\n'.join(lines),encoding='utf-8')
    print('FORWARD descriptive only, eight arms, no primary inference',flush=True)

if __name__=='__main__':main()
