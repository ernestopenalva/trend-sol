import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.monitor.feed_observability import FeedObservability, quantiles
from src.monitor.hs_observability import observe
from src.monitor.ws_manager import WSManager
from tests import test_fast_drop_ema_shadow as fast_fixtures
from tests import test_forward_experiment_shadows as risk_fixtures


class FeedTests(unittest.TestCase):
    def manager(self, callback=None):
        return WSManager('wss://example', ['solusdt@aggTrade'], Mock(), callback or Mock())

    def input(self, manager, exchange=100_000, receive=101_000):
        raw=json.dumps({'stream':'solusdt@aggTrade','data':{'T':exchange,'E':exchange+10,'p':'100'}})
        with patch('src.monitor.ws_manager.time.time',return_value=receive/1000):
            manager._on_message(None,raw)

    def test_normal_inputs_are_only_in_memory_and_payload_unchanged(self):
        m=self.manager()
        for _ in range(100):self.input(m)
        m.logger.system.assert_not_called()
        self.assertEqual(m.observability.inputs,100)
        self.assertEqual(len(m.observability.callbacks),100)
        self.assertEqual(m.on_event.call_args.args,('solusdt@aggTrade',{'T':100000,'E':100010,'p':'100'}))
        self.assertEqual(m.observability.health()['current_lag_ms'],1000)
        m.observability.flush()
        self.assertEqual(m.logger.system.call_count,1)
        payload=m.logger.system.call_args.kwargs
        self.assertEqual(payload['lag_ms'],{'p50':1000,'p90':1000,'p99':1000})
        self.assertEqual(payload['inputs'],100)
        self.assertEqual(m.observability.inputs,0)

    def test_lag_above_five_seconds_is_individual_with_timestamps_duration(self):
        m=self.manager();self.input(m,receive=105000)
        m.logger.system.assert_not_called()
        self.input(m,receive=105001)
        self.assertEqual(m.logger.system.call_args.args,('websocket_input_lag',))
        self.assertEqual(m.logger.system.call_args.kwargs['lag_ms'],5001)
        self.assertIn('callback_ms',m.logger.system.call_args.kwargs)
        self.assertIn('receive_at',m.logger.system.call_args.kwargs)

    def test_callback_exception_is_preserved_and_counted(self):
        m=self.manager(Mock(side_effect=ValueError('economic failure')))
        with self.assertRaisesRegex(ValueError,'economic failure'):self.input(m,receive=106000)
        self.assertEqual(m.observability.callback_errors,1)
        self.assertTrue(m.logger.system.call_args.kwargs['callback_failed'])

    def test_kline_uses_emission_not_future_candle_end(self):
        o=FeedObservability(Mock());r=o.received('solusdt@kline_5m',{'E':100000,'k':{'T':399999}},101000)
        self.assertEqual(r['lag_ms'],1000)
        r=o.received('ack',{},102000)
        self.assertIsNone(r['exchange_ts']);self.assertEqual(o.missing_ts,1)
        r=o.received('solusdt@aggTrade',{'T':float('nan')},103000)
        self.assertIsNone(r['exchange_ts']);self.assertEqual(o.missing_ts,2)

    def test_disconnect_duration_and_first_input_lag(self):
        m=self.manager();self.input(m)
        with patch('src.monitor.ws_manager.time.monotonic',return_value=10):
            m._on_error(None,Exception('ping/pong timed out'));m._on_close(None,None,None)
        with patch('src.monitor.ws_manager.time.monotonic',return_value=12):m._on_open(None)
        self.assertEqual(m._disconnect_duration,2)
        self.input(m,receive=102000)
        call=m.logger.system.call_args
        self.assertEqual(call.args[0],'websocket_reconnect_first_input')
        self.assertEqual(call.kwargs['lag_before_disconnect_ms'],1000)
        self.assertEqual(call.kwargs['lag_after_reconnect_ms'],2000)
        self.assertEqual(call.kwargs['disconnect_duration_seconds'],2)

    def test_backoff_increases_only_for_consecutive_failed_connections(self):
        m=self.manager();attempts=[]
        def run(**kwargs):
            attempts.append(1)
            if len(attempts)==3:self.input(m)
            if len(attempts)==5:m.stop_requested=True
        app=Mock();app.run_forever.side_effect=run
        with patch('src.monitor.ws_manager.WebSocketApp',return_value=app),patch('src.monitor.ws_manager.threading.Thread'),patch('src.monitor.ws_manager.time.sleep') as sleep:
            m._run_connections()
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[1,2,1,2])

    def test_reporting_runs_independently_of_inputs(self):
        o=FeedObservability(Mock())
        o.stop_event=Mock();o.stop_event.wait.side_effect=[False,False,True]
        o._run()
        self.assertEqual(o.logger.system.call_count,2)
        self.assertEqual(o.stop_event.wait.call_args.args,(10,))

    def test_empty_quantiles_and_logger_failure_are_safe(self):
        self.assertEqual(quantiles([]),{'p50':None,'p90':None,'p99':None})
        self.assertEqual(quantiles([0,100])['p50'],50)
        o=FeedObservability(Mock());o.logger.system.side_effect=OSError('disk')
        o.flush();self.assertEqual(o.write_errors,1)


