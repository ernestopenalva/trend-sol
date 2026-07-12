from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.logging_utils import JsonlLogger
from src.telemetry_writer import TelemetryWriter


class TelemetryWriterTests(unittest.TestCase):
    def test_writes_each_stream_append_only(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config()
            logger = JsonlLogger(root, config)
            writer = TelemetryWriter(root, config, logger)
            writer.start()

            self.assertTrue(writer.submit("trough_event", {"position_id": 1, "price": 99}))
            self.assertTrue(writer.submit("position_snapshot", {"position_id": 1, "price": 101}))
            self.assertTrue(writer.submit("rejected_signal", {"reason": "BLOCKED_MAX_POSITIONS"}))
            writer.flush()
            writer.stop()

            self.assertEqual(_read_jsonl(root / "data/telemetry/trough_events.jsonl")[0]["price"], 99)
            self.assertEqual(_read_jsonl(root / "data/telemetry/position_snapshots.jsonl")[0]["price"], 101)
            self.assertEqual(
                _read_jsonl(root / "data/telemetry/rejected_signals.jsonl")[0]["reason"],
                "BLOCKED_MAX_POSITIONS",
            )

    def test_write_failure_is_logged_and_does_not_stop_writer(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config()
            logger = JsonlLogger(root, config)
            writer = TelemetryWriter(root, config, logger)
            writer.paths["trough_event"] = root
            writer.start()

            self.assertTrue(writer.submit("trough_event", {"price": 99}))
            writer.flush()
            self.assertTrue(writer.submit("position_snapshot", {"price": 101}))
            writer.flush()
            writer.stop()

            system_log = (root / "logs/system.log").read_text(encoding="utf-8")
            self.assertIn("telemetry_write_failed", system_log)
            self.assertEqual(_read_jsonl(root / "data/telemetry/position_snapshots.jsonl")[0]["price"], 101)

    def test_full_queue_drops_only_telemetry(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = _config()
            config["instrumentation"]["queue_max_size"] = 1
            logger = JsonlLogger(root, config)
            writer = TelemetryWriter(root, config, logger)

            self.assertTrue(writer.submit("trough_event", {"price": 99}))
            self.assertFalse(writer.submit("trough_event", {"price": 98}))
            writer.start()
            writer.flush()
            writer.stop()

            system_log = (root / "logs/system.log").read_text(encoding="utf-8")
            self.assertIn("telemetry_queue_full", system_log)
            self.assertEqual(len(_read_jsonl(root / "data/telemetry/trough_events.jsonl")), 1)


def _config() -> dict:
    return {
        "instrumentation": {
            "enabled": True,
            "queue_max_size": 10,
            "trough_events_file": "data/telemetry/trough_events.jsonl",
            "position_snapshots_file": "data/telemetry/position_snapshots.jsonl",
            "rejected_signals_file": "data/telemetry/rejected_signals.jsonl",
        },
        "logging": {"console": False, "system_log": "logs/system.log"},
        "console": {"mode": "human"},
    }


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


if __name__ == "__main__":
    unittest.main()
