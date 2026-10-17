import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from src.monitor.price_structure import (HOUR,Hour,WAVE,WAVE_VERSION,VERSION,StructureBuffer,
    LiveStructure,classify,fields,install_live,iso,restore_fields)
from tools.trend_structure_migrate import migrate,verify,rollback,economic,WAVE_KEYS,transform

Q=WAVE.interval_ms


def candles(hi=120,lo=105):
    return [Hour(i*Q,115 if i<24 else hi,95 if i<24 else lo) for i in range(60)]


class WaveTests(unittest.TestCase):
    def tearDown(self):install_live(None)

    def test_four_states_ties_missing(self):
        for hi,lo,label in [(120,105,'BULL'),(112,90,'BEAR'),(120,90,'EXPA'),(112,98,'COMP'),(115,98,'UNDEFINED')]:
            s=classify(candles(hi,lo),48*Q,'REPLAY',WAVE)
            self.assertEqual(s['label'],label)
        s=classify([],48*Q,'LIVE',WAVE)
        self.assertEqual((s['label'],s['undefined_reason']),('UNAVAILABLE','INSUFFICIENT_HISTORY'))

    def test_closed_only_non_aligned_and_boundary(self):
        b=StructureBuffer(candles(),rule=WAVE)
        for at,expected in [(48*Q,47*Q),(48*Q+1234,47*Q),(49*Q-1,47*Q),(49*Q,48*Q)]:
            s=b.snapshot(at,'REPLAY');self.assertEqual(s['latest_closed_open_ms'],expected)
            self.assertEqual(s['window_end_ms'],at//Q*Q)
        bad=candles()[:48];bad[-1]=Hour(47*Q,999,1,False)
        self.assertEqual(classify(bad,48*Q,'LIVE',WAVE)['label'],'UNAVAILABLE')

    def test_oldest_tie_and_conflict_scale(self):
        s=classify(candles(),48*Q,'LIVE',WAVE)
        self.assertEqual([h['open_ms'] for h in s['highs']],[0,24*Q])
        for gap,conflict in [(1,True),(2,True),(3,False),(4,False)]:
            cs=[Hour(i*Q,110,100) for i in range(48)]
            cs[24-gap]=Hour((24-gap)*Q,115,95);cs[24]=Hour(24*Q,120,90)
            s=classify(cs,48*Q,'LIVE',WAVE)
            self.assertEqual(s['top_distance_minutes'],gap*15)
            self.assertEqual(s['top_boundary_conflict_45m'],conflict)
            self.assertEqual(s['bottom_boundary_conflict_45m'],conflict)

    def test_source_parity(self):
        b=StructureBuffer(candles(),rule=WAVE)
        snaps=[{k:v for k,v in b.snapshot(48*Q+1234,s).items() if k!='source'} for s in ('LIVE','BACKFILL','REPLAY')]
        self.assertEqual(snaps,[snaps[0]]*3)
        self.assertEqual(snaps[0],{k:v for k,v in classify(candles(),48*Q+1234,'REPLAY',WAVE).items() if k!='source'})

    def test_gap_delay_reconnect_first_availability(self):
        b=StructureBuffer(candles()[:47],rule=WAVE)
        at=48*Q;self.assertEqual(b.snapshot(at,'LIVE')['label'],'UNAVAILABLE')
        k=[47*Q,0,120,105,0,0,48*Q-1]
        self.assertTrue(b.add_kline(k,at+2000))
        self.assertEqual(b.snapshot(at+1999,'LIVE')['label'],'UNAVAILABLE')
        self.assertEqual(b.snapshot(at+2000,'LIVE')['label'],'BULL')
        self.assertTrue(b.add_kline(k,at+5000))
        self.assertEqual(b.snapshot(at+2000,'LIVE')['label'],'BULL')
        self.assertFalse(b.add_kline([48*Q,0,999,1,0,0,49*Q-1],at+5000))
        # Next boundary requires its own last closed candle; stale windows never slide.
        self.assertEqual(b.snapshot(49*Q,'LIVE')['label'],'UNAVAILABLE')
        with self.assertRaises(ValueError):b.add_kline([47*Q,0,121,105,0,0,48*Q-1],at+6000)

    def test_persistence_open_close_restart_and_error_isolation(self):
        from tests.test_price_structure_telemetry import position,Logger,synthetic
        from src.position.bot_full_engine import BotFullExitPosition
        live=LiveStructure('SOLUSDT',0);live.buffer=StructureBuffer(synthetic())
        # Position fixture opens at72.5h; use wave data spanning that time.
        live.wave_buffer=StructureBuffer([Hour(i*Q,110+i*.01,100+i*.01) for i in range(400)],rule=WAVE)
        install_live(live);p=position();entry=p.price_structure['wave_open_details']
        p.mark_closed(98.5,'HARD_STOP',iso(80*HOUR),{})
        self.assertEqual(p.price_structure['wave_open_details'],entry)
        self.assertEqual(p.price_structure['wave_close_details']['latest_closed_ms'],80*HOUR-1)
        restored=BotFullExitPosition.from_state(p.to_state(),p.config,None,Logger())
        self.assertEqual(restored.price_structure,p.price_structure)
        p=position();self.assertEqual(p.price_structure['wave_close'],'OPEN')
        with patch.object(live.wave_buffer,'snapshot',side_effect=ValueError('bad wave')):
            p.mark_closed(98.5,'HARD_STOP',iso(80*HOUR),{})
        self.assertEqual((p.status,p.price_structure['wave_close']),('CLOSED','UNAVAILABLE'))
        self.assertNotEqual(p.price_structure['trend_close'],'UNAVAILABLE')
        install_live(LiveStructure('SOLUSDT',81*HOUR))
        self.assertEqual(position().price_structure,{})

    def test_runtime_15m_telemetry_only_route(self):
        from src.app import Monitor
        from tests.test_price_structure_telemetry import Logger
        m=object.__new__(Monitor);m.config={'symbol':'SOLUSDT'}
        m.price_structure=LiveStructure('SOLUSDT',0);m.logger=Logger()
        m.market_shadow=Mock();m.registry=Mock();m.entry_engine=Mock();m._wave_only_stream=True
        k=dict(t=47*Q,T=48*Q-1,o='110',h='120',l='105',c='111',v='1',x=False)
        with patch.object(m,'_server_now_ms',return_value=48*Q+2000):
            m._on_ws_event('solusdt@kline_15m',dict(E=48*Q,k=k))
            self.assertEqual(len(m.price_structure.wave_buffer.hours),0)
            k['x']=True;m._on_ws_event('solusdt@kline_15m',dict(E=48*Q,k=k))
        self.assertEqual(m.price_structure.wave_buffer.hours[47*Q].available_ms,48*Q+2000)
        m.market_shadow.on_ws_event.assert_not_called();m.entry_engine.on_kline.assert_not_called()
        m.registry.on_tick.assert_not_called()

    def test_populated_wave_does_not_change_position_economics(self):
        from tests.test_price_structure_telemetry import position,synthetic
        def run(enabled):
            live=LiveStructure('SOLUSDT',0);live.buffer=StructureBuffer(synthetic())
            live.wave_buffer=StructureBuffer([Hour(i*Q,110+i*.01,100+i*.01) for i in range(400)],rule=WAVE)
            install_live(live if enabled else None);p=position()
            for i,price in enumerate((100.3,100.7,101.2,101.8,101.0,98.4)):
                p.on_tick(price,iso(73*HOUR+i*60_000))
            return {k:v for k,v in p.to_state().items() if not k.startswith(('trend_','wave_'))}
        self.assertEqual(run(False),run(True))

    def test_streams_reuse_15m_without_duplication(self):
        from src.app import Monitor
        m=object.__new__(Monitor);m.config={'symbol':'SOLUSDT','market_data':{'trade_stream':'solusdt@aggTrade','kline_streams':['solusdt@kline_15m']}}
        m.market_shadow=Mock();m.market_shadow.required_streams.return_value=[];m.market_shadow_ge30=None
        streams=m._market_streams()
        self.assertEqual(streams.count('solusdt@kline_15m'),1)
        self.assertFalse(m._wave_only_stream)
        m.config['market_data']['kline_streams']=[]
        self.assertEqual(m._market_streams().count('solusdt@kline_15m'),1)
        self.assertTrue(m._wave_only_stream)

    def test_reconstruction_carryovers_closed_open_and_source_integrity(self):
        from tools.wave_structure_backfill import reconstruct
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'bot';folder=root/'data/trades';folder.mkdir(parents=True)
            rows=[dict(symbol='SOLUSDT',pair_id='carry',opened_at=iso(48*Q),closed_at=iso(50*Q)),
                  dict(symbol='SOLUSDT',pair_id='live',opened_at=iso(50*Q),closed_at=None),
                  dict(symbol='SOLUSDT',pair_id='old',opened_at=iso(48*Q),closed_at=iso(49*Q))]
            path=folder/'x.jsonl';path.write_text(''.join(json.dumps(r)+'\n' for r in rows));before=path.read_bytes()
            cache=Path(tmp)/'15m.jsonl'
            cache.write_text('\n'.join(json.dumps(dict(open_time_ms=c.open_ms,close_time_ms=c.open_ms+Q-1,high=c.high,low=c.low)) for c in candles()))
            out=Path(tmp)/'one.jsonl';m=reconstruct(root,cache,out,iso(50*Q))
            got={r['pair_id']:r for r in map(json.loads,out.read_text().splitlines())}
            self.assertEqual(set(got),{'carry','live'})
            self.assertEqual(got['live']['wave_close'],'OPEN')
            self.assertEqual(got['carry']['wave_open_details']['source'],'BACKFILL')
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual(reconstruct(root,cache,out,iso(50*Q)),m)
            other=Path(tmp)/'two.jsonl';reconstruct(root,cache,other,iso(50*Q))
            self.assertEqual(out.read_bytes(),other.read_bytes())
            self.assertGreater(m['parity_snapshots'],0)

    def test_mare_exact_archived_snapshots(self):
        from tools.price_structure_backfill import load_buffer
        p=Path('data/analysis/trend_2x36h_20261017/study_v1/hourly.json')
        if not p.exists():self.skipTest('Laboratory artifact absent')
        old=json.loads(p.read_text());b=load_buffer(Path('data/analysis/trend_2x36h_20261017/SOLUSDT_1h.jsonl'))
        for s in old:
            expected={k:v for k,v in s.items() if k!='episode'}
            self.assertEqual(b.snapshot(s['at'],'REPLAY'),expected)

    def test_migration_only_wave_idempotence_rollback_interruption(self):
        for fail in (None,1):
            with self.subTest(fail=fail),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)/'bot';(root/'data/trades').mkdir(parents=True);(root/'data/state').mkdir()
                rows=[dict(pair_id='x',symbol='SOLUSDT',opened_at=iso(48*Q),closed_at=iso(49*Q),
                           net=-.3,trend_open='BEAR',trend_open_details={'version':VERSION,'evidence':[1,2]})]
                ledger=root/'data/trades/x.jsonl';ledger.write_text(json.dumps(rows[0])+'\n')
                dbpath=root/'data/state/x.history.sqlite'
                with sqlite3.connect(dbpath) as db:
                    db.executescript('CREATE TABLE history(kind TEXT,ordinal INTEGER,revision INTEGER,payload TEXT); CREATE TABLE pending(singleton INTEGER,payload TEXT); CREATE TABLE metadata(key TEXT,value TEXT);')
                    db.execute('INSERT INTO history VALUES (?,?,?,?)',('closed',0,2,json.dumps(rows[0])))
                db.close()
                originals={p:p.read_bytes() for p in (ledger,dbpath)}
                cache=Path(tmp)/'15m.jsonl'
                cache.write_text('\n'.join(json.dumps(dict(open_time_ms=c.open_ms,close_time_ms=c.open_ms+Q-1,high=c.high,low=c.low)) for c in candles()))
                backup=Path(tmp)/'backup'
                if fail:
                    with self.assertRaises(RuntimeError):migrate(root,cache,backup,since=iso(48*Q),rule=WAVE,fail_after=fail)
                else:
                    m=migrate(root,cache,backup,since=iso(48*Q),rule=WAVE)
                    self.assertEqual(verify(root,backup)['rule_version'],WAVE_VERSION)
                    self.assertEqual(migrate(root,cache,backup,since=iso(48*Q),rule=WAVE),m)
                    got=json.loads(ledger.read_text());self.assertEqual(got['wave_open'],'BULL')
                    self.assertEqual(economic(got,WAVE_KEYS),rows[0])
                    rollback(root,backup)
                self.assertEqual({p:p.read_bytes() for p in originals},originals)

    def test_report_fields_and_open_close_coverage(self):
        from tools.price_structure_report import overlay,aggregate
        from tools.forward_experiment_report import accepted_trade_rows
        r=dict(pair_id='x',symbol='SOLUSDT',open_ts=iso(48*Q),source_candle_open_time=1,entry_price=100)
        r.update(fields(classify(candles(),48*Q,'LIVE',WAVE),'open',WAVE))
        with tempfile.TemporaryDirectory() as tmp:
            got=overlay(r,Path(tmp));self.assertEqual(got['wave_open'],'BULL')
        row=accepted_trade_rows([dict(source_candle_open_time=1)],[],[r])[0]
        self.assertEqual(row['wave_open'],'BULL')
        self.assertEqual(aggregate([r],'wave')['wave_close']['OPEN'],1)


if __name__=='__main__':unittest.main()
