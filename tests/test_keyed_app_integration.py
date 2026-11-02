"""Startup/warmup integration without network, orders or production state."""
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import yaml

from src.app import Monitor
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.keyed_shadow import KeyedShadow
from src.monitor.price_structure import install_live

ROOT=Path(__file__).resolve().parents[1]


class KeyedAppIntegrationTests(unittest.TestCase):
    def tearDown(self):install_live(None)

    def test_three_enabled_shadows_warmup_versions_and_disabled_dmi_no_history(self):
        with TemporaryDirectory(dir=ROOT/'data/test_tmp') as tmp, ExitStack() as cleanup:
            root=Path(tmp);cfg=yaml.safe_load((ROOT/'config/config.yaml').read_text(encoding='utf8'))
            cfg['logging']['console']=False
            cfg['instrumentation']['dmi15_trajectory_context_shadow']['enabled']=False
            path=root/'config.yaml';path.write_text(yaml.safe_dump(cfg),encoding='utf8')
            with patch('src.app.PROJECT_ROOT',root),patch('src.app.load_project_env'), \
                 patch('src.app.BinanceClient') as execution,patch('src.app.BinanceMarketDataClient'):
                monitor=Monitor(path)
            for shadow in [monitor.circuit_breaker_shadow,monitor.be030_shadow,
                           monitor.be_off_shadow,monitor.be_off_cb_shadow,*monitor.forward_experiment_shadows]:
                if isinstance(shadow,CircuitBreakerShadow):cleanup.callback(shadow.close)
            self.assertEqual(sum(isinstance(s,KeyedShadow) and s.enabled for s in monitor.forward_experiment_shadows),2)
            self.assertTrue(next(s for s in monitor.forward_experiment_shadows
                if s.settings_key=='be_off_cb_ema_macd_cluster_act10_shadow').enabled)
            monitor.entry_engine.required_timeframes=Mock(return_value=['15m'])
            monitor.entry_engine.load_history=Mock()
            monitor.dmi15_trajectory_context_shadow.engine.load_history=Mock()
            monitor.slow_ge_context_shadow.engine.load_history=Mock()
            now=1_793_491_203_000;monitor._server_now_ms=lambda:now
            monitor.structure_repair.now_ms=lambda:now
            def candles(symbol,interval,limit):
                step={'1h':3600000,'15m':900000,'5m':300000}[interval]
                end=now//step*step
                return [[t,100,110,90,100,1,t+step-1] for t in range(end-(limit-1)*step,end+step,step)]
            monitor.market_data_client.klines.side_effect=candles
            monitor._load_historical_candles();monitor.structure_repair.repair_once()
            monitor.dmi15_trajectory_context_shadow.engine.load_history.assert_not_called()
            monitor.slow_ge_context_shadow.engine.load_history.assert_not_called()
            self.assertTrue(monitor.structure_repair.ready)
            self.assertEqual(monitor.price_structure.wave_buffer.rule.version,'TREND_12H_2X6H_15M_V2')
            self.assertTrue(all(s.structure_ready() for s in monitor.forward_experiment_shadows if isinstance(s,KeyedShadow)))
            execution.return_value.market_sell.assert_not_called()
            execution.return_value.market_buy.assert_not_called()


if __name__=='__main__':unittest.main()
