import json
import unittest
from copy import deepcopy
from unittest.mock import patch
from tools.trail_activation_gap_systemic_study import arm_config,Instrumentation,intervals,dominance_groups,ARMS
from tools import trail_gap_systemic_study as g
from tools.market_bot_replay import ReplayExecutionClient,NullLogger
from tools.market_selection_study import MarketCandle

class ActivationTests(unittest.TestCase):
    def position(self,activation):
        inst=Instrumentation(0,9999999999999,20);c=ReplayExecutionClient(0)
        cfg={'hard_stop':{'enabled':True,'stop_pct':1.5},'breakeven':{'mode':'off'},'trailing':{'mode':'atr','activation_atr':activation,'gap_atr':5},
            'profit_lock':{'mode':'atr','steps':[{'trigger_atr':5,'lock_atr':1.5},{'trigger_atr':8,'lock_atr':3},{'trigger_atr':12,'lock_atr':6}]}}
        p=inst.factory(pair_id='t',symbol='SOLUSDT',entry_price=100,quantity=.2,entry_order={},open_ts='2026-06-01T00:00:00+00:00',config=cfg,client=c,logger=NullLogger(),entry_atr=1)
        return p,c

    def tick(self,p,c,price,k):
        c.current_price=price;p.on_tick(price,f'2026-06-01T00:{k:02d}:00+00:00')

    def test_only_two_config_fields_change_and_original_preserved(self):
        cfg=json.loads((g.INPUT/'manifest.json').read_text())['config'];original=deepcopy(cfg)
        for arm,(a,b) in ARMS.items():
            result=arm_config(cfg,arm);self.assertEqual(result['risk']['trailing']['activation_atr'],a)
            self.assertEqual(result['risk']['trailing']['gap_atr'],b)
            result['risk']['trailing']=deepcopy(cfg['risk']['trailing']);self.assertEqual(result,cfg)
        self.assertEqual(cfg,original)

    def test_pl_remains_dominant_until_delayed_activation(self):
        for activation in (15,20):
            p,c=self.position(activation);self.tick(p,c,112,1)
            self.assertFalse(p.trailing_active);self.assertEqual(g.dominant(p),'PL3')
            self.tick(p,c,100+activation,2)
            self.assertTrue(p.trailing_active);self.assertEqual(g.dominant(p),'TRAIL')
            self.assertEqual(p.first_dominance['peak_atr'],activation);self.assertEqual(p.entry_atr,1)

    def test_never_activated_is_not_never_dominated_active_group(self):
        p,c=self.position(20);self.tick(p,c,112,1);self.tick(p,c,106,2)
        self.assertEqual(p.status,'CLOSED');self.assertIsNone(p.activation);self.assertIsNone(p.first_dominance)

    def test_tick_order_distinguishes_new_peak_same_minute_after_dominance(self):
        p,c=self.position(15);self.tick(p,c,115,1);self.tick(p,c,116,1)
        self.assertGreater(p.peak_events[-1]['tick_index'],p.first_dominance['tick_index'])
        self.assertEqual(p.peak_events[-1]['at_ms'],p.first_dominance['at_ms'])

    def test_pre_and_post_activation_pl_duration_not_conflated(self):
        d={'opened_ms':0,'closed_ms':240000,'first_dominance':{'at_ms':180000},'floor_events':[
            {'at_ms':60000,'owner':'PL2','trail_active':False},{'at_ms':120000,'owner':'PL3','trail_active':True},
            {'at_ms':180000,'owner':'TRAIL','trail_active':True}]}
        r=intervals(d,999999);self.assertEqual(r['pre_activation_PL2'],1)
        self.assertEqual(r['post_activation_PL3'],1);self.assertEqual(r['post_dominance_TRAIL'],1)
        self.assertEqual(sum(v for k,v in r.items() if k.startswith('total_')),4)

    def test_giveback_groups_honor_dominance_and_real_exit_owner(self):
        def trade(s):return {'source_candle':s,'closed_ms':180000,'opened_ms':0,'entry_price':100,'exit_price':108,'peak_price':120,'net_usd':1,'exit_reason':'PROFIT_LOCK'}
        ds=[]
        for s,dom in ((1,{'at_ms':60000,'tick_index':1,'peak':115,'peak_atr':15,'price':115}),(2,None)):
            ds.append({'source_candle':s,'opened_ms':0,'closed_ms':180000,'first_dominance':dom,'activation':{'at_ms':60000},'terminal_owner':'PL3',
                'peaks':[{'at_ms':60000,'tick_index':2,'price':120}], 'floor_events':[{'at_ms':60000,'owner':'TRAIL' if dom else 'PL3','trail_active':True,'tick_index':1},{'at_ms':120000,'owner':'PL3','trail_active':True,'tick_index':3}]})
        r=dominance_groups({'trades':[trade(1),trade(2)]},ds,180000)
        self.assertEqual(r['dominated']['n'],1);self.assertEqual(r['activated_never_dominated']['n'],1)
        self.assertEqual(r['dominated']['exit_owners'],{'PL3':1})
        self.assertEqual(r['dominated']['new_peak_frequency'],1)
        self.assertAlmostEqual(r['dominated']['post_dominance_price_pnl_usd']['mean'],-1.4)
        self.assertAlmostEqual(r['dominated']['post_dominance_net_residual_usd']['mean'],-2)

    def test_delayed_activation_has_independent_capacity_and_admissions(self):
        cfg=json.loads((g.INPUT/'manifest.json').read_text())['config'];cfg['capital']['max_open_positions']=1
        start=g.ms('2026-06-01T00:00:00-03:00');end=start+2*g.MINUTE_MS
        signals=[g.SignalEvent(t,g.EntrySignal(symbol='SOLUSDT',price=price,ts='t',source_candle_open_time=t-g.MINUTE_MS,entry_atr=1,atr_timeframe='1m',atr_period=14)) for t,price in ((start,100),(end,104))]
        candles=[MarketCandle(start-g.MINUTE_MS,start-1,100,100,100,100,0,0),MarketCandle(start,start+g.MINUTE_MS-1,100,111,104,104,0,0),MarketCandle(start+g.MINUTE_MS,end-1,104,104,104,104,0,0)]
        results={}
        for arm in ('ACT10_GAP5','ACT15_GAP5','ACT20_GAP5'):
            inst=Instrumentation(start,end,20)
            with patch.object(g.systemic,'BotFullExitPosition',inst.factory),patch.object(g.systemic,'process_candle_systemic',inst.processor):
                run=g.systemic.run_systemic(name=arm,config=arm_config(cfg,arm),signals=signals,candles=candles,contexts=[],start_ms=start,end_ms=end,path='HIGH_FIRST',spread_bps=5,fast_enabled=False)
            results[arm]=run.admission_audit[-1]['decision']
        self.assertEqual(results,{'ACT10_GAP5':'ADMITTED','ACT15_GAP5':'CAPACITY','ACT20_GAP5':'CAPACITY'})

if __name__=='__main__':unittest.main()
