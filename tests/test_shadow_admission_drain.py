"""Admission retirement keeps restored positions/exits alive."""
import json
import unittest
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock

from src.app import Monitor
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.forward_experiment_shadows import ExperimentalRiskShadow
from src.monitor.entry_engine import EntrySignal
from src.logging_utils import JsonlLogger
from tests.test_context_shadows import _config

class AdmissionDrainTests(unittest.TestCase):
    def test_dmi_restart_blocks_admissions_and_closes_existing_position(self):
        with TemporaryDirectory() as tmp:
            root=Path(tmp);config=_config();logger=JsonlLogger(root,config)
            def build():
                return RealAContextShadow(root,config,logger,None,
                    settings_key='dmi15_trajectory_context_shadow',strategy='DMI15_TRAJECTORY_CONTEXT_SHADOW',
                    shadow_kind='DMI15_TRAJECTORY_CONTEXT_SHADOW',pair_prefix='drain',predicate=lambda *_:True)
            shadow=build()
            self.assertTrue(shadow.on_signal(EntrySignal('SOLUSDT',100,'2026-10-31T03:00:00Z',1793415540000,.2,'1m',14)))
            pair=shadow.open_positions[0].pair_id
            config['instrumentation']['dmi15_trajectory_context_shadow']['accept_new_entries']=False
            restored=build()
            self.assertEqual(restored.open_positions[0].pair_id,pair)
            self.assertFalse(restored.on_signal(EntrySignal('SOLUSDT',101,'2026-10-31T03:05:00Z',1793415840000,.2,'1m',14)))
            restored.on_tick(98,'2026-10-31T03:06:00Z')
            self.assertFalse(restored.open_positions)
            self.assertEqual(len(restored.ledger.load()),1)
            self.assertEqual(restored.ledger.load()[0]['exit_reason'],'HARD_STOP')

    def test_elastic_restart_keeps_elastic_exit_while_admissions_are_stopped(self):
        from tests.test_forward_experiment_shadows import RiskShadowTests
        with TemporaryDirectory() as tmp,ExitStack() as cleanup:
            root=Path(tmp);shadow=RiskShadowTests()._shadow(root,'HS_BULL_ELASTIC')
            cleanup.callback(shadow._history_store.db.close)
            lon={'tf_5m':{'ema_context':'LON','close':100.,'latest_closed_at_ms':1790539199999}}
            shadow.on_approved_real_a_signal(RiskShadowTests._signal(100,1790538900000,1),lon)
            pair=shadow.open_positions[0].pair_id
            config=deepcopy(shadow.config);config['instrumentation']['risk_experiment']['accept_new_entries']=False
            restored=ExperimentalRiskShadow(root,config,shadow.logger,None,
                settings_key='risk_experiment',strategy='HS_BULL_ELASTIC',pair_prefix='risk',
                experiment='HS_BULL_ELASTIC',cohort_started_at='2026-09-27T20:00:00+00:00')
            cleanup.callback(restored._history_store.db.close)
            self.assertEqual(restored.open_positions[0].pair_id,pair)
            self.assertFalse(restored.on_approved_real_a_signal(RiskShadowTests._signal(101,1790539200000,6),lon))
            self.assertEqual(len(restored.open_positions),1)
            restored.on_tick(98.5,'2026-09-27T20:07:00Z')
            self.assertTrue(restored.open_positions[0].hs_elastic)
            restored.on_closed_5m({'tf_5m':{'ema_context':'BEA','close':98.4,'latest_closed_at_ms':1790539499999}})
            self.assertFalse(restored.open_positions)
            self.assertEqual(restored.closed_records[0]['exit_reason'],'HARD_STOP_ELASTIC_CONTEXT_LOST')

    def test_boot_marker_is_exact_and_only_for_requested_admission_holds(self):
        monitor=Monitor.__new__(Monitor)
        def arm(name,accept=False):return SimpleNamespace(strategy=name,enabled=True,accept_new_entries=accept,open_positions=[object()])
        monitor.dmi15_trajectory_context_shadow=arm('DMI15_TRAJECTORY_CONTEXT_SHADOW')
        monitor.forward_experiment_shadows=[arm('HS_BULL_ELASTIC_SHADOW'),arm('UNRELATED')]
        monitor.config={'run_id':'test'};monitor.logger=Mock();monitor._server_now_ms=lambda:1793416849955
        monitor._log_shadow_admission_holds()
        calls=monitor.logger.system.call_args_list
        self.assertEqual(len(calls),2)
        self.assertEqual({c.kwargs['arm'] for c in calls},{'DMI15_TRAJECTORY_CONTEXT_SHADOW','HS_BULL_ELASTIC_SHADOW'})
        for call in calls:
            self.assertEqual(call.kwargs['stopped_at'],'2026-10-31T03:20:49.955+00:00')
            self.assertEqual(call.kwargs['open_positions'],1)
            self.assertTrue(call.kwargs['existing_positions_continue'])

if __name__=='__main__':unittest.main()
