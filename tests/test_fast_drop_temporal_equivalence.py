import unittest
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.position.bot_full_engine import BotFullExitPosition
from tests import test_fast_drop_ema_shadow as fixtures
from tools.be_off_cb_fast_drop_systemic_replay import _fast_decision, process_candle_systemic
from tools.market_bot_replay import NullLogger, ReplayExecutionClient
from tools.market_selection_study import MarketCandle


def millis(text):
    return int(datetime.fromisoformat(text).timestamp()*1000)


BOUNDARY = millis('2026-10-01T00:10:00+00:00')
OLD_CLOSE = BOUNDARY-300001


def snapshot(context, close=OLD_CLOSE):
    return {'tf_5m': {'ema_context':context, 'latest_closed_at_ms':close}}


def reference(boundary=BOUNDARY-300000, price=100.):
    return MarketCandle(boundary-60000, boundary-1, price, price, price, price, 1., 1)


class FastDropTemporalTests(unittest.TestCase):
    def make(self, root):
        fixture = fixtures.FastDropTests()
        shadow, config = fixture.make(root)
        fixture.open(shadow)
        shadow.on_closed_5m(snapshot('BEA'))
        return shadow, config

    def test_open_5m_candle_cannot_supply_context_to_either_path(self):
        for old, future, eligible in (('BEA','LON',True), ('LON','SHO',False)):
            with self.subTest(old=old,future=future), TemporaryDirectory() as tmp:
                shadow, _ = self.make(Path(tmp))
                shadow.on_closed_5m(snapshot(old))
                shadow.on_closed_5m(snapshot(future, BOUNDARY-1))
                shadow.on_closed_1m({'x':True,'T':OLD_CLOSE,'c':'100'})
                replay = _fast_decision(SimpleNamespace(entry_price=100.), BOUNDARY, 99.5,100.,
                    {BOUNDARY-300000:reference()}, [(OLD_CLOSE,old,''),(BOUNDARY-1,future,'')])
                self.assertEqual(replay[0], eligible)
                self.assertEqual(replay[2], old)
                shadow.on_tick(99.5,'2026-10-01T00:09:30+00:00')
                self.assertEqual(bool(shadow.closed_records),eligible)
                if eligible:
                    self.assertEqual(shadow.closed_records[0]['ema_latest_closed_at_ms'],OLD_CLOSE)

    def test_missing_reference_retries_below_loss_and_survives_restart(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp); shadow, _ = self.make(root)
            contexts=[(OLD_CLOSE,'BEA','')]
            self.assertFalse(_fast_decision(SimpleNamespace(entry_price=100.),BOUNDARY,99.5,100.,{},contexts)[0])
            shadow.on_tick(99.5,'2026-10-01T00:09:30+00:00')
            self.assertFalse(shadow.open_positions[0].fast_drop_evaluated)
            restored, _ = fixtures.FastDropTests().make(root)
            self.assertFalse(restored.open_positions[0].fast_drop_evaluated)
            # A newer reference may have arrived first; the missing older one must be accepted.
            restored.on_closed_1m({'x':True,'T':BOUNDARY-120001,'c':'100'})
            restored.on_closed_1m({'x':True,'T':OLD_CLOSE,'c':'100'})
            replay=_fast_decision(SimpleNamespace(entry_price=100.),BOUNDARY,99.4,99.5,
                                 {BOUNDARY-300000:reference()},contexts)
            self.assertTrue(replay[0])
            restored.on_tick(99.4,'2026-10-01T00:09:31+00:00')
            self.assertEqual(restored.closed_records[0]['exit_reason'],'FAST_DROP')
            self.assertAlmostEqual(restored.closed_records[0]['velocity_5m_pct_per_min'],replay[3])

    def test_competing_stops_have_same_priority_on_both_paths(self):
        for stop, expected in ((98.5,'FAST_DROP'),(99.5,'HARD_STOP'),(99.7,'HARD_STOP')):
            for path in ('HIGH_FIRST','LOW_FIRST'):
                with self.subTest(stop=stop,path=path), TemporaryDirectory() as tmp:
                    shadow, _ = self.make(Path(tmp))
                    shadow.on_closed_1m({'x':True,'T':OLD_CLOSE,'c':'100'})
                    position=shadow.open_positions[0]
                    position.hard_stop_price=stop
                    position._refresh_effective_stop()
                    client=ReplayExecutionClient(0.)
                    replay_position=BotFullExitPosition.from_state(position.to_state(),position.config,client,NullLogger())
                    opened=SimpleNamespace(position=replay_position,client=client,
                        opened_ms=millis(position.open_ts),notional=position.position_notional_usdt)
                    candle=MarketCandle(BOUNDARY-60000,BOUNDARY-1,100.,100.,98.,98.,1.,1)
                    trades=[]; evaluated=set()
                    process_candle_systemic([opened],trades,candle,path,.2,True,evaluated,
                        {BOUNDARY-300000:reference()},[(OLD_CLOSE,'BEA','')])
                    shadow.on_tick(100.,'2026-10-01T00:09:01+00:00')
                    shadow.on_tick(98.,'2026-10-01T00:09:30+00:00')
                    self.assertEqual(trades[0].exit_reason,expected)
                    self.assertEqual(shadow.closed_records[0]['exit_reason'],expected)

    def test_future_reference_close_is_not_eligible_in_replay(self):
        late = MarketCandle(BOUNDARY-300000,BOUNDARY+1000,100.,100.,100.,100.,1.,1)
        result=_fast_decision(SimpleNamespace(entry_price=100.),BOUNDARY,99.5,100.,
                             {BOUNDARY-300000:late},[(OLD_CLOSE,'SHO','')])
        self.assertFalse(result[0])
        self.assertIsNone(result[3])


if __name__=='__main__':
    unittest.main()
