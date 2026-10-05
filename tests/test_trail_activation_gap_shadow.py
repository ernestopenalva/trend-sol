import io
import json
import unittest
from copy import deepcopy
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.trail_activation_gap_shadow import TrailActivationGapShadow, owner
from src.monitor.forward_experiment_shadows import ExperimentalRiskShadow
from src.monitor.entry_engine import EntrySignal
from tests.test_circuit_breaker_shadow import _config
from tests.test_exit_context_telemetry import snapshot
from tools import forward_experiment_report as report

START=datetime(2026,10,5,3,tzinfo=timezone.utc)

def config():
    cfg=_config();cfg['instrumentation']['be_off_cb_shadow']={**cfg['instrumentation']['circuit_breaker_shadow'],
        'state_file':'data/state/be_off_cb_shadow.json','ledger_file':'data/trades/trades_be_off_cb_shadow.jsonl','events_file':'data/telemetry/be_off_cb_shadow_events.jsonl'}
    cfg['risk']['profit_lock']['steps']=[{'trigger_atr':5,'lock_atr':1.5},{'trigger_atr':8,'lock_atr':3},{'trigger_atr':12,'lock_atr':6}]
    for key,act,gap in (('be_off_cb_act20_gap5_shadow',20,5),('be_off_cb_act10_gap13_shadow',10,13)):
        cfg['instrumentation'][key]={'enabled':True,'trail_activation_atr':act,'trail_gap_atr':gap,
            'state_file':f'data/state/{key}.json','ledger_file':f'data/trades/trades_{key}.jsonl','events_file':f'data/telemetry/{key}_events.jsonl'}
    return cfg

def make(root,cfg,act):
    arm=report.ACT20_GAP5 if act==20 else report.ACT10_GAP13;key='be_off_cb_act20_gap5_shadow' if act==20 else 'be_off_cb_act10_gap13_shadow'
    return TrailActivationGapShadow(root,cfg,JsonlLogger(root,cfg),None,settings_key=key,strategy=arm.name,pair_prefix=key,cohort_started_at=START.isoformat())

def signal(at=START,price=100):
    return EntrySignal('SOLUSDT',price,at.isoformat(),int(at.timestamp()*1000)-60000,1,'1m',14)

