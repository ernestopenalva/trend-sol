from types import SimpleNamespace

from tools.be_off_cb_fast_drop_systemic_replay import _fast_decision
from tools.market_selection_study import MarketCandle


def _minute(boundary: int, close: float) -> MarketCandle:
    return MarketCandle(boundary - 60_000, boundary - 1, close, close, close, close, 1.0, 1)


def test_fixed_fast_drop_requires_loss_velocity_and_bearish_ema():
    position = SimpleNamespace(entry_price=100.0)
    boundary = 600_000
    minutes = {boundary - 300_000: _minute(boundary - 300_000, 100.0)}
    bearish = [(boundary - 60_001, "BEA", "BU-")]
    eligible, target, ema_context, velocity = _fast_decision(
        position, boundary, 99.4, 100.0, minutes, bearish
    )
    assert eligible is True
    assert target == 99.5
    assert ema_context == "BEA"
    assert round(float(velocity), 6) == -0.1

    bullish = [(boundary - 60_001, "LON", "BU-")]
    assert _fast_decision(position, boundary, 99.4, 100.0, minutes, bullish)[0] is False


def test_fixed_fast_drop_can_retry_below_loss_level_until_reference_available():
    position = SimpleNamespace(entry_price=100.0)
    boundary = 600_000
    minutes = {boundary - 300_000: _minute(boundary - 300_000, 100.0)}
    contexts = [(boundary - 60_001, "SHO", "BE-")]
    assert _fast_decision(position, boundary, 99.6, 100.0, minutes, contexts)[0] is False
    assert _fast_decision(position, boundary, 99.4, 99.4, {}, contexts)[0] is False
    assert _fast_decision(position, boundary, 99.4, 99.4, minutes, contexts)[0] is True
