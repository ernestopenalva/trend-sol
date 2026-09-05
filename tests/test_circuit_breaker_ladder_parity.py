"""Synthetic paths through the prices observed in the September 4 audit."""
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from src.position.phantom_execution import PhantomExecutionClient
from tests.test_circuit_breaker_shadow import _config


class CircuitBreakerLadderParityTests(unittest.TestCase):
    def test_audited_paths_and_restored_positions_match_real_engine(self):
        cases = (
            (101.86, 0.029349357338441066, [102.01, 101.90, 102.20, 102.11], 'BREAKEVEN'),
            (101.93, 0.03142429141666558, [102.20, 102.02, 102.31, 102.18], 'BREAKEVEN'),
            (101.86, 0.029349357338441066, [102.23, 102.11], 'BREAKEVEN'),
            (101.86, 0.2, [102.87, 102.15], 'PROFIT_LOCK'),
            (101.86, 0.029349357338441066, [102.30, 102.14], 'TRAILING'),
            (101.86, 0.029349357338441066, [100.0], 'HARD_STOP'),
        )
        for entry, atr, prices, reason in cases:
            for restart in (False, True):
                with self.subTest(entry=entry, reason=reason, restart=restart), TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    config = _config()
                    config['risk']['profit_lock'].update({
                        'economic_floor': {'enabled': True, 'net_margin_pct': .05},
                        'steps': [{'trigger_atr': 5, 'lock_atr': 1.5}, {'trigger_atr': 8, 'lock_atr': 3}, {'trigger_atr': 12, 'lock_atr': 6}],
                    })
                    config['ladder'] = {'be_net_margin_pct': .05, 'be_activation_buffer_atr': .5}
                    logger = JsonlLogger(root, config)
                    shadow = CircuitBreakerShadow(root, config, logger, None)
                    now = datetime.now(timezone.utc)
                    signal = EntrySignal('SOLUSDT', entry, now.isoformat(), 1788564900000, atr, '1m', 14)
                    self.assertTrue(shadow.on_approved_real_a_signal(signal, None))
                    client = PhantomExecutionClient()
                    real = BotFullExitPosition(
                        pair_id='real-control', symbol='SOLUSDT', entry_price=entry, quantity=20/entry,
                        entry_order={}, open_ts=now.isoformat(), config=shadow._exit_config(),
                        client=client, logger=logger, entry_atr=atr, atr_timeframe='1m', atr_period=14,
                    )
                    for index, price in enumerate(prices):
                        if restart:
                            shadow._save_state()
                            shadow = CircuitBreakerShadow(root, config, logger, None)
                        position = shadow.open_positions[0]
                        self.assertEqual(position.shadow_kind, 'REAL_A_CB_SHADOW')
                        self.assertAlmostEqual(position._active_profit_lock_economic_floor(), real._active_profit_lock_economic_floor())
                        stamp = (now + timedelta(minutes=index + 1)).isoformat()
                        client.set_price(price)
                        real.on_tick(price, market_ts=stamp)
                        shadow.on_tick(price, stamp)
                        for field in ('status', 'profit_lock_step', 'profit_lock_stop', 'effective_stop', 'trailing_active', 'exit_reason', 'exit_trigger_price'):
                            self.assertEqual(getattr(position, field), getattr(real, field), field)
                        self.assertEqual(position.shadow_kind, 'REAL_A_CB_SHADOW')
                    self.assertEqual(real.exit_reason, reason)
                    records = shadow.ledger.load()
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0]['shadow_kind'], 'REAL_A_CB_SHADOW')
                    self.assertTrue(records[0]['phantom'])

    def test_disabled_floor_remains_disabled(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config()
            shadow = CircuitBreakerShadow(root, config, JsonlLogger(root, config), None)
            signal = EntrySignal('SOLUSDT', 100, datetime.now(timezone.utc).isoformat(), 1788564900000, .03, '1m', 14)
            shadow.on_approved_real_a_signal(signal, None)
            self.assertIsNone(shadow.open_positions[0]._active_profit_lock_economic_floor())
