from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Iterable

from websocket import WebSocketApp

from src.logging_utils import JsonlLogger
from src.monitor.feed_observability import FeedObservability, utc_ms


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
        self._subscription_request_id = 1
        self.observability = FeedObservability(logger)
        self._connection_had_input = False
        self._disconnected_monotonic = None
        self._lag_before_disconnect = None
        self._disconnect_duration = None
        self._awaiting_reconnect_input = False
        self._disconnected_at = None

    @property
    def feed_health(self):
        """Read-only operational snapshot; never consulted by the strategies."""
        return self.observability.health()

    def run_forever(self) -> None:
        self.observability.start()
        try:
            self._run_connections()
        finally:
            self.observability.stop()

    def _run_connections(self) -> None:
        backoff_sequence = [1, 2, 4, 8, 10]
        backoff_index = 0
        while not self.stop_requested:
            url = f"{self.ws_url}/stream?streams={'/'.join(self.streams)}"
            self.connection_started_at = time.time()
            self._connection_had_input = False
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
            # Receiving valid market data ends the consecutive-failure streak.
            if self._connection_had_input:
                backoff_index = 0
            backoff = backoff_sequence[min(backoff_index, len(backoff_sequence) - 1)]
            self.observability.emit("websocket_reconnect_scheduled", backoff_seconds=backoff,
                                    **self.observability.health())
            time.sleep(backoff)
            backoff_index += 1

    def stop(self) -> None:
        self.stop_requested = True
        self.observability.stop()
        if self._app:
            self._app.close()

    def update_streams(self, streams: Iterable[str]) -> bool:
        updated = list(dict.fromkeys(str(stream) for stream in streams))
        if updated == self.streams:
            return False
        previous = set(self.streams)
        self.streams = updated
        added = [stream for stream in updated if stream not in previous]
        removed = [stream for stream in previous if stream not in set(updated)]
        self.logger.system(
            "websocket_streams_updated",
            streams=self.streams,
            added=added,
            removed=removed,
        )
        if self._app and self.status == "connected":
            try:
                self._send_subscription("SUBSCRIBE", added)
                self._send_subscription("UNSUBSCRIBE", removed)
            except Exception as exc:
                self.logger.system(
                    "websocket_stream_update_failed",
                    error=str(exc),
                )
                self._app.close()
        return True

    def _send_subscription(self, method: str, streams: list[str]) -> None:
        if not streams or not self._app:
            return
        request_id = self._subscription_request_id
        self._subscription_request_id += 1
        self._app.send(
            json.dumps(
                {
                    "method": method,
                    "params": streams,
                    "id": request_id,
                }
            )
        )

    def _watchdog(self, app: WebSocketApp) -> None:
        # Retired connection watchdogs must not close a later/new connection.
        while not self.stop_requested and self._app is app:
            time.sleep(60)
            if self._app is not app:
                return
            if time.time() - self.connection_started_at > 23 * 60 * 60:
                self.logger.system("websocket_proactive_reconnect")
                app.close()
                return

    def _on_message(self, _app: WebSocketApp, message: str) -> None:
        receive_ms = time.time() * 1000
        started = time.perf_counter()
        observation = None
        failed = False
        try:
            payload = json.loads(message)
            stream = str(payload.get("stream", ""))
            data = payload.get("data") or {}
            observation = self.observability.received(stream, data, receive_ms)
            if observation["exchange_ts"] is not None:
                self._connection_had_input = True
            self.on_event(stream, data)
        except Exception:
            failed = True
            raise
        finally:
            duration = (time.perf_counter() - started) * 1000
            self.observability.completed(duration, failed)
            if observation is not None:
                if observation["lag_ms"] is not None and observation["lag_ms"] > 5000:
                    self.observability.emit("websocket_input_lag", **observation,
                                            callback_ms=duration, callback_failed=failed)
                if self._awaiting_reconnect_input and observation["exchange_ts"] is not None:
                    self._awaiting_reconnect_input = False
                    self.observability.emit("websocket_reconnect_first_input", **observation,
                        callback_ms=duration, callback_failed=failed,
                        lag_before_disconnect_ms=self._lag_before_disconnect,
                        lag_after_reconnect_ms=observation["lag_ms"],
                        disconnect_duration_seconds=self._disconnect_duration)

    def _mark_disconnected(self):
        if self._disconnected_monotonic is None:
            self._disconnected_monotonic = time.monotonic()
            self._disconnected_at = utc_ms(time.time() * 1000)
            self._lag_before_disconnect = self.observability.health()["current_lag_ms"]
        return {**self.observability.health(), "lag_before_disconnect_ms": self._lag_before_disconnect,
                "disconnect_at": self._disconnected_at, "lag_after_reconnect_ms": None,
                "disconnect_duration_seconds": time.monotonic() - self._disconnected_monotonic}

    def _on_error(self, _app: WebSocketApp, error: Exception) -> None:
        self.status = "error"
        fields = self._mark_disconnected()
        self.observability.emit("websocket_error", error=str(error),
                                ping_pong_timeout="ping/pong timed out" in str(error).lower(), **fields)

    def _on_close(self, _app: WebSocketApp, status_code: int, message: str) -> None:
        self.status = "closed"
        self.observability.emit("websocket_closed", status_code=status_code, close_message=message,
                                **self._mark_disconnected())

    def _on_open(self, _app: WebSocketApp) -> None:
        self.status = "connected"
        reconnect = self._disconnected_monotonic is not None
        self._disconnect_duration = (time.monotonic() - self._disconnected_monotonic) if reconnect else None
        self._awaiting_reconnect_input = reconnect
        self._disconnected_monotonic = None
        self.observability.emit("websocket_connected", streams=self.streams, reconnect=reconnect,
            disconnect_at=self._disconnected_at if reconnect else None,
            reconnect_at=utc_ms(time.time()*1000),
            lag_before_disconnect_ms=self._lag_before_disconnect, lag_after_reconnect_ms=None,
            disconnect_duration_seconds=self._disconnect_duration, **self.observability.health())
