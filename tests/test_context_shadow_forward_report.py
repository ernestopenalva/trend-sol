from __future__ import annotations

import unittest

from tools.context_shadow_forward_report import _open_positions


class ContextShadowForwardReportTests(unittest.TestCase):
    def test_real_a_excludes_its_phantoms_but_context_shadow_keeps_virtual_positions(self) -> None:
        real_state = [
            *[{"status": "OPEN", "label": "B", "phantom": False} for _ in range(5)],
            *[{"status": "OPEN", "label": "B", "phantom": True} for _ in range(5)],
        ]
        context_state = {"positions": [{"status": "OPEN", "phantom": True} for _ in range(5)]}
        self.assertEqual(len(_open_positions(real_state, real_a=True)), 5)
        self.assertEqual(len(_open_positions(context_state, real_a=False)), 5)
