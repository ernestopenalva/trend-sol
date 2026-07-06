from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Iterable

from websocket import WebSocketApp

from src.logging_utils import JsonlLogger


class WSManager:
    def __init__(
        self,
        ws_url: str,
        streams: Iterable[str],
        logger: JsonlLogger,
        on_event: Callable[[str, Dict[str, Any]], None],
        ping_interval_seconds: int = 180,
        ping_timeout_seconds: int = 30,
    ) -> None:
        self.ws_url = ws_url.rstrip("/")
        self.streams = list(streams)
        self.logger = logger
        self.on_event = on_event
        self.stop_requested = False
        self.connection_started_at = 0.0
        self.status = "starting"
        self.ping_interval_seconds = int(ping_interval_seconds)
        self.ping_timeout_seconds = int(ping_timeout_seconds)
        self._app: WebSocketApp | None = None

    def run_forever(self) -> None:
        backoff_sequence = [1, 2, 5, 10, 30, 60]
        backoff_index = 0
        while not self.stop_requested:
            url = f"{self.ws_url}/stream?streams={'/'.join(self.streams)}"
            self.connection_started_at = time.time()
            app = WebSocketApp(
                url,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
                on_open=self._on_open,
            )
            self._app = app
            watchdog = threading.Thread(target=self._watchdog, args=(app,), daemon=True)
            watchdog.start()
            app.run_forever(
                ping_interval=self.ping_interval_seconds,
                ping_timeout=self.ping_timeout_seconds,
            )
            self._app = None
            if self.stop_requested:
                break
            self.status = "reconnecting"
            backoff = backoff_sequence[min(backoff_index, len(backoff_sequence) - 1)]
            self.logger.system("websocket_reconnect_scheduled", backoff_seconds=backoff)
            time.sleep(backoff)
            backoff_index += 1

    def stop(self) -> None:
        self.stop_requested = True
        if self._app:
            self._app.close()

    def _watchdog(self, app: WebSocketApp) -> None:
        while not self.stop_requested:
            time.sleep(60)
            if time.time() - self.connection_started_at > 23 * 60 * 60:
                self.logger.system("websocket_proactive_reconnect")
                app.close()
                return

    def _on_message(self, _app: WebSocketApp, message: str) -> None:
        payload = json.loads(message)
        stream = str(payload.get("stream", ""))
        data = payload.get("data") or {}
        self.on_event(stream, data)

    def _on_error(self, _app: WebSocketApp, error: Exception) -> None:
        self.status = "error"
        self.logger.system("websocket_error", error=str(error))

    def _on_close(self, _app: WebSocketApp, status_code: int, message: str) -> None:
        self.status = "closed"
        self.logger.system("websocket_closed", status_code=status_code, close_message=message)

    def _on_open(self, _app: WebSocketApp) -> None:
        self.status = "connected"
        self.logger.system("websocket_connected", streams=self.streams)
