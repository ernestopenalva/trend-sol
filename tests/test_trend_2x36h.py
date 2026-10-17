import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from src.monitor.price_structure import Hour,HOUR,VERSION,classify,StructureBuffer,restore_fields
from tools.trend_structure_migrate import migrate,rollback,economic


def candles(hi=120,lo=105,distance=8):
    cs=[Hour(i*HOUR,110,100) for i in range(72)]
    cs[36-distance]=Hour((36-distance)*HOUR,115,95)
    cs[36]=Hour(36*HOUR,hi,lo)
    # Ensure the second half's min is lo (all other candles in that half >lo).
    for i in range(37,72):cs[i]=Hour(i*HOUR,hi-1,lo+1)
    return cs


class NewStructureTests(unittest.TestCase):
    def test_four_states_and_equal(self):
        for hi,lo,name in [(120,105,'BULL'),(112,90,'BEAR'),(120,90,'EXPA'),(112,98,'COMP'),(115,98,'UNDEFINED'),(120,95,'UNDEFINED')]:
            s=classify(candles(hi,lo),72*HOUR,'REPLAY')
            self.assertEqual(s['label'],name)
            self.assertEqual(s['undefined_reason'],'EQUAL_EXTREME' if name=='UNDEFINED' else None)
    def test_boundary_distances_do_not_change_label(self):
        for d,flag in [(1,True),(3,True),(4,False),(8,False)]:
            s=classify(candles(distance=d),72*HOUR,'LIVE')
            self.assertEqual((s['label'],s['top_distance_hours'],s['top_boundary_conflict_4h']),('BULL',d,flag))
            self.assertEqual(s['bottom_boundary_conflict_4h'],flag)
    def test_oldest_tie_convention(self):
        cs=candles();cs[1]=Hour(HOUR,115,95)
        s=classify(cs,72*HOUR,'REPLAY')
        self.assertEqual(s['topo_1']['open_ms'],HOUR)
        self.assertEqual(s['fundo_1']['open_ms'],HOUR)
    def test_extreme_crossing_midpoint_relabels_without_hysteresis(self):
        cs=candles()
        old=classify(cs,72*HOUR,'REPLAY')
        new=classify(cs+[Hour(72*HOUR,119,108)],73*HOUR,'REPLAY')
        self.assertEqual((old['label'],new['label']),('BULL','COMP'))
        self.assertEqual(new['topo_1']['open_ms'],old['topo_2']['open_ms'])
        self.assertTrue(new['top_boundary_conflict_4h'])
    def test_gaps_incomplete_and_future_not_used(self):
        cs=candles()
        for broken in [cs[:-1],cs[:30]+cs[31:],cs[:-1]+[Hour(71*HOUR,999,1,False)]]:
            self.assertEqual(classify(broken,72*HOUR,'LIVE')['label'],'UNDEFINED')
        a=classify(cs,72*HOUR,'LIVE')
        self.assertEqual(a,classify(cs+[Hour(72*HOUR,999,1)],72*HOUR,'LIVE'))
        b=classify(cs+[Hour(72*HOUR,999,1)],73*HOUR,'LIVE')
        self.assertEqual(b['window_start_ms'],HOUR)
        self.assertNotEqual(a['topo_2'],b['topo_2'])
    def test_source_parity_and_deprecated_version_unavailable(self):
        from src.monitor.price_structure import LiveStructure,iso
        from tools.price_structure_backfill import reconstruct
        live=LiveStructure('SOLUSDT',0);live.buffer=StructureBuffer(candles())
        row={'symbol':'SOLUSDT','pair_id':'x','opened_at':iso(72*HOUR)}
        values=[live.capture('SOLUSDT',72*HOUR),
                reconstruct([('x.jsonl',row)],live.buffer,0)[0]['trend_open_details'],
                classify(candles(),72*HOUR,'REPLAY')]
        self.assertEqual([{k:v for k,v in s.items() if k!='source'} for s in values],[{k:v for k,v in values[0].items() if k!='source'}]*3)
        self.assertEqual(restore_fields({'trend_open':'MIXED','trend_open_details':{'version':'OLD'}}),{})

    def test_migration_idempotence_sqlite_economics_and_rollback(self):
        self._migration(False)
    def test_partial_failure_rolls_back(self):
        self._migration(True)
    def test_old_open_event_does_not_receive_future_close(self):
        from tools.trend_structure_migrate import transform
        from src.monitor.price_structure import iso
        row=dict(pair_id='x',symbol='SOLUSDT',opened_at=iso(72*HOUR),closed_at=iso(73*HOUR),net=-.3)
        event={'event':'OPEN','pair_id':'x','ts':iso(72*HOUR)}
        result=transform(event,StructureBuffer(candles()),0,{'x':row})
        self.assertEqual(result['trend_close'],'OPEN')
        self.assertEqual(result['trend_open_details']['version'],VERSION)
        self.assertEqual(economic(result),event)
    def test_delayed_close_after_restart_is_not_left_open(self):
        from src.monitor.price_structure import LiveStructure,install_live,iso
        from tests.test_price_structure_telemetry import position
        live=LiveStructure('SOLUSDT',80*HOUR);live.buffer=StructureBuffer(candles()+[Hour(72*HOUR,121,106)])
        install_live(live)
        try:
            p=position();p.mark_closed(98.5,'HARD_STOP',iso(73*HOUR),{})
            self.assertNotEqual(p.price_structure.get('trend_close'),'OPEN')
            self.assertEqual(p.price_structure['trend_close_details']['version'],VERSION)
        finally:install_live(None)
    def _migration(self,fail):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'bot';backup=Path(tmp)/'backup'
            (root/'data/trades').mkdir(parents=True);(root/'data/state').mkdir()
            candlefile=Path(tmp)/'hours.jsonl'
            candlefile.write_text('\n'.join(json.dumps(dict(open_time_ms=c.open_ms,close_time_ms=c.open_ms+HOUR-1,high=c.high,low=c.low)) for c in candles()+[Hour(72*HOUR,122,106)]))
            row=dict(pair_id='x',symbol='SOLUSDT',opened_at='1970-01-04T00:00:00+00:00',closed_at='1970-01-04T01:00:00+00:00',net=-.3,trend_open='MIXED',trend_open_details={'version':'OLD'})
            ledger=root/'data/trades/test.jsonl';ledger.write_text(json.dumps(row)+'\n')
            state=root/'data/state/test.json';state.write_text(json.dumps(dict(history_revision=5,watermark=10,equity=15,positions=[{**row,'closed_at':None,'status':'OPEN'}])))
            journal=root/'data/state/test.history.sqlite'
            with sqlite3.connect(journal) as db:
                db.executescript('CREATE TABLE history(kind TEXT,ordinal INTEGER,revision INTEGER,payload TEXT); CREATE TABLE pending(singleton INTEGER,payload TEXT); CREATE TABLE metadata(key TEXT,value TEXT);')
                db.execute('INSERT INTO history VALUES (?,?,?,?)',('ledger',0,5,json.dumps(row)))
                db.execute('INSERT INTO metadata VALUES (?,?)',('owner',journal.name))
            db.close()
            originals={p:p.read_bytes() for p in (ledger,state,journal)}
            if fail:
                with self.assertRaises(RuntimeError):migrate(root,candlefile,backup,since='1970-01-04T00:30:00+00:00',fail_after=1)
            else:
                m=migrate(root,candlefile,backup,since='1970-01-04T00:30:00+00:00')
                self.assertEqual(m['status'],'APPLIED')
                new=json.loads(ledger.read_text());self.assertEqual(new['trend_open_details']['version'],VERSION)
                self.assertEqual(economic(new),economic(row))
                self.assertEqual(json.loads(state.read_text())['positions'][0]['trend_close'],'OPEN')
                self.assertEqual(migrate(root,candlefile,backup,since='1970-01-04T00:30:00+00:00'),m)
                with sqlite3.connect(journal) as db:
                    r=db.execute('SELECT kind,ordinal,revision,payload FROM history').fetchone()
                    self.assertEqual(r[:3],('ledger',0,5));self.assertEqual(economic(json.loads(r[3])),economic(row))
                db.close();rollback(root,backup)
            self.assertEqual({p:p.read_bytes() for p in originals},originals)


if __name__=='__main__':unittest.main()
