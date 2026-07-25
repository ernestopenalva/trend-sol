from __future__ import annotations

import json
import unittest
from unittest.mock import Mock

from src.monitor.ws_manager import WSManager


class WSManagerTests(unittest.TestCase):
    def test_stream_update_subscribes_without_reconnecting(self):
        logger = Mock()
        manager = WSManager(
            "wss://example.test",
            ["solusdt@aggTrade", "btcusdt@aggTrade"],
            logger,
            Mock(),
        )
        app = Mock()
        manager._app = app
        manager.status = "connected"

        changed = manager.update_streams(
            ["solusdt@aggTrade", "ethusdt@aggTrade"]
        )

        self.assertTrue(changed)
        messages = [json.loads(call.args[0]) for call in app.send.call_args_list]
        self.assertEqual(messages[0]["method"], "SUBSCRIBE")
        self.assertEqual(messages[0]["params"], ["ethusdt@aggTrade"])
        self.assertEqual(messages[1]["method"], "UNSUBSCRIBE")
        self.assertEqual(messages[1]["params"], ["btcusdt@aggTrade"])
        app.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
