import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch, Mock
import yaml

from src.logging_utils import JsonlLogger
from src.monitor.entry_engine import EntrySignal
from src.monitor.keyed_shadow import (KeyedShadow, EmaMacdClusterAct10Shadow,
                                      EmaMacdClusterAct10K1Shadow)
from src.monitor.price_structure import Hour, StructureRule, classify_window, classify, LiveStructure
from src.monitor.structure_repair import StructureRepair

ROOT=Path(__file__).resolve().parents[1]

class KeyedTests(unittest.TestCase):
    def shadow(self, root, green=False):
        cfg=yaml.safe_load((ROOT/'config/config.yaml').read_text(encoding='utf8'))
        key='chaveado_vivo_verde_shadow' if green else 'chaveado_vivo_shadow'
        cfg['instrumentation'][key].update(enabled=True, accept_new_entries=True)
        o=KeyedShadow(root,cfg,JsonlLogger(root,cfg),None,settings_key=key,
                      cohort_started_at='2026-11-01T00:00:00+00:00')
        self.addCleanup(o.close)
        o.structure_ready=lambda:True
        return o

    def test_equal_extremes_v2_and_v1_is_unchanged(self):
        for dh,dl,expected in [(1,1,'BULL'),(-1,-1,'BEAR'),(0,-1,'EXPA'),(1,0,'EXPA'),
                               (0,1,'COMP'),(-1,0,'COMP'),(0,0,'LATE')]:
            cs=[Hour(0,10,2),Hour(1,10+dh,2+dl)]
            self.assertEqual(classify_window(cs,StructureRule('TEST_V2',1,2,1))[0],expected)
            if dh==0 or dl==0:
                self.assertEqual(classify_window(cs,StructureRule('TEST_V1',1,2,1))[0],'UNDEFINED')

    def test_fallback_green_and_live_fast_consumption_restore_ladder(self):
        # Close journals before TemporaryDirectory cleanup on Windows.
        with TemporaryDirectory(dir=ROOT/'data/test_tmp') as tmp:
            root=Path(tmp);o=self.shadow(root)
            sig=EntrySignal('SOLUSDT',100,'2026-11-01T00:01:00+00:00',1793491200000,.2,'1m',14)
            context={'tf_5m':{'ema_context':'LON','macd_context':'BU+','latest_closed_at_ms':1793491199999}}
            with patch('src.monitor.keyed_shadow.capture_live',return_value={'wave_close':'BULL','marola_90_close':'BULL'}):
                self.assertTrue(o.on_approved_real_a_signal(sig,context))
            p=o.positions[0]
            self.assertEqual((p.trailing_activation_atr,p.trailing_gap_atr),(10,13))
            with patch('src.monitor.keyed_shadow.capture_live',return_value={'wave_close':'LATE','marola_90_close':'BULL'}):
                o.on_tick(99.5,'2026-11-01T00:02:00+00:00')
            self.assertTrue(p.fast_drop_evaluated)
            self.assertEqual(p.status,'OPEN')
            self.assertTrue(any(e['event']=='KEYED_REDUCER_DECISION' and e['one_shot_consumed'] for e in o.audit_events))
            o.close()
            restored=self.shadow(root)
            self.assertEqual(restored.positions[0].entry_house['label'],'ACT10')
            self.assertEqual(restored.positions[0].trailing_gap_atr,13)
            self.assertTrue(restored.positions[0].fast_drop_evaluated)
            restored.close()
            green=self.shadow(root,True)
            with patch('src.monitor.keyed_shadow.capture_live',return_value={'wave_close':'UNAVAILABLE','marola_90_close':'BULL'}):
                self.assertFalse(green.on_approved_real_a_signal(sig,context))
            self.assertEqual(green.audit_events[-1]['event'],'ENTRY_BLOCKED_SEM_ROTULO')
            green.close()

    def test_gap_repair_is_causal_deduplicated_and_ignores_open_candle(self):
        now=[3600000*80+3000]
        provider=LiveStructure('SOLUSDT',0)
        def rows(symbol,interval,limit):
            step={'1h':3600000,'15m':900000,'5m':300000}[interval];end=now[0]//step*step
            return [[t,0,100+t/1e8,90,0,0,t+step-1] for t in range(end-(limit-1)*step,end+step,step)]
        client=Mock();client.klines.side_effect=rows
        repair=StructureRepair(provider,client,Mock(),lambda:now[0],{'poll_seconds':5,'close_grace_ms':2000})
        repair.repair_once()
        self.assertTrue(repair.ready)
        self.assertEqual(provider.wave_buffer.snapshot(now[0],'LIVE')['status'],'OK')
        self.assertEqual(provider.wave_buffer.snapshot(now[0]-1,'LIVE')['label'],'UNAVAILABLE')
        self.assertNotIn(now[0]//900000*900000,provider.wave_buffer.hours)
        repair.repair_once()
        self.assertEqual(client.klines.call_count,3)
        tide=provider.buffer
        missing=79*3600000
        del tide.hours[missing]
        tide._cache.clear()
        gap_at=now[0]
        self.assertEqual(tide.snapshot(gap_at,'LIVE')['missing_hours'],[missing])
        now[0]+=1
        repair.repair_once()
        self.assertEqual(tide.snapshot(now[0],'LIVE')['missing_hours'],[])
        self.assertEqual(tide.snapshot(gap_at,'LIVE')['missing_hours'],[missing])
        self.assertEqual(tide.snapshot(now[0],'LIVE')['label'],'EXPA')

    def test_k1_runs_after_normal_spacing_and_block_does_not_consume_bucket(self):
        with TemporaryDirectory(dir=ROOT/'data/test_tmp') as tmp:
            root=Path(tmp)
            cfg=yaml.safe_load((ROOT/'config/config.yaml').read_text(encoding='utf8'))
            key='ema_macd_cluster_act10_k1_shadow'
            cfg['instrumentation'][key].update(enabled=True,accept_new_entries=True)
            shadow=EmaMacdClusterAct10K1Shadow(root,cfg,JsonlLogger(root,cfg),None,
                cohort_started_at='2026-11-01T00:00:00+00:00')
            try:
                t=1793491200000
                context={'tf_5m':{'ema_context':'LON','macd_context':'BU+',
                                  'latest_closed_at_ms':t+300000-1}}
                def signal(offset, price, atr):
                    source=t+offset*300000
                    item=EntrySignal('SOLUSDT',price,
                        __import__('datetime').datetime.fromtimestamp((source+60000)/1000,
                        __import__('datetime').timezone.utc).isoformat(),source,atr,'1m',14)
                    return shadow.on_approved_real_a_signal(item,context)
                self.assertTrue(signal(0,100,.5))
                self.assertTrue(signal(1,98,1))
                # Ordinary 1 ATR spacing passes from both entries; K1 blocks because
                # the open inventory contains a negative position and price is above 97.5.
                self.assertFalse(signal(2,99.2,.5))
                self.assertEqual(len(shadow.open_positions),2)
                self.assertEqual(shadow.entries_by_bucket.get(t+600000,0),0)
                event=shadow.audit_events[-1]
                self.assertEqual(event['event'],'ENTRY_BLOCKED_SPACING_QUEDA')
                self.assertEqual((event['open_positions_count'],event['negative_positions_count']),(2,1))
                self.assertEqual(event['lowest_open_entry_price'],98)
                self.assertEqual(event['required_signal_price'],97.5)
                self.assertEqual(event['entry_atr'],.5)
                self.assertEqual(shadow.k1_block_count,1)
                shadow.close()
                shadow=EmaMacdClusterAct10K1Shadow(root,cfg,JsonlLogger(root,cfg),None,
                    cohort_started_at='2026-11-01T00:00:00+00:00')
                self.assertEqual(shadow.k1_block_count,1)
                self.assertEqual(len(shadow.open_positions),2)
                self.assertTrue(signal(3,97.4,.5))
                self.assertEqual(len(shadow.open_positions),3)
            finally:
                shadow.close()

if __name__=='__main__':unittest.main()
