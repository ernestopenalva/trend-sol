import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from tools.trail_gap_systemic_study import (Instrumentation,dominant,activation_case,INPUT,ms,systemic,
    SignalEvent,EntrySignal,active_floor_minutes,realized_contributions)
from tools.market_bot_replay import ReplayExecutionClient,NullLogger,MINUTE_MS,_bot_exit_config
from tools.market_selection_study import MarketCandle


class GapSystemicTests(unittest.TestCase):
    def position(self,gap):
        cfg={'hard_stop':{'enabled':True,'stop_pct':1.5},'breakeven':{'mode':'off'},
            'trailing':{'mode':'atr','activation_atr':10,'gap_atr':gap},
            'profit_lock':{'mode':'atr','steps':[{'trigger_atr':5,'lock_atr':1.5},
                {'trigger_atr':8,'lock_atr':3},{'trigger_atr':12,'lock_atr':6}]}}
        inst=Instrumentation(0,999999,20);client=ReplayExecutionClient(0)
        p=inst.factory(pair_id='t',symbol='SOLUSDT',entry_price=100,quantity=.2,
            entry_order={},open_ts='2026-06-01T00:00:00+00:00',config=cfg,client=client,logger=NullLogger(),entry_atr=1)
        return p,client

    def tick(self,p,c,price,k=1):
        c.current_price=price;p.on_tick(price,f'2026-06-01T00:{k:02d}:00+00:00')

    def test_activation_and_effective_dominance_all_fixed_gaps(self):
        for gap,owner,case in [(5,'TRAIL','TRAIL_ABOVE_PL'),(7,'PL2','TIE'),
            (9,'PL2','PL_ABOVE_TRAIL'),(13,'PL2','PL_ABOVE_TRAIL')]:
            p,c=self.position(gap);self.tick(p,c,110)
            self.assertTrue(p.trailing_active);self.assertEqual(p.entry_atr,1)
            self.assertEqual(p.trailing_activation_atr,10)
            self.assertEqual(dominant(p),owner);self.assertEqual(p.activation['comparison'],case)
            self.assertTrue(p.activation['PL2_armed']);self.assertEqual(p.hard_stop_price,98.5)

    def test_pl3_delays_gap9_dominance_until_above15(self):
        p,c=self.position(9)
        for k,price in enumerate((110,112,115),1):self.tick(p,c,price,k)
        self.assertEqual(dominant(p),'PL3');self.assertIsNone(p.first_dominance)
        self.tick(p,c,116,4);self.assertEqual(dominant(p),'TRAIL')
        self.assertEqual(p.first_dominance['peak_atr'],16)
        self.assertEqual(p.first_dominance['minutes_from_activation'],3)

    def test_gap13_delay_and_ratchet(self):
        p,c=self.position(13)
        for k,price in enumerate((110,112,119),1):self.tick(p,c,price,k)
        self.assertEqual(dominant(p),'PL3');self.assertIsNone(p.first_dominance)
        self.tick(p,c,120,4);self.assertEqual(p.first_dominance['peak_atr'],20)
        stop=p.effective_stop;self.tick(p,c,119,5);self.assertEqual(p.effective_stop,stop)

    def test_owner_can_return_from_trail_to_pl(self):
        p,c=self.position(7);self.tick(p,c,111)
        self.assertEqual(dominant(p),'TRAIL');self.tick(p,c,112,2)
        self.assertEqual(dominant(p),'PL3');self.assertTrue(p.trailing_active)

    def test_peak_levels_are_observed_overshoot_not_interpolation(self):
        p,c=self.position(5);self.tick(p,c,116)
        for level in (5,8,10,12,15):self.assertEqual(p.levels[level]['peak_atr'],16)
        self.assertNotIn(20,p.levels)

    def test_no_pl_is_not_invented_tie(self):
        self.assertEqual(activation_case(99,None),'NO_PL')

    def test_detail_exit_timestamp_is_model_clock_not_wall_clock(self):
        p,c=self.position(5);self.tick(p,c,110);self.tick(p,c,105,2)
        self.assertEqual(p.status,'CLOSED')
        self.assertEqual(p.study.details()[0]['closed_ms'],ms('2026-06-01T00:02:00+00:00'))

    def test_economic_floor_can_leave_pl2_unarmed_at_trail_activation(self):
        cfg=_bot_exit_config(json.loads((INPUT/'manifest.json').read_text())['config'])
        inst=Instrumentation(0,999999,20);c=ReplayExecutionClient(0)
        p=inst.factory(pair_id='f',symbol='SOLUSDT',entry_price=100,quantity=.2,
            entry_order={},open_ts='2026-06-01T00:00:00+00:00',config=cfg,client=c,logger=NullLogger(),entry_atr=.04)
        self.tick(p,c,100.4)
        self.assertTrue(p.trailing_active);self.assertFalse(p.activation['PL2_armed'])
        self.assertEqual(dominant(p),'PL1');self.assertEqual(p.activation['comparison'],'PL_ABOVE_TRAIL')

    def test_after_activation_floor_time_uses_last_same_minute_state(self):
        d={'opened_ms':0,'closed_ms':180000,'floor_events':[
            {'at_ms':60000,'owner':'TRAIL','trail_active':True},
            {'at_ms':60000,'owner':'PL3','trail_active':True},
            {'at_ms':120000,'owner':'TRAIL','trail_active':True}]}
        self.assertEqual(active_floor_minutes(d,999999),{'PL3':1,'TRAIL':1})

    def test_top_contributions_include_changed_admissions_and_keep_open_null(self):
        def t(s,net):return {'source_candle':s,'closed_ms':100 if net is not None else None,'net_usd':net}
        c={'trades':[t(1,1),t(2,-2),t(4,None)]}
        v={'trades':[t(1,3),t(3,4),t(4,None)]}
        rows=realized_contributions(c,v)
        self.assertEqual(sum(r['contribution'] for r in rows),8)
        self.assertEqual(rows[0]['kind'],'VARIANT_ONLY')
        self.assertEqual(v['trades'][-1]['net_usd'],None)

    def test_full_system_releases_slots_only_when_each_arm_exits(self):
        cfg=json.loads((INPUT/'manifest.json').read_text())['config'];original=deepcopy(cfg)
        cfg['capital']['max_open_positions']=1
        start=ms('2026-06-01T00:00:00-03:00');end=start+2*MINUTE_MS
        signals=[SignalEvent(t,EntrySignal(symbol='SOLUSDT',price=price,ts='t',source_candle_open_time=t-MINUTE_MS,
            entry_atr=1,atr_timeframe='1m',atr_period=14)) for t,price in ((start,100),(end,104))]
        candles=[MarketCandle(start-MINUTE_MS,start-1,100,100,100,100,0,0),
            MarketCandle(start,start+MINUTE_MS-1,100,111,104,104,0,0),
            MarketCandle(start+MINUTE_MS,end-1,104,104,104,104,0,0)]
        decisions={}
        for gap in (5,13):
            c=deepcopy(cfg);c['risk']['trailing']['gap_atr']=gap
            inst=Instrumentation(start,end,20)
            with patch.object(systemic,'BotFullExitPosition',inst.factory),patch.object(systemic,'process_candle_systemic',inst.processor):
                r=systemic.run_systemic(name=f'GAP_{gap}',config=c,signals=signals,candles=candles,contexts=[],
                    start_ms=start,end_ms=end,path='HIGH_FIRST',spread_bps=5,fast_enabled=False)
            decisions[gap]=r.admission_audit[-1]['decision']
        self.assertEqual(decisions,{5:'ADMITTED',13:'CAPACITY'})
        self.assertEqual(original['risk']['trailing']['gap_atr'],5)
        self.assertEqual(cfg['risk']['trailing']['gap_atr'],5)


if __name__=='__main__':unittest.main()
