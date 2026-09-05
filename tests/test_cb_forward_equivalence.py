import json
import random
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from src.monitor.cb_replay_clock import CBReplayClock
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal
from src.logging_utils import JsonlLogger
from tests.test_circuit_breaker_wiring import _config
from tools.real_a_circuit_breaker_replay import CircuitGuard, _rules


class ForwardEquivalenceTests(unittest.TestCase):
    def test_exact_clock_vs_replay_with_restart_expiry_retrigger_and_signals(self):
        rng = random.Random(312)
        original = CircuitGuard(_rules()[2], 6, 100, 20)
        forward = CBReplayClock()
        result = SimpleNamespace(trades=[])
        original_triggers, forward_triggers = [], []
        original_releases, forward_releases = [], []
        original_blocks, forward_blocks = [], []
        previously_paused = False
        count = 0
        for minute in range(2500):
            boundary = minute * 60_000
            closes = []
            if rng.random() < .15:
                for _ in range(rng.randint(1,3)):
                    pct = rng.choice([-1.7, -1.7, .05, 3.0])
                    result.trades.append(SimpleNamespace(closed_ms=boundary, net_pct=pct))
                    closes.append((boundary, 20*pct/100))
            allowed = original.allows(boundary, result)
            events = forward.minute(boundary, closes)
            if original.crises != count:
                original_triggers.append((boundary, original.pause_until))
            if previously_paused and allowed:
                original_releases.append(boundary)
            for e in events:
                if e['event'].endswith('TRIGGERED'):
                    forward_triggers.append((boundary, e['until']))
                else:
                    forward_releases.append(boundary)
            if minute % 3 == 0:
                if not allowed: original_blocks.append(boundary)
                if forward.paused: forward_blocks.append(boundary)
            self.assertEqual((original.equity,original.peak,original.was_true,original.crises,original.pause_until,original.paused_minutes,not allowed),
                (forward.equity,forward.peak,forward.was_true,forward.crises,forward.pause_until,forward.paused_minutes,forward.paused))
            previously_paused, count = not allowed, original.crises
            if minute % 7 == 0:
                forward = CBReplayClock.from_state(json.loads(json.dumps(forward.to_state())))
        self.assertGreater(count, 1)
        self.assertEqual(original_triggers,forward_triggers)
        self.assertEqual(original_releases,forward_releases)
        self.assertEqual(original_blocks,forward_blocks)

    def _integration(self, root, restart):
        config = _config()
        logger = JsonlLogger(root, config)
        make = lambda: CircuitBreakerShadow(root, config, logger, None)
        cb = make()
        t = datetime(2026,9,5,0,0,tzinfo=timezone.utc)
        def signal(at, price):
            return EntrySignal('SOLUSDT',price,at.isoformat(),int(at.timestamp()*1000)-60_000,.1,'1m',14)
        for n in range(5):
            at = t + timedelta(minutes=5*n)
            self.assertTrue(cb.on_approved_real_a_signal(signal(at,100+n), {}))
            if restart: cb = make()
        # All five HS exits, same minute, committed before detector evaluation.
        cb.on_tick(97,(t+timedelta(minutes=21,seconds=1)).isoformat())
        if restart: cb = make()
        cb.on_tick(97,(t+timedelta(minutes=22)).isoformat())
        self.assertTrue(cb.circuit_breaker_active)
        if restart: cb = make()
        self.assertFalse(cb.on_approved_real_a_signal(signal(t+timedelta(minutes=23),97),{}))
        if restart: cb = make()
        release = t+timedelta(hours=6,minutes=22)
        cb.on_tick(100,release.isoformat())
        if restart: cb = make()
        self.assertFalse(cb.circuit_breaker_active)
        self.assertTrue(cb.on_approved_real_a_signal(signal(release+timedelta(minutes=1),100),{}))
        # Persist BE/PL/TRAIL without any closure; restore that protection.
        cb.on_tick(101.5,(release+timedelta(minutes=1,seconds=10)).isoformat())
        if restart: cb = make()
        self.assertTrue(cb.open_positions[0].trailing_active)
        cb.on_tick(100.9,(release+timedelta(minutes=1,seconds=20)).isoformat())
        if restart: cb = make()
        cb.on_tick(100.9,(release+timedelta(minutes=2)).isoformat())
        self.assertTrue(cb.on_approved_real_a_signal(signal(release+timedelta(minutes=6),101),{}))
        if restart: cb = make()
        records=cb.ledger.load()
        self.assertEqual(len({x['pair_id'] for x in records}),len(records))
        net=sum(x['net_pnl_pct']*x['position_notional_usdt']/100 for x in records)
        self.assertAlmostEqual(cb.clock.equity+sum(x['net'] for x in cb.pending_closes),100+net)
        fields=('pair_id','opened_at','closed_at','entry_price','exit_price','exit_reason','net_pnl_pct')
        return {'trades':[{k:x[k] for k in fields} for x in records],
            'clock':cb.clock.to_state(), 'events':cb.audit_events,
            'buckets':cb.entries_by_bucket, 'blocked':cb.blocked_circuit_breaker,
            'opens':[(x.pair_id,x.entry_price,x.effective_stop) for x in cb.open_positions]}

    def test_full_integration_continuous_equals_restart(self):
        with TemporaryDirectory() as a, TemporaryDirectory() as b:
            self.assertEqual(self._integration(Path(a),False),self._integration(Path(b),True))

    def test_recovery_after_commit_before_ledger_projection(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); config=_config(); logger=JsonlLogger(root,config)
            cb=CircuitBreakerShadow(root,config,logger,None)
            at=datetime(2026,9,5,tzinfo=timezone.utc)
            signal=EntrySignal('SOLUSDT',100,at.isoformat(),int(at.timestamp()*1000)-60_000,.1,'1m',14)
            cb.on_signal(signal)
            # Ensure clock boundary already advanced, fail only the closure commit projection.
            with patch.object(cb,'_project_committed',side_effect=OSError('injected crash')):
                with self.assertRaises(OSError):
                    cb.on_tick(98,(at+timedelta(seconds=10)).isoformat())
            restored=CircuitBreakerShadow(root,config,logger,None)
            self.assertEqual(len(restored.ledger.load()),1)
            self.assertEqual(len(restored.open_positions),0)
            restored=CircuitBreakerShadow(root,config,logger,None)
            self.assertEqual(len(restored.ledger.load()),1)

    def test_recovery_before_checkpoint_replays_pending_input_once(self):
        from src.monitor import circuit_breaker_shadow as module
        for action in ('open','protection','close'):
            with self.subTest(action=action), TemporaryDirectory() as directory:
                root=Path(directory); config=_config(); logger=JsonlLogger(root,config)
                cb=CircuitBreakerShadow(root,config,logger,None)
                at=datetime(2026,9,5,tzinfo=timezone.utc)
                signal=EntrySignal('SOLUSDT',100,at.isoformat(),int(at.timestamp()*1000)-60_000,.1,'1m',14)
                if action != 'open': cb.on_signal(signal)
                original=module._atomic_json
                def injected(path,content):
                    if path == cb.state_path: raise OSError('crash before checkpoint')
                    return original(path,content)
                with patch.object(module,'_atomic_json',side_effect=injected):
                    with self.assertRaises(OSError):
                        if action=='open': cb.on_signal(signal)
                        else: cb.on_tick(101.5 if action=='protection' else 98,(at+timedelta(seconds=10)).isoformat())
                cb=CircuitBreakerShadow(root,config,logger,None)
                self.assertEqual(len(cb.ledger.load()),int(action=='close'))
                self.assertEqual(len(cb.open_positions),int(action!='close'))
                if action=='protection': self.assertTrue(cb.open_positions[0].trailing_active)
                events=cb.audit_events.copy(); sequence=cb.sequence
                cb=CircuitBreakerShadow(root,config,logger,None)
                self.assertEqual(events,cb.audit_events)
                self.assertEqual(sequence,cb.sequence)

    def test_no_skip_rolling_expiry_and_late_tick_rejected(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); config=_config(); logger=JsonlLogger(root,config)
            cb=CircuitBreakerShadow(root,config,logger,None)
            at=datetime(2026,9,5,tzinfo=timezone.utc)
            cb.on_tick(100,at.isoformat())
            cb.on_tick(100,(at+timedelta(minutes=1)).isoformat())
            with self.assertRaises(ValueError):
                cb.on_tick(100,(at+timedelta(seconds=59)).isoformat())
            self.assertFalse(cb.enabled)

    def test_report_does_not_mistake_open_trade_for_exclusive_entry(self):
        from tools.circuit_breaker_shadow_report import _path_attribution
        trade={'source_candle_open_time':123}
        self.assertEqual(_path_attribution([trade],[trade],[]),{'common':1})
        events=[{'event':'REAL_A_ADMISSION_OBSERVED','source_candle_open_time':123,'outcome':'blocked'}]
        result=_path_attribution([], [trade], events)
        self.assertEqual(result['CB-only/real admission declined (independent path)'],1)
        result=_path_attribution([], [trade], [])
        self.assertEqual(result['CB-only/UNEXPLAINED_CHECK_INTEGRATION'],1)

    def test_storage_corruption_fails_instead_of_resetting_equity(self):
        from src.monitor.circuit_breaker_shadow import _atomic_json
        with TemporaryDirectory() as directory:
            root=Path(directory); config=_config(); logger=JsonlLogger(root,config)
            cb=CircuitBreakerShadow(root,config,logger,None)
            cb.on_tick(100,'2026-09-05T00:00:00+00:00')
            data=json.loads(cb.state_path.read_text())
            data['clock']['equity']=95
            _atomic_json(cb.state_path,json.dumps(data))
            with self.assertRaisesRegex(ValueError,'reconciliation'):
                CircuitBreakerShadow(root,config,logger,None)

    def test_real_admission_observation_cannot_affect_detector(self):
        with TemporaryDirectory() as directory:
            root=Path(directory); config=_config(); logger=JsonlLogger(root,config)
            cb=CircuitBreakerShadow(root,config,logger,None)
            at=datetime(2026,9,5,tzinfo=timezone.utc)
            signal=EntrySignal('SOLUSDT',100,at.isoformat(),int(at.timestamp()*1000)-60_000,.1,'1m',14)
            cb.on_signal(signal)
            before=cb.clock.to_state()
            cb.record_real_admission(signal,'blocked')
            self.assertEqual(before,cb.clock.to_state())
            self.assertEqual(len(cb.open_positions),1)


if __name__=='__main__': unittest.main()
