"""Sparse HS diagnostics outside financial journals/checkpoints.

Failures here never disable a shadow. Deduplication is memory-only; after
restart a relevant observation can repeat, but no financial event is replayed.
"""
from datetime import datetime, timezone
import time


def observe(owner, intelligence, position, market_ts, decision, *, conditions=None,
            values=None, reasons=(), context=None, dedup_key=None, asof_ms=None):
    started = time.perf_counter()
    try:
        moment = datetime.fromisoformat(market_ts) if isinstance(market_ts, str) else market_ts
        stamp = int(moment.timestamp() * 1000) if moment else None
        context = context or {}
        close_ms = context.get('latest_closed_at_ms')
        available_at = asof_ms if asof_ms is not None else stamp
        expected = available_at // 300_000 * 300_000 - 1 if available_at is not None else None
        freshness = ('UNAVAILABLE' if close_ms is None or expected is None else 'FRESH' if close_ms == expected
                     else 'STALE' if close_ms < expected else 'NOT_CAUSAL')
        pair = getattr(position, 'pair_id', None)
        identity = (intelligence, pair)
        signature = (decision, tuple(reasons), dedup_key)
        cache = getattr(owner, '_hs_observation_cache', None)
        if cache is None:
            cache = owner._hs_observation_cache = {}
        if cache.get(identity) == signature:
            return
        event = {'event': 'HS_INTELLIGENCE_EVALUATION', 'ts': moment.isoformat() if moment else None,
                 'market_ts': moment.isoformat() if moment else None,
                 'logged_at': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                 'arm': owner.shadow_kind, 'intelligence': intelligence, 'pair_id': pair,
                 'source_candle_open_time': getattr(position, 'source_candle_open_time', None),
                 'decision': decision, 'reasons': list(reasons), 'conditions': conditions or {},
                 'values': values or {}, 'context': context,
                 'ema_freshness': freshness, 'ema_expected_closed_at_ms': expected,
                 'ema_latest_closed_at_ms': close_ms,
                 'ema_age_ms': stamp - close_ms if close_ms is not None and stamp is not None else None,
                 'diagnostic_only': True}
        event['context_availability_asof_ms'] = available_at
        telemetry = getattr(owner, 'telemetry', None)
        # Async stream when enabled; retain diagnostics even if disabled/full.
        submitted = bool(telemetry and telemetry.submit('hs_intelligence', event))
        if not submitted:
            owner.logger.decision(event)
        cache[identity] = signature
        if len(cache) > 4096:
            cache.pop(next(iter(cache)))
        owner._hs_observation_emitted = getattr(owner, '_hs_observation_emitted', 0) + 1
    except Exception:
        owner._hs_observation_errors = getattr(owner, '_hs_observation_errors', 0) + 1
    finally:
        owner._hs_observation_ms = getattr(owner, '_hs_observation_ms', 0.0) + (time.perf_counter() - started) * 1000
