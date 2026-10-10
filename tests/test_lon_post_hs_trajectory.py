import unittest
from types import SimpleNamespace

from tools.lon_post_hs_trajectory import EPISODE_GAP_MS, HORIZONS, NUMERIC, CATEGORICAL, episodes, feature, first_loss, slot_cost, compare_horizons


class LonPostHsTests(unittest.TestCase):
    def test_episode_grouping_fixed_sixty_minute_transitive(self):
        cases=[{'hs_ms':at,'source_candle':i} for i,at in enumerate((0,3600000,7200000,10800001))]
        groups=episodes(cases)
        self.assertEqual([len(g) for g in groups],[3,1])
        self.assertEqual(EPISODE_GAP_MS,3600000)

    def test_closed_cases_have_no_post_exit_features(self):
        out=feature({'hs_ms':1000},[],61000,None,50000,1)
        self.assertEqual(out['status'],'CLOSED_AT_OR_BEFORE_CUT')
        self.assertIsNone(out['features'])

    def test_first_loss_uses_only_known_trace(self):
        trace=[{'at_ms':1000,'kind':'CONTEXT','snapshot':{'ema_context':'LON'}},
               {'at_ms':120000,'kind':'CONTEXT','snapshot':{'ema_context':'BUL'}}]
        known=[r for r in trace if r['at_ms']<=60000]
        self.assertIsNone(first_loss(known,1000))
        self.assertEqual(first_loss(trace,1000),120000)

    def test_episode_capacity_counts_two_extra_slots_and_dedupes_signal(self):
        cases=[{'hs_ms':0,'closed_ms':600000}]*2
        control={'admissions':[{'at_ms':60000,'decision':'ADMITTED','source_candle':7}],
            'trades':[{'opened_ms':10000,'closed_ms':100000}]*4}
        one=slot_cost(cases[:1],control,3,5)
        two=slot_cost(cases,control,3,5)
        self.assertEqual(one['potential_capacity_conflicts'],0)
        self.assertEqual(two['potential_capacity_conflicts'],1)
        self.assertEqual(two['opportunities'],1)
        self.assertEqual(two['slot_minutes'],6)

    def test_slot_time_stops_at_actual_exit(self):
        out=slot_cost([{'hs_ms':0,'closed_ms':120000}],{'admissions':[],'trades':[]},60,5)
        self.assertEqual(out['slot_minutes'],2)
        self.assertEqual(out['still_open'],0)

    def test_admission_at_exit_time_has_no_extra_slot_conflict(self):
        control={'admissions':[{'at_ms':60000,'decision':'ADMITTED','source_candle':7}],
            'trades':[{'opened_ms':10000,'closed_ms':100000}]*5}
        out=slot_cost([{'hs_ms':0,'closed_ms':60000}],control,3,5)
        self.assertEqual(out['potential_capacity_conflicts'],0)
        self.assertEqual(out['opportunities'],0)

    def test_live_features_do_not_expose_future_lon_loss(self):
        state={'entry_price':100,'entry_atr':1,'profit_lock_stop':None,'trailing_stop':None,
            'stop_type':'review','hs_elastic':True,'hard_stop_price':None,'review_stop':70,'effective_stop':70}
        snap={'ema_context':'LON','macd_context':'BU-','latest_closed_at_ms':0,
            'latest_open_at_ms':-300000,'previous_closed_at_ms':-300000,'macd_line':.1,'macd_line_previous':.2}
        for n in (50,100,200):
            snap.update({f'ema{n}':100+n/100,f'ema{n}_previous':100,f'ema{n}_direction':'UP'})
        candle=SimpleNamespace(high=101,low=99,close=100)
        review=SimpleNamespace(context=lambda at:snap,five_boundaries=[0],candles={'5m':[candle]},spread=0,notional=20,fees=0)
        capture={'hs_ms':60000,'causal_at_ms':0,'opened_ms':0,'state':state,'trigger_price':98.5,'snapshot':snap,'control_net':-.3}
        trace=[{'at_ms':120000,'kind':'PRICE','price':99,'state':state,'snapshot':snap},
               {'at_ms':180000,'kind':'CONTEXT','price':97,'state':state,'snapshot':{'ema_context':'BUL'}}]
        out=feature(capture,trace,120000,review,360000,1)['features']
        self.assertIsNone(out['LON_lost_at_ms'])
        self.assertIsNone(out['since_LON_lost_min'])
        self.assertAlmostEqual(out['pnl_pct'],-1)

    def test_episode_comparison_does_not_treat_cluster_as_independent(self):
        cases=[]
        for kind,ep,count in [('BETTER','E1',2),('WORSE','E2',1)]:
            features={k:1 for k in NUMERIC}
            features.update({k:'LON' for k in CATEGORICAL})
            for _ in range(count):
                cases.append({'outcome':kind,'episode_id':ep,
                    'horizons':{str(h):{'features':features} for h in HORIZONS}})
        g=compare_horizons(cases)['1']
        self.assertEqual(g['eligible']['BETTER'],2)
        self.assertEqual(g['episode_N']['BETTER'],1)
        self.assertEqual(g['episode_numeric']['pnl_pct']['BETTER']['n'],1)


if __name__=='__main__':unittest.main()
