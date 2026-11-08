"""Bounded public REST gap repair, outside the WebSocket decision thread."""
from threading import Event, Thread


class StructureRepair:
    def __init__(self, provider, client, logger, now_ms, settings):
        self.provider, self.client, self.logger, self.now_ms = provider, client, logger, now_ms
        self.poll = float(settings['poll_seconds'])
        self.grace = int(settings['close_grace_ms'])
        self.health_interval_ms = int(settings.get('health_log_seconds', 60)) * 1000
        self.last_health_ms = None
        if self.poll <= 0 or self.grace < 0 or self.health_interval_ms <= 0:
            raise ValueError('Invalid structure repair schedule')
        self.stopped = Event()
        self.thread = None
        self.ready = False

    def repair_once(self):
        for interval, buffer in [('15m', self.provider.wave_buffer),
                                 ('5m', self.provider.marola_buffers['marola_90']),
                                 ('1h', self.provider.buffer)]:
            now = self.now_ms()
            snapshot = buffer.snapshot(now, 'LIVE')
            missing_key = 'missing_hours' if interval == '1h' else 'missing_candles'
            missing = snapshot.get(missing_key, [])
            # Allow the closing WebSocket message to arrive before polling REST.
            if not missing or now < snapshot['window_end_ms'] + self.grace:
                continue
            try:
                rows = self.client.klines(symbol=self.provider.symbol, interval=interval,
                                          limit=buffer.rule.count + 2)
                received = self.now_ms()
                for row in rows:
                    if interval != '5m':
                        buffer.add_kline(row, received)
                    else:
                        self.provider.add_marola_kline(row, received)
                after = buffer.snapshot(received, 'LIVE')
                self.logger.system('structure_gap_repair', interval=interval,
                    received_ms=received, missing_before=missing,
                    missing_after=after.get(missing_key, []), status=after['status'])
            except Exception as exc:
                self.logger.system('structure_gap_repair_failed', interval=interval,
                                   error=type(exc).__name__)
        now = self.now_ms()
        if all(b.snapshot(now, 'LIVE')['status'] in ('OK', 'EQUAL_EXTREME') for b in
               (self.provider.wave_buffer, self.provider.marola_buffers['marola_90'])):
            self.ready = True
        if self.last_health_ms is None or now - self.last_health_ms >= self.health_interval_ms:
            # Sparse read-only evidence: never feeds back into an economic decision.
            layers = {}
            for name, buffer in [('tide', self.provider.buffer),
                                 ('wave', self.provider.wave_buffer),
                                 ('marola_90', self.provider.marola_buffers['marola_90'])]:
                snapshot = buffer.snapshot(now, 'LIVE')
                layers[name] = {key: snapshot.get(key) for key in
                    ('label', 'status', 'rule_version', 'undefined_reason', 'missing_candles', 'missing_hours',
                     'window_start_ms', 'window_end_ms', 'latest_closed_ms')}
            self.logger.system('structure_health', sampled_at_ms=now,
                               initial_warmup_complete=self.ready, **layers)
            self.last_health_ms = now

    def start(self):
        self.repair_once()
        def run():
            while not self.stopped.wait(self.poll):
                try:
                    self.repair_once()
                except Exception as exc:
                    self.logger.system('structure_repair_worker_failed', error=type(exc).__name__)
        self.thread = Thread(target=run, name='structure-repair', daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
