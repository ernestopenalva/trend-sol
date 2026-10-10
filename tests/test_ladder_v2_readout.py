import unittest
from tools.ladder_v2_readout import band, contrast, bootstrap, observations
from tools.market_selection_study import MarketCandle

class ReadoutTests(unittest.TestCase):
    def t(self,s,net,closed,owner='PL2',reason='PROFIT_LOCK'):
        return dict(source_candle=s,opened_ms=0,closed_ms=closed,net_usd=net,owner=owner,exit_reason=reason)
    def test_pairing_censoring_decomposition(self):
        c=[self.t(1,.1,60),self.t(2,.2,60),self.t(3,.3,60)]
        v=[self.t(1,.5,120,'TRAIL','TRAILING'),self.t(2,None,None),self.t(4,-.1,70)]
        x=contrast(c,v,1000)
        self.assertAlmostEqual(sum(x['decomposition'].values()),-.2)
        self.assertEqual(x['continued_PL']['resolved'],1)
        self.assertEqual(x['continued_PL']['censored'],1)
        self.assertAlmostEqual(x['continued_PL']['delta'],.4)
    def test_prespecified_bands(self):
        self.assertEqual([band(v) for v in [7.9,8,10,13,20]],['<8','8–10','10–13','13–20','>=20'])
    def test_bootstrap_zero_delta(self):
        rows=[self.t(1,.1,60000)]
        x=bootstrap(rows,rows,0,86400000*14)
        self.assertEqual(x['delta'],0);self.assertEqual(x['ci95'],[0,0])
    def test_reconstruct_does_not_visit_price_after_exit(self):
        d=dict(opened_ms=0,closed_ms=60000,entry_price=100,terminal_peak=103,
            floor_events=[dict(tick_index=3,price=100.5)])
        c=MarketCandle(0,59999,100,103,99,102,0,0)
        rows=observations(d,{},'HIGH_FIRST',{60000:c},60000)
        self.assertEqual([r['price'] for r in rows],[100,103,100.5])

if __name__=='__main__':unittest.main()
