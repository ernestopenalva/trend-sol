import unittest
from tools.stop_ownership_diagnostic import classify

class StopOwnershipTests(unittest.TestCase):
    def sample(self, events):
        d=dict(entry_price=100.,entry_atr=.05,opened_ms=0,closed_ms=120000,
            source_candle=0,terminal_peak=100.6,terminal_stop=100.35,floor_events=events)
        return classify(d,dict(net_usd=.01,exit_reason='TRAILING'),[.04,.08,.12])
    def event(self,at,peak,stop,owner,armed):
        return dict(at_ms=at,tick_index=at,peak=peak,effective_stop=stop,owner=owner,PL_armed=armed)
    def test_equal_economic_floors_redundant_second_step(self):
        r=self.sample([self.event(1,100.45,100.25,'PL1',['atr:1']),
            self.event(2,100.5,100.25,'PL2',['atr:1','atr:2'])])
        self.assertEqual([a['changed'] for a in r['arms']],[True,False])
        self.assertEqual(r['regressions'],0)
    def test_simultaneous_arms_follow_engine_order(self):
        r=self.sample([self.event(1,100.5,100.25,'PL2',['atr:1','atr:2'])])
        self.assertEqual([a['changed'] for a in r['arms']],[True,False])
        self.assertEqual(r['timeline'][0]['owner'],'HARD_STOP')
    def test_takeover_uses_immediate_pl_floor(self):
        r=self.sample([self.event(1,100.45,100.25,'PL1',['atr:1']),
            self.event(2,100.6,100.35,'TRAIL',['atr:1','atr:2'])])
        self.assertAlmostEqual(r['transitions'][0]['takeover_jump_atr'],2.)
        self.assertTrue(r['zone']['reached_trail'])
    def test_zone_censored_and_regression_detected(self):
        r=self.sample([self.event(1,100.45,100.25,'PL1',['atr:1']),
            self.event(2,100.46,100.24,'PL2',['atr:1','atr:2'])])
        self.assertTrue(r['zone']['censored_by_exit'])
        self.assertEqual(r['effective_stop_decreases'],1)

if __name__=='__main__': unittest.main()
