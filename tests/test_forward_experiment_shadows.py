from __future__ import annotations

import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.monitor.forward_experiment_shadows import ExperimentalRiskShadow, PolicyShadow
from src.monitor.market_context import classify_ema_context, classify_macd_context
from src.monitor.entry_engine import EntrySignal
from tests.test_circuit_breaker_shadow import _config


class ContextClassificationTests(unittest.TestCase):
    def test_ema_precedence_and_partial_contexts(self) -> None:
        self.assertEqual(classify_ema_context(3, 2, 1, "UP", "UP", "UP"), "LON")
        self.assertEqual(classify_ema_context(1, 2, 3, "DOWN", "DOWN", "DOWN"), "SHO")
        self.assertEqual(classify_ema_context(1, 2, 3, "UP", "UP", "DOWN"), "BUL")
        self.assertEqual(classify_ema_context(3, 2, 1, "UP", "DOWN", "UP"), "MUP")
        self.assertEqual(classify_ema_context(3, 2, 1, "DOWN", "UP", "DOWN"), "MDO")
        self.assertEqual(classify_ema_context(3, 2, 1, "DOWN", "DOWN", "UP"), "BEA")
        self.assertEqual(classify_ema_context(3, 2, 1, "FLAT", "FLAT", "FLAT"), "MIX")

    def test_macd_uses_line_zero_position_and_t_minus_one_direction(self) -> None:
        self.assertEqual(classify_macd_context(0.2, 0.1), "BU+")
        self.assertEqual(classify_macd_context(0.1, 0.2), "BU-")
        self.assertEqual(classify_macd_context(-0.1, -0.2), "BE+")
        self.assertEqual(classify_macd_context(-0.2, -0.1), "BE-")
        self.assertEqual(classify_macd_context(0.0, -0.1), "UNAVAILABLE")


class PolicyShadowTests(unittest.TestCase):
    def _shadow(self, root: Path, policy: str) -> PolicyShadow:
        config = deepcopy(_config())
        key = "policy"
        config["instrumentation"][key] = {
            "enabled": True, "accept_new_entries": True, "initial_capital_usdt": 100,
            "max_open_positions": 5, "state_file": "data/state/policy.json",
            "ledger_file": "data/trades/policy.jsonl", "events_file": "data/telemetry/policy.jsonl",
        }
        return PolicyShadow(root, config, JsonlLogger(root, config), None, settings_key=key,
                            strategy="POLICY", pair_prefix="policy", policy=policy,
                            cohort_started_at="2026-09-27T20:00:00+00:00")

    def test_macd_shadow_blocks_only_bu_minus_among_available_quadrants(self) -> None:
        with TemporaryDirectory() as tmp:
            shadow = self._shadow(Path(tmp), "MACD_BU_MINUS")
            for label in ("BU+", "BE+", "BE-"):
                self.assertTrue(shadow._entry_policy({"macd_context": label})[0])
            self.assertFalse(shadow._entry_policy({"macd_context": "BU-"})[0])

    def test_ema_macd_matrix_is_deliberately_restrictive(self) -> None:
        with TemporaryDirectory() as tmp:
            shadow = self._shadow(Path(tmp), "EMA_MACD")
            for ema in ("LON", "BUL", "BEA"):
                for macd in ("BU+", "BE+"):
                    self.assertTrue(shadow._entry_policy({"ema_context": ema, "macd_context": macd})[0])
                for macd in ("BU-", "BE-"):
                    self.assertFalse(shadow._entry_policy({"ema_context": ema, "macd_context": macd})[0])
            for ema in ("SHO", "MUP", "MDO", "MIX"):
                for macd in ("BU+", "BU-", "BE+", "BE-"):
                    self.assertFalse(shadow._entry_policy({"ema_context": ema, "macd_context": macd})[0])

    def test_policy_shadow_is_phantom_and_has_own_state(self) -> None:
        with TemporaryDirectory() as tmp:
            shadow = self._shadow(Path(tmp), "MACD_BU_MINUS")
            signal = EntrySignal("SOLUSDT", 100.0, "2026-09-27T20:01:00+00:00", 1_790_538_900_000, .2, "1m", 14)
            snapshot = {"tf_5m": {"macd_context": "BU+", "latest_open_at_ms": 1_790_538_600_000,
                                    "latest_closed_at_ms": 1_790_538_899_999}}
            self.assertTrue(shadow.on_approved_real_a_signal(signal, snapshot))
            self.assertTrue(shadow.open_positions[0].phantom)
            self.assertIsInstance(shadow.open_positions[0].client, __import__("src.position.phantom_execution", fromlist=["PhantomExecutionClient"]).PhantomExecutionClient)
            self.assertTrue(shadow.state_path.exists())


class RiskShadowTests(unittest.TestCase):
    def _shadow(self, root: Path, experiment: str) -> ExperimentalRiskShadow:
        config = deepcopy(_config())
        key = "risk_experiment"
        config["instrumentation"][key] = {
            "enabled": True, "accept_new_entries": True, "initial_capital_usdt": 100,
            "max_open_positions": 5, "state_file": "data/state/risk.json",
            "ledger_file": "data/trades/risk.jsonl", "events_file": "data/telemetry/risk.jsonl",
        }
        return ExperimentalRiskShadow(root, config, JsonlLogger(root, config), None,
            settings_key=key, strategy=experiment, pair_prefix="risk", experiment=experiment,
            cohort_started_at="2026-09-27T20:00:00+00:00")

    @staticmethod
    def _signal(price: float, source: int, minute: int) -> EntrySignal:
        return EntrySignal("SOLUSDT", price, f"2026-09-27T20:{minute:02d}:00+00:00", source, .2, "1m", 14)

    def test_elastic_hard_stop_survives_only_while_lon(self) -> None:
        with TemporaryDirectory() as tmp:
            shadow = self._shadow(Path(tmp), "HS_BULL_ELASTIC")
            lon = {"tf_5m": {"ema_context": "LON", "close": 100.0, "latest_closed_at_ms": 1_790_539_199_999}}
            shadow.on_approved_real_a_signal(self._signal(100, 1_790_538_900_000, 1), lon)
            shadow.on_tick(98.5, "2026-09-27T20:02:00+00:00")
            self.assertEqual(len(shadow.open_positions), 1)
            self.assertTrue(shadow.open_positions[0].hs_elastic)
            lost = {"tf_5m": {"ema_context": "BEA", "close": 98.4, "latest_closed_at_ms": 1_790_539_499_999}}
            shadow.on_closed_5m(lost)
            self.assertEqual(shadow.open_positions, [])
            self.assertEqual(shadow.closed_records[0]["exit_reason"], "HARD_STOP_ELASTIC_CONTEXT_LOST")

    def test_cluster_exit_requires_sho_and_closes_only_negative_neighbors(self) -> None:
        with TemporaryDirectory() as tmp:
            shadow = self._shadow(Path(tmp), "HS_BEAR_CLUSTER_EXIT")
            sho = {"tf_5m": {"ema_context": "SHO", "close": 100.0}}
            shadow.on_approved_real_a_signal(self._signal(100, 1_790_538_900_000, 1), sho)
            shadow.on_approved_real_a_signal(self._signal(99, 1_790_539_200_000, 6), sho)
            shadow.on_tick(98.5, "2026-09-27T20:07:00+00:00")
            self.assertEqual(shadow.open_positions, [])
            self.assertEqual({item["exit_reason"] for item in shadow.closed_records}, {"HARD_STOP", "HS_BEAR_CLUSTER_EXIT"})


if __name__ == "__main__":
    unittest.main()