class TrailShadowTests(unittest.TestCase):
    def test_act20_threshold_gap_ratchet_and_pl(self):
        with TemporaryDirectory() as tmp:
            s=make(Path(tmp),config(),20);s.on_signal(signal());p=s.positions[0]
            for i,price in enumerate((112,119.99,120,123,122),1):
                s.on_tick(price,(START+timedelta(seconds=i)).isoformat())
                if i<3:self.assertFalse(p.trailing_active);self.assertEqual(owner(p),'PL3')
                if i==3:self.assertTrue(p.trailing_active);self.assertEqual(p.trailing_stop,115)
            self.assertEqual(p.trailing_stop,118);self.assertEqual(p.entry_atr,1)
            self.assertIsNotNone(p.trail_audit['first_dominance_time'])

    def test_gap13_activated_without_dominance_then_dominates(self):
        with TemporaryDirectory() as tmp:
            s=make(Path(tmp),config(),10);s.on_signal(signal());p=s.positions[0]
            for i,price in enumerate((109.99,110,112,119,120,121,120),1):
                s.on_tick(price,(START+timedelta(seconds=i)).isoformat())
                if i==1:self.assertFalse(p.trailing_active)
                if i==2:self.assertTrue(p.trailing_active);self.assertEqual(p.trailing_stop,97);self.assertEqual(owner(p),'PL2')
                if i==4:self.assertEqual(owner(p),'PL3');self.assertIsNone(p.trail_audit['first_dominance_time'])
                if i==5:self.assertEqual(owner(p),'TRAIL');self.assertEqual(p.trailing_stop,107)
            self.assertEqual(p.trailing_stop,108);self.assertEqual(p.trail_audit['peak_at_first_dominance'],120)

    def test_context_without_opportunities_restart_and_causal_exit(self):
        for act in (20,10):
            with self.subTest(act=act),TemporaryDirectory() as tmp:
                root=Path(tmp);cfg=config();s=make(root,cfg,act)
                entry=snapshot(START-timedelta(milliseconds=1),'BUL','BU+')
                s.on_approved_real_a_signal(signal(),entry);p=s.positions[0];original=p.to_state()
                for i in range(1,6):s.on_closed_5m(snapshot(START+timedelta(minutes=5*i,milliseconds=-1),'BEA','BE-'))
                latest=s.latest_market_context
                self.assertEqual(p.to_state(),original)
                s.on_tick(122,(START+timedelta(minutes=25,seconds=1)).isoformat())
                audit=deepcopy(p.trail_audit)
                s.on_closed_5m(snapshot(START+timedelta(minutes=30,milliseconds=-1),'LON','BU+'))
                restored=make(root,cfg,act);rp=restored.positions[0]
                self.assertEqual(rp.trail_audit,audit);self.assertEqual(rp.highest_price,122)
                stop=rp.effective_stop
                restored.on_tick(stop,(START+timedelta(minutes=25,seconds=2)).isoformat())
                r=restored.closed_records[0]
                self.assertEqual(r['market_context_entry'],entry);self.assertEqual(r['market_context_exit'],latest)
                self.assertEqual(report._recorded_exit_context(r)['ema_context'],'BEA')
                self.assertEqual(r['arm'],restored.strategy);self.assertTrue(r['trail_activated'])
                self.assertIsNotNone(r['first_trail_dominance_time']);self.assertIn('fees',r)
                self.assertGreaterEqual(sum(r['stop_owner_seconds'].values()),25*60)

    def test_control_and_arms_independent_capacity_cb_ledger_and_signal(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=config();original=deepcopy(cfg)
            c=CircuitBreakerShadow(root,cfg,JsonlLogger(root,cfg),None,settings_key='be_off_cb_shadow',strategy='BE_OFF_CB_SHADOW',shadow_kind='BE_OFF_CB_SHADOW',pair_prefix='control',be_off=True)
            a=make(root,cfg,20);b=make(root,cfg,10)
            for s in (c,a,b):self.assertTrue(s.on_signal(signal()))
            self.assertEqual(cfg,original);self.assertEqual(c._exit_config()['trailing']['activation_atr'],10)
            self.assertEqual(c._exit_config()['trailing']['gap_atr'],5)
            for s in (a,b):
                exp=deepcopy(s._exit_config());exp['trailing']=deepcopy(c._exit_config()['trailing'])
                self.assertEqual(exp,c._exit_config())
                self.assertEqual(s.positions[0].source_candle_open_time,signal().source_candle_open_time)
            for s in (c,a,b):s.on_tick(111,(START+timedelta(seconds=1)).isoformat())
            for s in (c,a,b):s.on_tick(104,(START+timedelta(seconds=2)).isoformat())
            self.assertEqual(len(c.open_positions),0);self.assertEqual(len(a.open_positions),1);self.assertEqual(len(b.open_positions),1)
            self.assertEqual(len({s.state_path for s in (c,a,b)}),3)
            self.assertIsNot(a.clock,b.clock);self.assertIsNot(a.entries_by_bucket,b.entries_by_bucket)
            a.settings['max_open_positions']=1
            next_signal=signal(START+timedelta(minutes=5),price=108)
            self.assertFalse(a.on_signal(next_signal));self.assertTrue(b.on_signal(next_signal))
            self.assertEqual(a.audit_events[-1]['event'],'ENTRY_BLOCKED_SHADOW_CAPACITY')
            self.assertNotEqual(a.ledger.path,b.ledger.path)

    def test_report_general_specific_since_and_no_extra_common_columns(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=config();s=make(root,cfg,20);s.announce_cohort();s.on_signal(signal())
            s.on_tick(121,(START+timedelta(minutes=1)).isoformat());s.on_tick(116,(START+timedelta(minutes=2)).isoformat())
            for args in (['--since','05/10/2026 00:00'],['--experiment','trail_activation_gap','--since','05/10/2026 00:00']):
                output=io.StringIO()
                with patch.object(report,'ROOT',root),patch('sys.argv',['report',*args]),redirect_stdout(output):report.main()
                text=output.getvalue();self.assertIn(report.SUMMARY_HEADER,text)
                self.assertIn(report.ACT20_GAP5.name,text);self.assertIn(report.ACT10_GAP13.name,text)
                if '--experiment' in args:
                    self.assertIn('first trail dominance BRT',text);self.assertIn('TRAIL dominated | 1',text)
            output=io.StringIO()
            with patch.object(report,'ROOT',root),patch('sys.argv',['report','--experiment','trail_activation_gap','--since','05/10/2026 00:03']),redirect_stdout(output):report.main()
            self.assertIn('total trades | 0',output.getvalue())

    def test_gap_without_candles_remains_honestly_stale(self):
        with TemporaryDirectory() as tmp:
            s=make(Path(tmp),config(),20);s.on_closed_5m(snapshot(START-timedelta(milliseconds=1)))
            s.on_signal(signal());s.on_tick(98,(START+timedelta(minutes=11)).isoformat())
            self.assertEqual(report._recorded_exit_context(s.closed_records[0])['ema_context'],'STALE')

    def test_risk_override_retains_causal_exit_history_not_only_latest(self):
        for experiment in ('HS_BULL_ELASTIC','HS_BEAR_CLUSTER_EXIT','CB_EXIT_ALL'):
            with self.subTest(experiment=experiment),TemporaryDirectory() as tmp:
                root=Path(tmp);cfg=config()
                s=ExperimentalRiskShadow(root,cfg,JsonlLogger(root,cfg),None,settings_key='be_off_cb_shadow',strategy=experiment,pair_prefix='risk',experiment=experiment,cohort_started_at=START.isoformat())
                prior=snapshot(START-timedelta(milliseconds=1),'BEA','BE-')
                before=(s.clock.to_state(),s.equity,s.sequence)
                s.on_closed_5m(prior);s.on_closed_5m(snapshot(START+timedelta(minutes=5,milliseconds=-1),'LON','BU+'))
                self.assertEqual(s._exit_context_at(START+timedelta(seconds=5)),prior)
                self.assertEqual(before,(s.clock.to_state(),s.equity,s.sequence))

    def test_new_cohort_rejects_comparison_with_older_control_period(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=config();s=make(root,cfg,20);s.announce_cohort()
            with patch.object(report,'ROOT',root),self.assertRaises(SystemExit):
                report.print_trail_activation_gap(START-timedelta(minutes=1),START+timedelta(minutes=5))

    def test_config_validation_only_new_parameters_and_independent_paths(self):
        import yaml
        import subprocess
        from src.config_profiles import effective_config
        raw=yaml.safe_load(Path('config/config.yaml').read_text());resolved=effective_config(raw)
        self.assertEqual(resolved['risk']['trailing']['activation_atr'],10)
        prior=effective_config(yaml.safe_load(subprocess.check_output(['git','show','HEAD:config/config.yaml'],text=True)))
        for section in ('risk','capital','fees','entry','trend','trend_gate','ladder','market_data','position_mode'):
            self.assertEqual(resolved.get(section),prior.get(section),section)
        disabled=deepcopy(config());disabled['instrumentation'].pop('be_off_cb_act20_gap5_shadow')
        with TemporaryDirectory() as tmp:
            self.assertFalse(make(Path(tmp),disabled,20).enabled)
        bad=deepcopy(raw);bad['instrumentation']['be_off_cb_act20_gap5_shadow']['trail_gap_atr']=13
        with self.assertRaises(ValueError):effective_config(bad)
        bad=deepcopy(raw);bad['instrumentation']['be_off_cb_act20_gap5_shadow']['state_file']=bad['instrumentation']['be_off_cb_shadow']['state_file']
        with self.assertRaises(ValueError):effective_config(bad)

if __name__=='__main__':unittest.main()
