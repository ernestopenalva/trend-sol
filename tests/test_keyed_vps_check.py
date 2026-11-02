from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import unittest
from unittest.mock import Mock, patch
import yaml

from tools.keyed_vps_check import resource_verdict, observe, public_probe, ACTIVE, NEW
from src.monitor.price_structure import LiveStructure
from src.monitor.structure_repair import StructureRepair
from tools.keyed_fallback_config import fallback, KEYS

ROOT=Path(__file__).resolve().parents[1]


class VpsCheckTests(unittest.TestCase):
    def setUp(self):
        self.config=yaml.safe_load((ROOT/'config/config.yaml').read_text(encoding='utf8'))
        self.settings=self.config['instrumentation']['keyed_validation']

    def samples(self):
        return [{'memory_available_kib':210*1024,'swap_used_kib':100*1024,
                 'pswpin_pages':12,'pswpout_pages':5,'oom_kill_count':0,
                 'processes':[{'pid':123,'start_ticks':1,'uptime_seconds':1900}]} for _ in range(5)]

    def test_existing_static_swap_is_accepted_and_192_is_inclusive(self):
        values=self.samples();values[2]['memory_available_kib']=192*1024
        self.assertTrue(resource_verdict(values,self.settings,5)['passed'])
        values[2]['memory_available_kib']-=1
        self.assertFalse(resource_verdict(values,self.settings,5)['passed'])

    def test_continuous_paging_or_growth_blocks_even_with_available_memory(self):
        for field in ('swap_used_kib','pswpin_pages','pswpout_pages'):
            values=self.samples()
            for i,s in enumerate(values):s[field]+=i
            self.assertFalse(resource_verdict(values,self.settings,5)['passed'],field)

    def test_cold_process_or_changed_pid_or_incomplete_sample_blocks(self):
        for kind in ('cold','pid','partial','oom'):
            values=self.samples()
            if kind=='cold':values[0]['processes'][0]['uptime_seconds']=1799
            if kind=='pid':values[1]['processes'][0]['pid']=456
            if kind=='partial':values=values[:2]
            if kind=='oom':values[-1]['oom_kill_count']=1
            self.assertFalse(resource_verdict(values,self.settings,5)['passed'],kind)

    def test_observer_counts_missing_separately_and_does_not_mutate_sources(self):
        with TemporaryDirectory(dir=ROOT/'data/test_tmp') as tmp:
            root=Path(tmp);(root/'logs').mkdir()
            since='2026-11-02T00:00:00+00:00'
            events=[{'ts':since,'event':'structure_v2_enabled','new_shadows':
                     {k:{'enabled':True,'accept_new_entries':True} for k in NEW}},
                    {'ts':since,'event':'structure_health','wave':{'label':'UNAVAILABLE','undefined_reason':'MISSING_CANDLES'}}]
            (root/'logs/system.log').write_text(''.join(json.dumps(x)+'\n' for x in events),encoding='utf8')
            for key in ACTIVE:
                settings=self.config['instrumentation'][key]
                state=root/settings['state_file'];state.parent.mkdir(parents=True,exist_ok=True)
                state.write_text(json.dumps({'positions':[]}),encoding='utf8')
            first=self.config['instrumentation'][ACTIVE[0]]
            ledger=root/first['ledger_file'];ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({'pair_id':'one','opened_at':since,'closed_at':since,'wave_open':'UNAVAILABLE'})+'\n',encoding='utf8')
            before={str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()}
            result=observe(SimpleNamespace(root=root,since=since,until='2026-11-02T00:01:00+00:00'),self.config)
            arm=result['arms'][ACTIVE[0]]
            self.assertEqual(arm['open']['unavailable_pct_of_labeled'],100)
            self.assertIsNone(arm['close']['unavailable_pct_of_labeled'])
            self.assertEqual(arm['close']['counts'],{'MISSING_FIELD':1})
            self.assertIsNone(result['arms'][ACTIVE[1]]['open']['unavailable_pct_of_labeled'])
            self.assertTrue(result['new_shadows_confirmed_enabled'])
            self.assertEqual(before,{str(p):p.read_bytes() for p in root.rglob('*') if p.is_file()})

    def test_sparse_health_keeps_gap_visible_and_repair_recovers_it(self):
        now=[900000*60+3000];provider=LiveStructure('SOLUSDT',0);logger=Mock()
        client=Mock()
        def rows(symbol,interval,limit):
            step={'15m':900000,'5m':300000}[interval];end=now[0]//step*step
            return [[t,0,100,90,0,0,t+step-1] for t in range(end-(limit-1)*step,end+step,step)]
        client.klines.side_effect=rows
        repair=StructureRepair(provider,client,logger,lambda:now[0],
                               {'poll_seconds':5,'close_grace_ms':2000,'health_log_seconds':60})
        repair.repair_once();repair.repair_once()
        calls=[c for c in logger.system.call_args_list if c.args[0]=='structure_health']
        self.assertEqual(len(calls),1)
        self.assertTrue(calls[0].kwargs['initial_warmup_complete'])
        now[0]+=60000;client.klines.side_effect=TimeoutError
        del provider.wave_buffer.hours[min(provider.wave_buffer.hours)+900000]
        # Remove an expected slot, not an extra retained seed slot.
        end=now[0]//900000*900000
        provider.wave_buffer.hours.pop(end-10*900000,None);provider.wave_buffer._cache.clear()
        repair.repair_once()
        latest=[c for c in logger.system.call_args_list if c.args[0]=='structure_health'][-1]
        self.assertEqual(latest.kwargs['wave']['label'],'UNAVAILABLE')
        client.klines.side_effect=rows;repair.repair_once()
        self.assertNotEqual(provider.wave_buffer.snapshot(now[0],'LIVE')['label'],'UNAVAILABLE')

    def test_fallback_changes_only_two_flags_after_authorized_memory_failure(self):
        values=self.samples();values[2]['memory_available_kib']=191*1024
        evidence={'mode':'resources','phase':'after','settings':self.settings,
                  **resource_verdict(values,self.settings,5)}
        result=fallback(self.config,evidence)
        expected=deepcopy(self.config)
        for key in KEYS:expected['instrumentation'][key]['enabled']=False
        self.assertEqual(result,expected)
        self.assertTrue(all(self.config['instrumentation'][k]['enabled'] for k in KEYS))
        self.assertEqual(fallback(self.config,{**evidence,'phase':'before'}),expected)
        with self.assertRaises(ValueError):fallback(self.config,{**evidence,'phase':'fallback'})
        with self.assertRaises(ValueError):fallback(self.config,{**evidence,'warmed_up':False})

    def test_isolated_public_probe_recovers_gap_without_retroactive_availability(self):
        clock=[900000*2000000+3000]
        def now():
            clock[0]+=2
            return clock[0]/1000
        client=Mock()
        def candles(symbol,interval,limit):
            clock[0]+=100
            step={'15m':900000,'5m':300000}[interval];end=clock[0]//step*step
            return [[t,0,100,90,0,0,t+step-1] for t in range(end-(limit-1)*step,end+step,step)]
        client.klines.side_effect=candles
        with patch('src.exchange.binance_market_data.BinanceMarketDataClient',return_value=client), \
             patch('tools.keyed_vps_check.time.time',side_effect=now):
            result=public_probe(SimpleNamespace(),self.config)
        self.assertTrue(result['passed'])
        self.assertFalse(result['production_buffers_modified'])
        self.assertEqual(result['past_snapshot_after_repair']['label'],'UNAVAILABLE')
        self.assertEqual(client.klines.call_count,3)
        client.session.close.assert_called_once()


if __name__=='__main__':unittest.main()
