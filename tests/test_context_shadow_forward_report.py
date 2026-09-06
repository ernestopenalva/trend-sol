from __future__ import annotations

import unittest

from datetime import datetime, timezone

from tools.context_shadow_forward_report import _normalized_realized_stats, _open_positions


class ContextShadowForwardReportTests(unittest.TestCase):
    def test_real_a_excludes_its_phantoms_but_context_shadow_keeps_virtual_positions(self) -> None:
        real_state = [
            *[{"status": "OPEN", "label": "B", "phantom": False} for _ in range(5)],
            *[{"status": "OPEN", "label": "B", "phantom": True} for _ in range(5)],
        ]
        context_state = {"positions": [{"status": "OPEN", "phantom": True} for _ in range(5)]}
        self.assertEqual(len(_open_positions(real_state, real_a=True)), 5)
        self.assertEqual(len(_open_positions(context_state, real_a=False)), 5)

    def test_normalized_real_a_uses_trigger_and_shadow_keeps_recorded_phantom_exit(self) -> None:
        closed = datetime(2026, 9, 1, tzinfo=timezone.utc).isoformat()
        base = {
            "entry_price": 100.0,
            "exit_price": 105.0,
            "exit_trigger_price": 102.0,
            "qty": 1.0,
            "position_notional_usdt": 100.0,
            "estimated_fees_pct": 0.2,
            "closed_at": closed,
        }
        real = _normalized_realized_stats([base], 100.0, real_a=True)
        shadow = _normalized_realized_stats([base], 100.0, real_a=False)
        self.assertIsNotNone(real)
        self.assertIsNotNone(shadow)
        self.assertAlmostEqual(real["net"], 1.8)
        self.assertAlmostEqual(real["drawdown"], 0.0)
        self.assertAlmostEqual(shadow["net"], 4.8)
        self.assertAlmostEqual(shadow["drawdown"], 0.0)
