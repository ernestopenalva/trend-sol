from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger


class LoggingUtilsTests(unittest.TestCase):
    def test_system_accepts_close_message_field_without_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            logger = JsonlLogger(
                Path(tmp),
                {
                    "logging": {
                        "console": False,
                        "system_log": "logs/system.log",
                    },
                    "console": {"mode": "human"},
                },
            )
            logger.system("websocket_closed", close_message="closed by host")
            content = (Path(tmp) / "logs" / "system.log").read_text(encoding="utf-8")
            self.assertIn("websocket_closed", content)
            self.assertIn("closed by host", content)


if __name__ == "__main__":
    unittest.main()
