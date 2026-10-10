import unittest
from tools.price_structure_readout import context_series,at_entry,scoped,metrics,contrast
from tools.price_structure_study import START
from tools.market_selection_study import MarketCandle

class ReadoutTests(unittest.TestCase):
    def test_close_at_entry_boundary_is_already_available(self):
        hs=[MarketCandle(i*3600000,(i+1)*3600000-1,5,10,3,5,0,0) for i in range(72)]
        s=context_series(hs,3);at=72*3600000
        self.assertEqual(at_entry(s,[x['available_ms'] for x in s],at)['latest_closed_ms'],at-1)
        self.assertEqual(at_entry(s,[x['available_ms'] for x in s],at-1)['latest_closed_ms'],at-3600000-1)
    def test_censor_old_window_does_not_use_future_exit(self):
        s=[dict(available_ms=START,label='BULL',episode=1,latest_closed_ms=START-1,tops=[],bottoms=[])]
        t=dict(opened_ms=START,closed_ms=START+120000,source_candle=1,net_usd=1,exit_reason='TRAILING',exit_price=150,peak_price=200)
        row=scoped([t],s,START+60000)[0]
        self.assertFalse(row['resolved']);self.assertIsNone(row['net_usd']);self.assertEqual(metrics([row])['N'],0)
        self.assertNotIn('exit_price',row);self.assertNotIn('peak_price',row)
    def test_common_pair_and_exclusive_delta(self):
        def row(source,net):return dict(source_candle=source,opened_ms=START,closed_ms=START+60000,
            context='BULL',episode=1,entry_day='2026-06-01',net_usd=net,exit_reason='PROFIT_LOCK',resolved=True)
        c=contrast([row(1,.1),row(2,.2)],[row(1,.3),row(3,.4)],START+14*86400000)
        self.assertAlmostEqual(c['delta'],.4);self.assertAlmostEqual(c['common_delta'],.2)
        self.assertEqual(c['overlap'],dict(common=1,arm_only=1,baseline_only=1))

if __name__=='__main__':unittest.main()
