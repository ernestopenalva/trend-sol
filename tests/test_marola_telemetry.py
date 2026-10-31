import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing, ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

from src.monitor.price_structure import (MAROLAS, MAROLA_60, MAROLA_90, MAROLA_KEYS,
    Hour, StructureBuffer, LiveStructure, WAVE, classify, capture_live, install_live,
    marola_fields, iso, restore_fields, HOUR)
from tools.marola_telemetry import annotate, migrate, prepare, compare, verify_migration
from tools.trend_structure_migrate import rollback, verify, economic

Q=300_000


def candles(n=40):
    return [Hour(i*Q,110+i*.1,100+i*.1,True,(i+1)*Q) for i in range(n)]


def provider():
    live=LiveStructure('SOLUSDT',0)
    live.marola_buffers={r.prefix:StructureBuffer(candles(1000),rule=r) for r in MAROLAS}
    return live


class MarolaTests(unittest.TestCase):
    def tearDown(self):install_live(None)

    def test_same_geometry_as_frozen_study_and_causal_window(self):
        cs=candles()
        for rule in MAROLAS:
            study=replace(WAVE,interval_ms=Q,count=rule.count)
            for at in (18*Q,18*Q+1234,19*Q-1,19*Q):
                got=classify(cs,at,'ao_vivo',rule)
                expected=classify(cs,at,'IDEALIZED_PUBLIC',study)
                for key in ('label','highs','lows','highs_class','lows_class','window_end_ms'):
                    self.assertEqual(got[key],expected[key])
                self.assertTrue(all(x['closed_ms']<at for x in got['highs']+got['lows']))
        self.assertEqual(MAROLA_60.version,'TREND_60M_2X30M_5M_V1')
        self.assertEqual(MAROLA_90.version,'TREND_90M_2X45M_5M_V1')

    def test_gap_late_delivery_open_candle_and_duplicate(self):
        rule=MAROLA_90;b=StructureBuffer(candles(17),rule=rule);at=18*Q
        self.assertEqual(b.snapshot(at,'ao_vivo')['label'],'UNAVAILABLE')
        k=[17*Q,0,120,110,0,0,18*Q-1]
        b.add_kline(k,at+2000)
        self.assertEqual(b.snapshot(at+1999,'ao_vivo')['label'],'UNAVAILABLE')
        self.assertEqual(b.snapshot(at+2000,'ao_vivo')['label'],'BULL')
        b.add_kline(k,at+3000)
        self.assertEqual(b.hours[17*Q].available_ms,at+2000)
        self.assertFalse(b.add_kline([18*Q,0,999,1,0,0,19*Q-1],at+5000))

    def test_entry_persistence_all_shared_position_types_restart_and_no_exit_rewrite(self):
        from tests.test_price_structure_telemetry import position,Logger
        from src.position.bot_full_engine import BotFullExitPosition
        install_live(provider());p=position();before=p.price_structure.copy()
        for rule in MAROLAS:
            self.assertEqual(before[rule.prefix+'_open_source'],'ao_vivo')
            self.assertEqual(before[rule.prefix+'_open'],'BULL')
        p.mark_closed(98.5,'HARD_STOP',iso(80*HOUR),{})
        self.assertEqual({k:v for k,v in p.price_structure.items() if k in MAROLA_KEYS},
                         {k:v for k,v in before.items() if k in MAROLA_KEYS})
        restored=BotFullExitPosition.from_state(p.to_state(),p.config,None,Logger())
        self.assertEqual(restore_fields(restored.to_state()),restore_fields(p.to_state()))
        old=provider();old.started_ms=81*HOUR;install_live(old)
        self.assertEqual(position().price_structure,{})

    def test_telemetry_failure_isolated_and_other_symbol_never_uses_SOL(self):
        live=provider();install_live(live)
        with patch.object(live.marola_buffers['marola_60'],'snapshot',side_effect=ValueError('bad')):
            value=capture_live('SOLUSDT',iso(18*Q),'open')
        self.assertEqual(value['marola_60_open'],'UNAVAILABLE')
        self.assertEqual(value['marola_90_open'],'BULL')
        self.assertEqual(capture_live('ETHUSDT',iso(18*Q),'open')['marola_90_open'],'UNAVAILABLE')

    def test_ws_open_5m_ignored_and_telemetry_only_stream_never_enters_engine(self):
        from src.app import Monitor
        m=object.__new__(Monitor);m.config={'symbol':'SOLUSDT'};m.price_structure=LiveStructure('SOLUSDT',0)
        m.logger=Mock();m.entry_engine=Mock();m.registry=Mock();m.market_shadow=Mock();m._marola_only_stream=True
        k=dict(t=17*Q,T=18*Q-1,o='100',h='120',l='110',c='115',v='1',x=False)
        with patch.object(m,'_server_now_ms',return_value=18*Q+1000):
            m._on_ws_event('solusdt@kline_5m',{'k':k})
            self.assertEqual(len(m.price_structure.marola_buffers['marola_90'].hours),0)
            k['x']=True;m._on_ws_event('solusdt@kline_5m',{'k':k})
        self.assertEqual(m.price_structure.marola_buffers['marola_90'].hours[17*Q].available_ms,18*Q+1000)
        m.entry_engine.on_kline.assert_not_called();m.registry.on_tick.assert_not_called()
        m.market_shadow.on_ws_event.assert_not_called()

    def test_bootstrap_reuses_signal_candles_and_emits_exact_marker(self):
        from src.app import Monitor
        m=object.__new__(Monitor)
        m.config={'symbol':'SOLUSDT','market_data':{},'run_id':'test'}
        m.price_structure=LiveStructure('SOLUSDT',0);m.logger=Mock()
        m.entry_engine=Mock();m.entry_engine.required_timeframes.return_value=['5m']
        m.dmi15_trajectory_context_shadow=Mock();m.slow_ge_context_shadow=Mock()
        bars=[[c.open_ms,0,c.high,c.low,0,0,c.open_ms+Q-1] for c in candles(18)]
        m.market_data_client=Mock()
        m.market_data_client.klines.side_effect=lambda **kw:bars if kw['interval']=='5m' else []
        with patch.object(m,'_server_now_ms',return_value=18*Q+123):m._load_historical_candles()
        calls=m.market_data_client.klines.call_args_list
        self.assertEqual(sum(c.kwargs['interval']=='5m' for c in calls),1)
        m.entry_engine.load_history.assert_called_once_with('5m',bars,now_ms=18*Q+123)
        marker=next(c.kwargs for c in m.logger.system.call_args_list if c.args[0]=='marola_live_enabled')
        self.assertEqual(marker['enabled_at'],iso(18*Q+123))
        self.assertEqual(marker['source'],'ao_vivo')
        self.assertEqual(marker['versions'],{r.prefix:r.version for r in MAROLAS})
        self.assertEqual(marker['warmup_candles'],{'marola_60':18,'marola_90':18})
        self.assertEqual(len(marker['classification_code_sha256']),64)

    def test_no_economic_or_exit_change_with_labels_populated(self):
        from tests.test_price_structure_telemetry import position
        def run(enabled):
            install_live(provider() if enabled else None);p=position();events=[]
            for i,price in enumerate((100.5,101,101.8,101,98.4)):
                p.on_tick(price,iso(73*HOUR+i*60_000));events.append((p.status,p.effective_stop,p.exit_reason,p.reserved_qty))
            return events,economic(p.to_state(),set(k for k in p.to_state() if k.startswith(('trend_','wave_','marola_'))))
        self.assertEqual(run(False),run(True))

    def test_cb_shadow_open_history_restore_and_ledger_reprojection(self):
        from tests.test_circuit_breaker_shadow import _config
        from src.logging_utils import JsonlLogger
        from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
        from src.monitor.entry_engine import EntrySignal
        at=1780282800000
        live=LiveStructure('SOLUSDT',at-1)
        cs=[Hour(t,110,90,True,t+Q) for t in range(at-20*Q,at,Q)]
        live.marola_buffers={r.prefix:StructureBuffer(cs,rule=r) for r in MAROLAS};install_live(live)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as cleanup:
            root=Path(tmp);config=_config();logger=JsonlLogger(root,config)
            shadow=CircuitBreakerShadow(root,config,logger,None)
            cleanup.callback(shadow._history_store.db.close)
            self.assertTrue(shadow.on_signal(EntrySignal('SOLUSDT',100,iso(at),at-Q,.2,'1m',14)))
            before=shadow.positions[0].to_state()
            self.assertEqual(before['marola_90_open_source'],'ao_vivo')
            restored=CircuitBreakerShadow(root,config,logger,None)
            cleanup.callback(restored._history_store.db.close)
            after=restored.positions[0].to_state()
            # Existing restore normalizes this descriptive NPE source, even
            # without MAROLA. Compare every other field, including economics.
            self.assertEqual((before['no_progress_tolerance_source'],after['no_progress_tolerance_source']),
                             ('DISABLED','DISABLED_BY_CONFIG'))
            self.assertEqual({k:v for k,v in before.items() if k!='no_progress_tolerance_source'},
                             {k:v for k,v in after.items() if k!='no_progress_tolerance_source'})
            restored.on_tick(98,iso(at+60_000))
            rows=[json.loads(l) for l in restored.ledger.path.read_text().splitlines()]
            self.assertEqual(rows[0]['marola_60_open_source'],'ao_vivo')
            self.assertEqual(rows[0]['exit_reason'],'HARD_STOP')

    def fixture(self,tmp):
        root=Path(tmp)/'bot';(root/'data/trades').mkdir(parents=True);(root/'data/state').mkdir()
        row=dict(pair_id='p',symbol='SOLUSDT',opened_at=iso(18*Q),entry_price=100,net=-.3,trend_open='BEAR',wave_open='COMP')
        ledger=root/'data/trades/test.jsonl';ledger.write_text(json.dumps(row)+'\n')
        state=root/'data/state/live.json';state.write_text(json.dumps({'positions':[row],'clock':{'equity':99.7}}))
        cache=Path(tmp)/'candles';cache.mkdir()
        (cache/'SOLUSDT_5m.jsonl').write_text(''.join(json.dumps(dict(open_time_ms=c.open_ms,close_time_ms=c.open_ms+Q-1,high=c.high,low=c.low))+'\n' for c in candles()))
        return root,ledger,state,cache,row

    def test_migration_preserves_all_other_fields_states_and_sqlite_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,ledger,state,cache,row=self.fixture(tmp);state_bytes=state.read_bytes()
            dbfile=root/'data/state/test.history.sqlite'
            with closing(sqlite3.connect(dbfile)) as db:
                db.executescript('CREATE TABLE history(kind TEXT,ordinal INTEGER,revision INTEGER,payload TEXT);CREATE TABLE pending(singleton INTEGER,payload TEXT);CREATE TABLE metadata(key TEXT,value TEXT);')
                db.execute('INSERT INTO history VALUES(?,?,?,?)',('ledger',0,1,json.dumps(row)))
                db.execute('INSERT INTO history VALUES(?,?,?,?)',('events',0,1,'{"event":"AUDIT"}'))
                db.execute('INSERT INTO metadata VALUES(?,?)',('owner','unchanged'))
                db.commit()
            backup=Path(tmp)/'backup';manifest=migrate(root,cache,backup,stopped=True)
            self.assertEqual(verify(root,backup)['status'],'VERIFIED')
            value=json.loads(ledger.read_text());self.assertEqual(economic(value,MAROLA_KEYS),row)
            self.assertEqual(value['marola_90_open_source'],'reclassificado');self.assertEqual(state.read_bytes(),state_bytes)
            with closing(sqlite3.connect(dbfile)) as db:
                self.assertEqual(json.loads(db.execute("SELECT payload FROM history WHERE kind='ledger'").fetchone()[0]),value)
                self.assertEqual(db.execute("SELECT payload FROM history WHERE kind='events'").fetchone()[0],'{"event":"AUDIT"}')
            rollback(root,backup);self.assertEqual(json.loads(ledger.read_text()),row)

    def test_failure_rolls_back_and_live_provenance_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,ledger,state,cache,row=self.fixture(tmp);before=ledger.read_bytes()
            with self.assertRaises(RuntimeError):migrate(root,cache,Path(tmp)/'backup',stopped=True,fail_after=1)
            self.assertEqual(ledger.read_bytes(),before)
            with self.assertRaises(ValueError):migrate(root,cache,Path(tmp)/'next')
            with self.assertRaises(FileNotFoundError):migrate(root,Path(tmp)/'missing',Path(tmp)/'no-data',stopped=True)
            self.assertEqual(ledger.read_bytes(),before)
            live=provider();native={}
            for rule in MAROLAS:native.update(marola_fields(live.marola_buffers[rule.prefix].snapshot(18*Q,'ao_vivo'),rule))
            self.assertEqual(annotate({**row,**native},{'SOLUSDT':live.marola_buffers}),{**row,**native})

    def test_live_offline_comparison_reports_every_difference(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,ledger,state,cache,row=self.fixture(tmp);live=provider()
            for rule in MAROLAS:row.update(marola_fields(live.marola_buffers[rule.prefix].snapshot(18*Q,'ao_vivo'),rule))
            ledger.write_text(json.dumps(row)+'\n')
            state.write_text(json.dumps({'positions':[row]}))
            self.assertEqual(compare(root,cache,iso(0))['match_pct'],100)
            row['marola_90_open']='BEAR';ledger.write_text(json.dumps(row)+'\n')
            result=compare(root,cache,iso(0));self.assertFalse(result['passed']);self.assertEqual(len(result['differences']),1)
            ledger.write_text(json.dumps(economic(row,MAROLA_KEYS))+'\n')
            result=compare(root,cache,iso(0));self.assertFalse(result['passed']);self.assertEqual(len(result['differences']),2)

    def test_prepare_only_missing_slots_and_cannot_touch_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,ledger,state,cache,row=self.fixture(tmp);before=ledger.read_bytes();client=Mock()
            result=prepare(root,cache,client=client)
            client.get.assert_not_called();self.assertEqual(result['symbols'],1);self.assertEqual(ledger.read_bytes(),before)

    def test_verify_rejects_advanced_checkpoint_even_without_new_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,ledger,state,cache,row=self.fixture(tmp);backup=Path(tmp)/'backup'
            migrate(root,cache,backup,stopped=True)
            self.assertTrue(verify_migration(root,backup)['checkpoints_pending_unchanged'])
            state.write_text(json.dumps({'positions':[row],'clock':{'equity':99.8}}))
            with self.assertRaises(ValueError):verify_migration(root,backup)

    def test_seed_requires_matching_audited_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            root,ledger,state,cache,row=self.fixture(tmp)
            market=Path(tmp)/'study/market';market.mkdir(parents=True)
            seed=market/'5m.jsonl';seed.write_bytes((cache/'SOLUSDT_5m.jsonl').read_bytes())
            (market.parent/'market_manifest.json').write_text(json.dumps({'granularities':{'5m':{'sha256':'0'*64}}}))
            client=Mock()
            with self.assertRaises(ValueError):prepare(root,cache,client=client,seed=seed)
            client.get.assert_not_called()


if __name__=='__main__':unittest.main()
