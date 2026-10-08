"""In-memory feed health; output is diagnostic, never a trading input."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone


def utc_ms(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).isoformat(timespec="milliseconds")


def quantiles(values):
    if not values:
        return {name: None for name in ("p50", "p90", "p99")}
    values = sorted(values)
    result = {}
    for name, q in (("p50", .5), ("p90", .9), ("p99", .99)):
        at = (len(values) - 1) * q
        low = int(at)
        result[name] = values[low] + (values[min(low + 1, len(values) - 1)] - values[low]) * (at - low)
    return result


class FeedObservability:
    INTERVAL_SECONDS = 10

    def __init__(self, logger):
        self.logger = logger
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread = None
        self.last_exchange_ts = None
        self.last_receive_at = None
        self.current_lag_ms = None
        self.stream_health = {}
        self.inputs = 0
        self.missing_ts = 0
        self.lags = []
        self.callbacks = []
        self.callback_errors = 0
        self.write_count = 0
        self.write_ms = 0.0
        self.write_errors = 0
        self.window_started = time.monotonic()

    def health(self):
        with self.lock:
            return {"last_exchange_ts": self.last_exchange_ts, "last_receive_at": self.last_receive_at,
                    "current_lag_ms": self.current_lag_ms}

    def received(self, stream, data, receive_ms):
        # aggTrade T is trade time, matching app._market_timestamp; kline E is
        # emission time (k.T is candle end, not input emission/arrival).
        raw = data.get("T") if stream.endswith("@aggTrade") else data.get("E")
        if raw is None:
            raw = data.get("E")
        try:
            exchange_ms = float(raw) if raw is not None else None
            exchange_ts = utc_ms(exchange_ms) if exchange_ms is not None else None
        except (TypeError, ValueError, OverflowError, OSError):
            exchange_ms = None
            exchange_ts = None
        lag = receive_ms - exchange_ms if exchange_ms is not None else None
        item = {"stream": stream, "exchange_ts": exchange_ts,
                "receive_at": utc_ms(receive_ms), "lag_ms": lag,
                "exchange_timestamp_source": "T" if stream.endswith("@aggTrade") and data.get("T") is not None else "E"}
        with self.lock:
            self.inputs += 1
            self.last_exchange_ts = item["exchange_ts"]
            self.last_receive_at = item["receive_at"]
            self.current_lag_ms = lag
            self.stream_health[stream] = item
            if lag is None:
                self.missing_ts += 1
            else:
                self.lags.append(lag)
        return item

    def completed(self, duration_ms, failed=False):
        with self.lock:
            self.callbacks.append(duration_ms)
            self.callback_errors += int(failed)

    def emit(self, event, **fields):
        started = time.perf_counter()
        failed = False
        try:
            self.logger.system(event, **fields)
        except Exception:
            # Diagnostics must not mask callback exceptions or affect decisions.
            failed = True
        finally:
            with self.lock:
                self.write_count += 1
                self.write_errors += int(failed)
                self.write_ms += (time.perf_counter() - started) * 1000

    def flush(self):
        now = time.monotonic()
        with self.lock:
            lags, callbacks = self.lags, self.callbacks
            record = {"emitted_at": utc_ms(time.time()*1000),
                      "window_seconds": now - self.window_started, "inputs": self.inputs,
                      "callbacks_completed": len(callbacks), "missing_exchange_ts": self.missing_ts,
                      "callback_errors": self.callback_errors, "telemetry_write_count": self.write_count,
                      "telemetry_write_ms": self.write_ms, "telemetry_write_errors": self.write_errors,
                      "last_exchange_ts": self.last_exchange_ts, "last_receive_at": self.last_receive_at,
                      "current_lag_ms": self.current_lag_ms,
                      "stream_health": dict(self.stream_health)}
            self.lags, self.callbacks = [], []
            self.inputs = self.missing_ts = self.callback_errors = self.write_count = self.write_errors = 0
            self.write_ms = 0.0
            self.window_started = now
        record.update(lag_ms=quantiles(lags), callback_ms=quantiles(callbacks))
        self.emit("websocket_input_metrics", **record)

    def _run(self):
        while not self.stop_event.wait(self.INTERVAL_SECONDS):
            self.flush()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="trend-sol-feed-metrics", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=1)

