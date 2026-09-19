from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.monitor.entry_engine import EntrySignal
from src.monitor.ladder_shadow import RealALadderShadow
from src.monitor.circuit_breaker_shadow import CircuitBreakerShadow
from src.position.phantom_execution import PhantomExecutionClient


class LadderShadowTests(unittest.TestCase):
    def test_be030_changes_only_be_economic_floor(self) -> None:
        with TemporaryDirectory() as tmp:
            shadow = _shadow(Path(tmp), "be030_shadow", "BE030_SHADOW", "BE030")
            shadow.on_signal(_signal())
            position = shadow.open_positions[0]
            shadow.on_tick(100.8, "2026-10-01T00:01:00+00:00")
            self.assertAlmostEqual(position.be_net_floor or 0, 100.3)
            self.assertEqual(position.hard_stop_pct, 1.5)
            self.assertEqual(position.profit_lock_atr_steps[0]["trigger_atr"], 5)
            self.assertIsInstance(position.client, PhantomExecutionClient)

    def test_be_off_keeps_profit_lock_and_trailing_while_never_arming_be(self) -> None:
        with TemporaryDirectory() as tmp:
            shadow = _shadow(Path(tmp), "be_off_shadow", "BE_OFF_SHADOW", "BE_OFF")
            shadow.on_signal(_signal())
            position = shadow.open_positions[0]
            shadow.on_tick(101.1, "2026-10-01T00:01:00+00:00")
            self.assertIsNone(position.breakeven_stop)
            self.assertEqual(position.breakeven_mode, "off")
            self.assertEqual(position.profit_lock_step, "PL1")
            shadow.on_tick(102.1, "2026-10-01T00:02:00+00:00")
            self.assertTrue(position.trailing_active)

    def test_two_variants_admit_the_same_opportunity_independently(self) -> None:
        with TemporaryDirectory() as tmp:
            root=Path(tmp); a=_shadow(root,"be030_shadow","BE030_SHADOW","BE030"); b=_shadow(root,"be_off_shadow","BE_OFF_SHADOW","BE_OFF")
            self.assertTrue(a.on_signal(_signal())); self.assertTrue(b.on_signal(_signal()))
            self.assertNotEqual(a.open_positions[0].pair_id,b.open_positions[0].pair_id)
            self.assertNotEqual(a.ledger.path,b.ledger.path)

    def test_be_off_cb_uses_its_own_be_off_ladder(self) -> None:
        with TemporaryDirectory() as tmp:
            root=Path(tmp); cfg=_config("be_off_cb_shadow")
            cfg["instrumentation"]["be_off_cb_shadow"]["initial_capital_usdt"]=100
            cb=CircuitBreakerShadow(root,cfg,JsonlLogger(root,cfg),None,settings_key="be_off_cb_shadow",
                strategy="BE_OFF_CB_SHADOW",shadow_kind="BE_OFF_CB_SHADOW",pair_prefix="beoffcb",be_off=True,
                cohort_started_at="2026-10-01T00:00:00+00:00")
            self.assertTrue(cb.on_approved_real_a_signal(_signal(), None))
            position=cb.open_positions[0]; cb.on_tick(101.1,"2026-10-01T00:01:00+00:00")
            self.assertEqual(position.breakeven_mode,"off")
            self.assertEqual(position.profit_lock_step,"PL1")

def _signal() -> EntrySignal:
    return EntrySignal("SOLUSDT",100.,"2026-10-01T00:00:00+00:00",1_800_000_000_000,.2,"1m",14)

def _shadow(root: Path, key: str, strategy: str, variant: str) -> RealALadderShadow:
    cfg=_config(key)
    return RealALadderShadow(root,cfg,JsonlLogger(root,cfg),None,settings_key=key,strategy=strategy,shadow_kind=strategy,pair_prefix=key,variant=variant,cohort_started_at="2026-10-01T00:00:00+00:00")

def _config(key: str) -> dict:
    return {"symbol":"SOLUSDT","capital":{"operational_balance_usdt":100,"trade_size_pct":20,"max_open_positions":5},"trend":{"timeframe":"15m","ema_period":20,"ema_slope_lookback":3},"trend_gate":{"mode":"ge30","candle_interval":"5m","lookback_candles":3,"sync":{"enabled":False}},"entry":{"timeframe":"1m","atr_period":14,"max_entries_per_candle":1,"admission_candle_interval":"5m","entry_spacing_atr":1},"risk":{"hard_stop":{"enabled":True,"stop_pct":1.5},"no_progress":{"enabled":False},"breakeven":{"mode":"atr","trigger_atr":3,"offset_atr":.1},"profit_lock":{"mode":"atr","economic_floor":{"enabled":True,"net_margin_pct":.05},"steps":[{"trigger_atr":5,"lock_atr":1.5},{"trigger_atr":8,"lock_atr":3},{"trigger_atr":12,"lock_atr":6}]},"trailing":{"mode":"atr","activation_atr":10,"gap_atr":5}},"fees":{"enabled":True,"taker_fee_pct":.1},"ladder":{"be_net_margin_pct":.05,"be_activation_buffer_atr":.5},"logging":{"console":False},"instrumentation":{key:{"enabled":True,"accept_new_entries":True,"max_open_positions":5,"state_file":f"data/state/{key}.json","ledger_file":f"data/trades/{key}.jsonl"}}}
