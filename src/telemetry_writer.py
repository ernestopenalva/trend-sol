from __future__ import annotations

import json
import queue
import threading
from pathlib import Path
from typing import Any, Dict, Optional

from src.logging_utils import JsonlLogger


class TelemetryWriter:
    STREAM_PATH_KEYS = {
        "trough_event": "trough_events_file",
        "position_snapshot": "position_snapshots_file",
        "rejected_signal": "rejected_signals_file",
    }

    def __init__(self, project_root: Path, config: Dict[str, Any], logger: JsonlLogger) -> None:
        cfg = config.get("instrumentation") if isinstance(config.get("instrumentation"), dict) else {}
        self.enabled = bool(cfg.get("enabled", False))
        self.logger = logger
        self.paths = {
            stream: project_root / str(cfg.get(key, f"data/telemetry/{stream}s.jsonl"))
            for stream, key in self.STREAM_PATH_KEYS.items()
        }
        self._queue: queue.Queue[Optional[tuple[str, Dict[str, Any]]]] = queue.Queue(
            maxsize=max(1, int(cfg.get("queue_max_size", 10000)))
        )
        self._thread: Optional[threading.Thread] = None
        self._dropped = 0
        self._write_errors = 0

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._thread = threading.Thread(target=self._run, name="trend-sol-telemetry", daemon=True)
        self._thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        if not self._thread:
            return
        try:
            self._queue.put(None, timeout=max(0.0, timeout_seconds))
        except queue.Full:
            self._warn("telemetry_stop_queue_full", dropped_count=self._dropped)
            return
        self._thread.join(timeout=max(0.0, timeout_seconds))
        if self._thread.is_alive():
            self._warn("telemetry_stop_timeout", pending=self._queue.qsize())
        self._thread = None

    def submit(self, stream: str, event: Dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        if stream not in self.paths:
            self._warn("telemetry_unknown_stream", stream=stream)
            return False
        try:
            self._queue.put_nowait((stream, dict(event)))
            return True
        except queue.Full:
            self._dropped += 1
            self._warn("telemetry_queue_full", stream=stream, dropped_count=self._dropped)
            return False
        except Exception as exc:
            self._dropped += 1
            self._warn("telemetry_enqueue_failed", stream=stream, error=str(exc), dropped_count=self._dropped)
            return False

    def flush(self) -> None:
        if self.enabled:
            self._queue.join()

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                stream, event = item
                self._append_jsonl(self.paths[stream], event)
            except Exception as exc:
                self._write_errors += 1
                stream = item[0] if item else "unknown"
                self._warn(
                    "telemetry_write_failed",
                    stream=stream,
                    error=str(exc),
                    write_error_count=self._write_errors,
                )
            finally:
                self._queue.task_done()

    @staticmethod
    def _append_jsonl(path: Path, event: Dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _warn(self, event: str, **fields: Any) -> None:
        try:
            self.logger.system(event, **fields)
        except Exception:
            pass
