import io
import unittest
from types import SimpleNamespace
from tools.fast_drop_v2_study import context_at,evaluate,Observer


class FastV2Tests(unittest.TestCase):
    def test_fresh_requires_exact_last_closed_5m(self):
        value,fresh,expected=context_at([(299999,'SHO','BE-')],600000)
        self.assertFalse(fresh);self.assertEqual(expected,599999)
        self.assertTrue(context_at([(599999,'SHO','BE-')],600000)[1])
        self.assertEqual(context_at([(599999,'SHO','BE-'),(899999,'BUL','BU+')],600000)[0][1],'SHO')

    def test_same_fixed_thresholds_missing_reference_and_fresh_gate(self):
        self.assertTrue(evaluate(100,99.4,100.2,'SHO',True,'REEVALUATE',98.5)[3])
        self.assertFalse(evaluate(100,99.4,99.8,'SHO',True,'REEVALUATE',98.5)[3])
        self.assertFalse(evaluate(100,99.4,None,'SHO',True,'REEVALUATE',98.5)[3])
        self.assertFalse(evaluate(100,99.4,100.2,'BUL',True,'REEVALUATE',98.5)[3])
        self.assertTrue(evaluate(100,99.4,100.2,'SHO',False,'REEVALUATE',98.5)[3])
        self.assertFalse(evaluate(100,99.4,100.2,'SHO',False,'REEVALUATE_FRESH',98.5)[3])

    def run_two_bars(self,mode,missing=False):
        class Position:
            status='OPEN';entry_price=100;effective_stop=98.5;source_candle_open_time=1;pair_id='p'
            highest_price=100;trough_price=100;exit_price=None;exit_reason=None
            def on_tick(self,price,ts):
                self.trough_price=min(self.trough_price,price)
                if price<=self.effective_stop:self._close_at_market(price,'HARD_STOP',ts,None)
            def _close_at_market(self,price,reason,ts,target):
                self.status='CLOSED';self.exit_price=price;self.exit_reason=reason
            def pnl_pct(self,price):return (price/self.entry_price-1)*100
        p=Position();rp=SimpleNamespace(position=p,client=SimpleNamespace(current_price=0),opened_ms=0)
        a=SimpleNamespace(open_time_ms=600000,boundary_ms=660000,open=99.4,high=99.4,low=99.4,close=99.4)
        b=SimpleNamespace(open_time_ms=900000,boundary_ms=960000,open=99.3,high=99.3,low=99.3,close=99.3)
        refs={360000:SimpleNamespace(close=100.2,close_time_ms=359999),660000:SimpleNamespace(close=100.2,close_time_ms=659999)}
        if missing:refs.pop(360000)
        obs=Observer(mode,'HIGH_FIRST',io.StringIO());flags=set();trades=[]
        obs.process([rp],trades,a,'HIGH_FIRST',.2,True,flags,refs,[(599999,'BUL','BU+')])
        obs.process([rp],trades,b,'HIGH_FIRST',.2,True,flags,refs,[(599999,'BUL','BU+'),(899999,'SHO','BE-')])
        return p,obs,flags

    def test_one_shot_rejects_once_reevaluation_can_fire_later_at_real_price(self):
        p,o,f=self.run_two_bars('ONE_SHOT');self.assertEqual(p.status,'OPEN');self.assertEqual(o.stats[1]['valid'],1)
        p,o,f=self.run_two_bars('REEVALUATE');self.assertEqual(p.exit_reason,'FAST_DROP')
        self.assertEqual(p.exit_price,99.3);self.assertTrue(o.stats[1]['after_first'])

    def test_missing_buffer_does_not_consume_one_shot(self):
        p,o,f=self.run_two_bars('ONE_SHOT',True)
        self.assertEqual(p.exit_reason,'FAST_DROP');self.assertEqual(o.stats[1]['valid'],1)
        self.assertEqual(o.stats[1]['missing_reference'],1)

    def test_normal_stop_already_higher_than_fast_target(self):
        target,speed,predicates,allowed=evaluate(100,99.4,100.2,'SHO',True,'REEVALUATE',99.6)
        self.assertFalse(predicates['normal_priority']);self.assertFalse(allowed)

    def test_context_missing_is_not_fresh_or_ema_pass(self):
        self.assertEqual(context_at([],600000),(None,False,599999))
        self.assertFalse(evaluate(100,99.4,100.2,'UNAVAILABLE',False,'REEVALUATE_FRESH',98.5)[3])

    def test_endpoint_after_higher_normal_stop_is_not_a_loss_visit(self):
        class Position:
            status='OPEN';entry_price=100;effective_stop=100.1;source_candle_open_time=1;pair_id='p'
            highest_price=100.4;trough_price=100;exit_price=None;exit_reason=None
            def on_tick(self,price,ts):
                if price<=self.effective_stop:
                    self.status='CLOSED';self.exit_price=price;self.exit_reason='PROFIT_LOCK'
            def pnl_pct(self,price):return (price/self.entry_price-1)*100
        p=Position();rp=SimpleNamespace(position=p,client=SimpleNamespace(current_price=0),opened_ms=0)
        bar=SimpleNamespace(open_time_ms=600000,boundary_ms=660000,open=100.2,high=100.4,low=99.4,close=100.2)
        observer=Observer('ONE_SHOT','HIGH_FIRST',io.StringIO());trades=[]
        refs={360000:SimpleNamespace(close=100.2,close_time_ms=359999)}
        observer.process([rp],trades,bar,'HIGH_FIRST',.2,True,set(),refs,[(599999,'SHO','BE-')])
        self.assertEqual(observer.counts()['zone_trades'],0)
        self.assertEqual(p.exit_price,100.1)


if __name__=='__main__':unittest.main()
