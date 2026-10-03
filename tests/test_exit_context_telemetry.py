import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from copy import deepcopy
from unittest.mock import MagicMock

from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal
from tests.test_circuit_breaker_shadow import _config
from tools.forward_experiment_report import _recorded_exit_context


def snapshot(close, ema='BEA', macd='BE-'):
    return {'captured_at':(close+timedelta(milliseconds=1)).isoformat(),
            'tf_5m':{'latest_closed_at_ms':int(close.timestamp()*1000),
                     'ema_context':ema,'macd_context':macd}}


class ExitTelemetryTests(unittest.TestCase):
    def test_app_routes_closed_5m_to_all_separate_controls(self):
        from src.app import Monitor
        m=MagicMock()
        m.config={'symbol':'SOLUSDT'}
        m.market_shadow_ge30=None
        m._entry_should_pause.return_value=False
        m.entry_engine.on_kline.return_value=None
        value=snapshot(datetime(2026,10,1,7,24,59,999000,tzinfo=timezone.utc))
        m._safe_refresh_market_context.return_value=value
        experiment=MagicMock()
        m.forward_experiment_shadows=[experiment]
        Monitor._on_ws_event(m,'solusdt@kline_5m',{'k':{'x':False}})
        m.registry.record_market_context.assert_not_called()
        for s in (m.circuit_breaker_shadow,m.be_off_cb_shadow,m.be030_shadow,m.be_off_shadow,experiment):
            s.on_closed_5m.assert_not_called()
        Monitor._on_ws_event(m,'solusdt@kline_5m',{'k':{'x':True}})
        m.registry.record_market_context.assert_called_once_with(value)
        for s in (m.circuit_breaker_shadow,m.be_off_cb_shadow,m.be030_shadow,m.be_off_shadow,experiment):
            s.on_closed_5m.assert_called_once_with(value)
        m.registry.open_pair.assert_not_called()

    def test_real_a_refreshes_context_without_positions_or_admissions(self):
        from src.monitor.position_registry import PositionRegistry
        registry=PositionRegistry.__new__(PositionRegistry)
        registry.positions=[]
        first=snapshot(datetime(2026,10,1,7,24,59,999000,tzinfo=timezone.utc),'BUL','BU-')
        second=snapshot(datetime(2026,10,1,7,29,59,999000,tzinfo=timezone.utc))
        registry.record_market_context(first)
        registry.record_market_context(second)
        self.assertEqual(registry._latest_market_context,second)
        second['tf_5m']['ema_context']='MIX'
        self.assertEqual(registry._latest_market_context['tf_5m']['ema_context'],'BEA')
        registry.record_market_context(None)
        self.assertEqual(registry._latest_market_context['tf_5m']['ema_context'],'BEA')

    def test_retro_audit_flags_oct01_stale_snapshot_without_reconstruction(self):
        from tools.exit_context_telemetry_audit import audit
        old=snapshot(datetime(2026,10,1,6,19,59,999000,tzinfo=timezone.utc),'BUL','BU-')
        rows=[{'closed_at':'2026-10-01T07:25:16.617+00:00','exit_reason':'HARD_STOP',
               'market_context_exit':old}]
        original=deepcopy(rows)
        result=audit(rows)
        self.assertEqual(result[0]['status'],'STALE')
        self.assertAlmostEqual(result[0]['snapshot_age_min'],65.2769666667)
        self.assertEqual(rows,original)

    def test_hours_without_signals_latest_closed_snapshot_and_restart(self):
        for seconds in (0.001,16.617,180):
            with self.subTest(seconds=seconds),TemporaryDirectory() as tmp:
                root=Path(tmp);cfg=_config();cfg['risk']['breakeven']={'mode':'off'}
                shadow=CircuitBreakerShadow(root,cfg,JsonlLogger(root,cfg),None)
                entry=datetime(2026,10,1,5,17,tzinfo=timezone.utc)
                old=snapshot(entry-timedelta(minutes=2,milliseconds=1),'BUL','BU-')
                signal=EntrySignal('SOLUSDT',100,entry.isoformat(),int(entry.timestamp()*1000)-60000,.2,'1m',14)
                shadow.on_approved_real_a_signal(signal,old)
                states=deepcopy(shadow.positions[0].to_state())
                boundary=datetime(2026,10,1,7,25,tzinfo=timezone.utc)
                for i in range(25):
                    shadow.on_closed_5m(snapshot(boundary-timedelta(minutes=120-i*5,milliseconds=1)))
                latest=snapshot(boundary-timedelta(milliseconds=1))
                self.assertEqual(states,shadow.positions[0].to_state())
                # Newer callback can precede delivery of an older tick. It must
                # not supply future context to that tick; retain previous snapshot.
                shadow.on_closed_5m(snapshot(boundary+timedelta(minutes=5,milliseconds=-1),'LON','BU+'))
                restored=CircuitBreakerShadow(root,cfg,JsonlLogger(root,cfg),None)
                restored.on_tick(98,(boundary+timedelta(seconds=seconds)).isoformat())
                record=restored.closed_records[0]
                self.assertEqual(record['exit_reason'],'HARD_STOP')
                self.assertEqual(record['market_context_entry'],old)
                self.assertEqual(record['market_context_exit'],latest)
                self.assertEqual(_recorded_exit_context(record)['ema_context'],'BEA')

    def test_open_or_future_snapshot_rejected_and_report_boundary_recency(self):
        boundary=datetime(2026,10,1,7,25,tzinfo=timezone.utc)
        with TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=_config()
            s=CircuitBreakerShadow(root,cfg,JsonlLogger(root,cfg),None)
            valid=snapshot(boundary-timedelta(milliseconds=1))
            s.on_closed_5m(valid)
            bad=snapshot(boundary+timedelta(minutes=5,milliseconds=-1))
            bad['captured_at']=boundary.isoformat()
            s.on_closed_5m(bad)
            self.assertEqual(s.latest_market_context,valid)
            bad['captured_at']=(boundary+timedelta(minutes=5)).isoformat()
            bad['tf_5m']['closed']=False
            s.on_closed_5m(bad)
            self.assertEqual(s.latest_market_context,valid)
        for offset,status in ((0,'BEA'),(299998,'BEA'),(299999,'STALE'),(300000,'STALE')):
            row={'closed_at':(boundary+timedelta(milliseconds=offset)).isoformat(),'market_context_exit':valid}
            self.assertEqual(_recorded_exit_context(row)['ema_context'],status)
        self.assertEqual(_recorded_exit_context({'closed_at':boundary.isoformat()})['ema_context'],'UNAVAILABLE')

    def test_callback_changes_no_financial_state(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=_config();s=CircuitBreakerShadow(root,cfg,JsonlLogger(root,cfg),None)
            before=(s.clock.to_state(),s.equity,s.peak_equity,s.sequence,deepcopy(s.pending_closes))
            s.on_closed_5m(snapshot(datetime(2026,10,1,7,24,59,999000,tzinfo=timezone.utc)))
            after=(s.clock.to_state(),s.equity,s.peak_equity,s.sequence,deepcopy(s.pending_closes))
            self.assertEqual(before,after)

    def test_legacy_state_snapshot_is_retained_before_new_callback(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=_config();s=CircuitBreakerShadow(root,cfg,JsonlLogger(root,cfg),None)
            boundary=datetime(2026,10,1,7,25,tzinfo=timezone.utc)
            prior=snapshot(boundary-timedelta(milliseconds=1))
            s.latest_market_context=deepcopy(prior)
            s._exit_context_history=[]
            s.on_closed_5m(snapshot(boundary+timedelta(minutes=5,milliseconds=-1)))
            self.assertEqual(s._exit_context_at(boundary+timedelta(seconds=16)),prior)


if __name__=='__main__':unittest.main()
