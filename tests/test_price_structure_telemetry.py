import json
import tempfile
import unittest
from pathlib import Path
from src.monitor.price_structure import (
    HOUR, Hour, StructureBuffer, LiveStructure, install_live, fields, timestamp_ms)
from src.position.bot_full_engine import BotFullExitPosition
from src.trade_ledger import TradeLedger
from tools.price_structure_backfill import reconstruct, load_buffer, verify_parity
from tools.price_structure_report import overlay, aggregate
from tools import forward_experiment_report as report


class Logger:
    def trade(self, *a, **k): pass
    def system(self, *a, **k): pass


def synthetic():
    import math
    return [Hour(i*HOUR, 110+math.sin(i)*4+i*.02, 100+math.sin(i)*4+i*.02) for i in range(100)]


def position():
    return BotFullExitPosition('test', 'SOLUSDT', 100, .2, {},
        '1970-01-04T00:30:00+00:00', {'hard_stop': {'enabled': True, 'stop_pct': 1.5}},
        None, Logger(), source_candle_open_time=1)


class StructureTelemetryTests(unittest.TestCase):
    def tearDown(self): install_live(None)

    def test_parity_with_original_function_for_every_synthetic_window(self):
        from tools.price_structure_study import label
        cs = synthetic()
        for i in range(71, len(cs)):
            window = cs[i-71:i+1]
            expected, hi, lo = label(window)
            snap = StructureBuffer(cs).snapshot((i+1)*HOUR, 'BACKFILL')
            self.assertEqual(snap['label'], expected)
            self.assertEqual([p['open_ms'] for p in snap['highs']], [window[j].open_ms for j in hi])
            self.assertEqual([p['open_ms'] for p in snap['lows']], [window[j].open_ms for j in lo])

    def test_open_hour_future_and_confirmation_excluded(self):
        cs = synthetic()
        at = 72*HOUR
        before = StructureBuffer(cs).snapshot(at-1, 'LIVE')
        snap = StructureBuffer(cs).snapshot(at, 'LIVE')
        self.assertEqual(snap, StructureBuffer(cs[:72]).snapshot(at, 'LIVE'))
        self.assertEqual(before['latest_closed_ms'], 71*HOUR-1)
        self.assertTrue(all(p['confirmed_ms'] < at for p in snap['highs']+snap['lows']))
        self.assertTrue(all(p['open_ms'] <= 68*HOUR for p in snap['highs']+snap['lows']))

    def test_gap_is_not_stale_substitute_and_recovers(self):
        cs = synthetic()[:72]
        b = StructureBuffer(cs[:-1])
        self.assertEqual(b.snapshot(72*HOUR, 'LIVE')['status'], 'MISSING_CANDLES')
        c = cs[-1]
        self.assertTrue(b.add_kline([c.open_ms, 0, c.high, c.low, 0, 0, c.open_ms+HOUR-1],72*HOUR))
        self.assertEqual(b.snapshot(72*HOUR, 'LIVE')['status'], 'OK')
        self.assertFalse(b.add_kline([72*HOUR,0,999,1,0,0,73*HOUR-1],72*HOUR+1000))

    def test_entry_exit_independent_persist_restore_and_ledger(self):
        live = LiveStructure('SOLUSDT', 0); live.buffer = StructureBuffer(synthetic())
        install_live(live)
        p = position()
        entry = dict(p.price_structure)
        p.mark_closed(98.5, 'HARD_STOP', '1970-01-04T10:00:00+00:00', {})
        self.assertEqual(p.price_structure['trend_open_details'], entry['trend_open_details'])
        self.assertNotEqual(p.price_structure['trend_open_at'],p.price_structure['trend_close_at'])
        self.assertEqual(p.price_structure['trend_close_details']['latest_closed_ms'],82*HOUR-1)
        restored = BotFullExitPosition.from_state(p.to_state(),p.config,None,Logger())
        self.assertEqual(restored.price_structure,p.price_structure)
        record = TradeLedger(Path('.'))._record(p, {})
        self.assertEqual(record['trend_close'],p.price_structure['trend_close'])
        self.assertEqual(p._trade_event('CLOSE',98.5,-1.5,'HARD_STOP')['trend_open'],entry['trend_open'])

    def test_restart_does_not_fabricate_live_entry(self):
        live = LiveStructure('SOLUSDT', 80*HOUR);live.buffer = StructureBuffer(synthetic())
        install_live(live)
        p=position()
        self.assertEqual(p.price_structure,{})
        p.mark_closed(99,'HARD_STOP','1970-01-04T10:00:00+00:00',{})
        self.assertIn('trend_close',p.price_structure)

    def test_economic_parity_with_and_without_telemetry(self):
        def run(enabled):
            live=LiveStructure('SOLUSDT',0);live.buffer=StructureBuffer(synthetic())
            install_live(live if enabled else None)
            p=position();events=[]
            for price in [100.5,101,100,98.4]:
                p.on_tick(price,'1970-01-04T01:00:00+00:00')
                events.append((p.status,p.exit_price,p.exit_reason,p.effective_stop,p.highest_price,p.reserved_qty))
            state=p.to_state()
            return events,{k:v for k,v in state.items() if not k.startswith('trend_')}
        self.assertEqual(run(False),run(True))

    def test_backfill_carryover_and_only_causal_data(self):
        b=StructureBuffer(synthetic())
        row=dict(pair_id='test',symbol='SOLUSDT',opened_at='1970-01-04T00:30:00+00:00',closed_at='1970-01-04T10:00:00+00:00')
        rs=reconstruct([('test.jsonl',row)],b,80*HOUR)
        self.assertEqual(len(rs),1)
        self.assertEqual(rs[0]['trend_open_details']['source'],'BACKFILL')
        self.assertEqual(rs[0]['trend_close_details']['latest_closed_ms'],82*HOUR-1)
        self.assertEqual(reconstruct([('test.jsonl',{**row,'closed_at':None})],b,80*HOUR),[])

    def test_overlay_identity_native_precedence_open_and_grouping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'data/telemetry/price_structure_backfill.jsonl'
            path.parent.mkdir(parents=True)
            record=dict(symbol='SOLUSDT',pair_id='test',opened_at='1970-01-04T00:30:00+00:00',closed_at='1970-01-04T10:00:00+00:00',trend_open='BULL',trend_close='BEAR')
            path.write_text(json.dumps(record)+'\n')
            self.assertEqual(overlay(record,root)['trend_close'],'BEAR')
            native={**record,'trend_open':'MIXED'}
            self.assertEqual(overlay(native,root)['trend_open'],'MIXED')
            opened={k:v for k,v in record.items() if k not in ['closed_at','trend_open','trend_close']}
            augmented=overlay(opened,root)
            self.assertNotIn('trend_close',augmented)
            self.assertEqual(aggregate([augmented])['transitions'],{'BULL -> OPEN':1})
            self.assertNotIn('trend_open',overlay({**opened,'pair_id':'other'},root))

    def test_report_reads_structural_fields_without_changing_ema(self):
        event=dict(source_candle_open_time=1,ema_context='BUL',macd_context='BU+')
        opened=dict(source_candle_open_time=1,entry_price=100,trend_open='BEAR',open_ts='1970-01-04T00:30:00+00:00')
        r=report.accepted_trade_rows([event],[],[opened])[0]
        self.assertEqual((r['trend_open'],r['ema'],r['macd'],r['status']),('BEAR','BUL','BU+','OPEN'))

    def test_three_previously_audited_instants(self):
        path=Path('data/analysis/price_structure_72h_20261010/market/SOLUSDT_1h.jsonl')
        if not path.exists():
            self.skipTest('Audited local study artifact absent')
        b=load_buffer(path)
        cases=[('2026-10-08T19:00:00-03:00','BEAR','LH','LL',[116.81,115.86],[114.22,105.71]),
               ('2026-09-29T18:45:00-03:00','BULL','HH','HL',[120.73,121.69],[116.32,117.36]),
               ('2026-09-24T08:30:00-03:00','MIXED','LH','HL',[119.77,116.13],[113.88,114.07])]
        for at,label,hi,lo,tops,bottoms in cases:
            s=b.snapshot(at,'BACKFILL')
            self.assertEqual((s['label'],s['highs_class'],s['lows_class']),(label,hi,lo))
            self.assertEqual([p['price'] for p in s['highs']],tops)
            self.assertEqual([p['price'] for p in s['lows']],bottoms)

    def test_live_telemetry_error_cannot_block_close(self):
        from unittest.mock import patch
        live=LiveStructure('SOLUSDT',0);install_live(live)
        p=position()
        with patch.object(live.buffer,'snapshot',side_effect=ValueError('bad telemetry')):
            p.mark_closed(98.5,'HARD_STOP','1970-01-04T10:00:00+00:00',{})
        self.assertEqual((p.status,p.exit_price,p.reserved_qty),('CLOSED',98.5,0))
        self.assertEqual(p.price_structure['trend_close_details']['status'],'TELEMETRY_ERROR')

    def test_hourly_route_is_separate_from_economic_consumers(self):
        from src.app import Monitor
        from unittest.mock import Mock
        m=object.__new__(Monitor)
        m.config={'symbol':'SOLUSDT'}
        m.price_structure=LiveStructure('SOLUSDT',0)
        m.market_shadow=Mock();m.registry=Mock();m.entry_engine=Mock();m.logger=Logger()
        payload={'E':73*HOUR,'k':{'t':72*HOUR,'T':73*HOUR-1,'o':'100','h':'110','l':'99','c':'105','v':'1','x':False}}
        m._on_ws_event('solusdt@kline_1h',payload)
        self.assertEqual(len(m.price_structure.buffer.hours),0)
        payload['k']['x']=True
        m._on_ws_event('solusdt@kline_1h',payload)
        self.assertEqual(len(m.price_structure.buffer.hours),1)
        m.market_shadow.on_ws_event.assert_not_called()
        m.registry.on_tick.assert_not_called()
        m.entry_engine.on_kline.assert_not_called()
        m.config['market_data']={'trade_stream':'solusdt@aggTrade','kline_streams':['solusdt@kline_5m']}
        m.market_shadow.required_streams.return_value=[]
        m.market_shadow_ge30=None
        streams=m._market_streams()
        self.assertEqual(streams.count('solusdt@kline_1h'),1)
        self.assertTrue(m._structure_only_stream)
        m.config['market_data']['kline_streams'].append('solusdt@kline_1h')
        self.assertEqual(m._market_streams().count('solusdt@kline_1h'),1)
        self.assertFalse(m._structure_only_stream)


if __name__=='__main__': unittest.main()