class HSTests(unittest.TestCase):
    def diagnostics(self,root):
        p=root/'logs/decisions.jsonl'
        if not p.exists():return []
        return [r for line in p.read_text().splitlines() if (r:=json.loads(line)).get('event')=='HS_INTELLIGENCE_EVALUATION']

    def test_fast_missing_retry_then_multiple_rejection_consumes_only_once(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);f=fast_fixtures.FastDropTests();s,_=f.make(root);f.open(s)
            s.on_tick(99.5,'2026-10-01T00:06:01+00:00')
            s.on_tick(99.4,'2026-10-01T00:06:02+00:00')
            events=self.diagnostics(root)
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['reasons'],['REFERENCE_MISSING'])
            self.assertFalse(events[0]['values']['one_shot_consumed'])
            self.assertFalse(s.open_positions[0].fast_drop_evaluated)
            f.reference(s,99.6)
            s.on_tick(99.4,'2026-10-01T00:06:03+00:00')
            e=self.diagnostics(root)[-1]
            self.assertEqual(e['reasons'],['SPEED_BLOCKED','EMA_BLOCKED'])
            self.assertTrue(e['values']['one_shot_consumed'])
            s.on_tick(99.3,'2026-10-01T00:06:04+00:00')
            self.assertEqual(len(self.diagnostics(root)),2)
            s.on_tick(98.5,'2026-10-01T00:06:05+00:00')
            self.assertEqual(self.diagnostics(root)[-1]['decision'],'NOT_REEVALUATED')
            self.assertEqual(s.closed_records[-1]['exit_reason'],'HARD_STOP')

    def test_fast_stale_context_is_visible_but_not_new_gate(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);f=fast_fixtures.FastDropTests();s,_=f.make(root);f.open(s);f.reference(s)
            snapshot=fast_fixtures._snapshot('SHO');snapshot['tf_5m']['latest_closed_at_ms']-=300000
            s.latest_market_context=snapshot;s.context_history=[]
            s.on_tick(99.5,'2026-10-01T00:06:01+00:00')
            e=self.diagnostics(root)[0]
            self.assertEqual(e['ema_freshness'],'STALE')
            self.assertEqual(e['decision'],'TRIGGER')
            self.assertEqual(s.closed_records[0]['exit_reason'],'FAST_DROP')
            self.assertFalse(any(x['event']=='HS_INTELLIGENCE_EVALUATION' for x in s.audit_events))

    def test_cluster_logs_why_bul_rejected_own_hs(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);f=risk_fixtures.RiskShadowTests();s=f._shadow(root,'HS_BEAR_CLUSTER_EXIT')
            s.on_approved_real_a_signal(f._signal(100,1790538900000,1),{'tf_5m':{'ema_context':'BUL'}})
            s.on_tick(98.5,'2026-09-27T20:02:00+00:00')
            e=self.diagnostics(root)[-1]
            self.assertEqual(e['intelligence'],'HS_BEAR_CLUSTER')
            self.assertEqual(e['reasons'],['EMA_NOT_SHO'])
            self.assertEqual(s.closed_records[0]['exit_reason'],'HARD_STOP')

    def test_elastic_rechecks_closed_snapshot_without_false_future_flag(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);f=risk_fixtures.RiskShadowTests();s=f._shadow(root,'HS_BULL_ELASTIC')
            close=int(datetime.fromisoformat('2026-09-27T20:00:00+00:00').timestamp()*1000)-1
            s.on_approved_real_a_signal(f._signal(100,1790538900000,1),{'tf_5m':{'ema_context':'LON','close':100,'latest_closed_at_ms':close}})
            s.on_tick(98.5,'2026-09-27T20:02:00+00:00')
            s.on_closed_5m({'tf_5m':{'ema_context':'LON','close':98.4,'latest_closed_at_ms':close+300000}})
            e=self.diagnostics(root)[-1]
            self.assertEqual(e['decision'],'HOLD');self.assertEqual(e['ema_freshness'],'FRESH')
            s.on_closed_5m({'tf_5m':{'ema_context':'BEA','close':98.4,'latest_closed_at_ms':close+600000}})
            self.assertEqual(self.diagnostics(root)[-1]['decision'],'TRIGGER')
            self.assertEqual(s.closed_records[0]['exit_reason'],'HARD_STOP_ELASTIC_CONTEXT_LOST')

    def test_diagnostic_failure_never_raises_or_mutates_position(self):
        owner=SimpleNamespace(shadow_kind='FAST',logger=Mock());owner.logger.decision.side_effect=OSError('disk')
        p=SimpleNamespace(pair_id='p',source_candle_open_time=1,fast_drop_evaluated=False)
        observe(owner,'FAST_DROP',p,'2026-10-01T00:06:01+00:00','NOT_EVALUABLE')
        self.assertEqual(owner._hs_observation_errors,1)
        self.assertFalse(p.fast_drop_evaluated)

    def test_cb_detector_observes_real_clock_predicates_and_is_sparse(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);f=risk_fixtures.RiskShadowTests();s=f._shadow(root,'CB_EXIT_ALL')
            s.on_tick(100,'2026-09-27T20:01:00+00:00')
            s.on_tick(100,'2026-09-27T20:02:00+00:00')
            self.assertEqual(self.diagnostics(root),[])
            # Inject realized input only as a deterministic fixture for detector.
            s.pending_closes=[{'boundary':1790539380000,'net':-1,'pair_id':'a'},
                              {'boundary':1790539380000,'net':-1,'pair_id':'b'}]
            s.on_tick(100,'2026-09-27T20:03:00+00:00')
            events=self.diagnostics(root)
            detector=next(e for e in events if e['intelligence']=='CB_EXIT_ALL_DETECTOR')
            self.assertEqual(detector['decision'],'CRISIS_TRANSITION')
            self.assertEqual(detector['values']['realized_dd_pct'],2)
            self.assertEqual(detector['values']['closes_4h'],2)
            self.assertTrue(s.circuit_breaker_active)
            self.assertEqual(events[-1]['reasons'],['NO_OPEN_POSITIONS'])


if __name__=='__main__':unittest.main()
