import unittest
from tools.real_a_circuit_breaker_replay import CircuitGuard, Rule
from tools.ge_replay_study import ReplayResult, ReplayTrade

class CircuitReplayTests(unittest.TestCase):
    def test_pnl_guard_pauses_only_on_crossing(self):
        r=ReplayResult("x",0,0);g=CircuitGuard(Rule("x","PNL",.5,2),1,100,20)
        r.trades.append(ReplayTrade(0,60_000,1,1,1,1,-3, -3,"HARD_STOP"))
        self.assertFalse(g.allows(60_000,r)); self.assertFalse(g.allows(120_000,r)); self.assertEqual(g.crises,1)
