"""Fixed FAST_DROP rules shared by the causal replay and forward shadow."""

MINUTE_MS = 60_000
FAST_LOSS_PCT = 0.50
FAST_VELOCITY_5M = -0.10
FAST_EMA = frozenset({'SHO', 'BEA'})


def fast_drop_boundary(at_ms: int) -> int:
    return at_ms - at_ms % MINUTE_MS + MINUTE_MS


def loss_reached(entry: float, price: float) -> bool:
    return (price-entry)/entry*100 <= -FAST_LOSS_PCT + 1e-12


def fast_drop_values(entry: float, reference: float | None) -> tuple[float, float | None]:
    target = entry * (1-FAST_LOSS_PCT/100)
    velocity = (target/reference-1)*100/5 if reference else None
    return target, velocity


def fast_drop_allowed(velocity: float | None, ema_context: str) -> bool:
    return velocity is not None and velocity <= FAST_VELOCITY_5M+1e-12 and ema_context in FAST_EMA


def normal_stop_precedes_fast(normal_stop: float | None, target: float) -> bool:
    # On a downward move the higher threshold wins; ties go to the normal stop.
    return normal_stop is not None and normal_stop >= target


def closed_before(close_ms: int | None, evaluated_at_ms: int) -> bool:
    # Binance close_time is the final millisecond of the candle, not its next boundary.
    return close_ms is not None and close_ms < evaluated_at_ms
