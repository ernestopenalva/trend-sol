from tools.be_off_cb_deterioration_study import (
    Crossing,
    counterfactual,
    first_crossing,
    select_threshold,
)
from tools.ge_replay_study import ReplayTrade
from tools.market_selection_study import MarketCandle


def _trade(trough=98.0, reason="HARD_STOP", net=-1.6):
    return ReplayTrade(0, 120_000, 100.0, 98.5, 101.0, trough, -1.5, net, reason)


def _candle(open_ms, open_, high, low, close):
    return MarketCandle(open_ms, open_ms + 59_999, open_, high, low, close, 1.0, 1)


def test_first_crossing_uses_loss_level_and_path():
    candles = [_candle(0, 100, 101, 99.4, 99.8), _candle(60_000, 99.8, 100, 98.9, 99.0)]
    assert first_crossing(_trade(), 0.5, candles, "HIGH_FIRST") == (60_000, 99.5)
    assert first_crossing(_trade(), 1.0, candles, "LOW_FIRST") == (120_000, 99.0)
    assert first_crossing(_trade(trough=99.2), 1.0, candles, "LOW_FIRST") is None


def _row(reason, velocity, net=-1.6):
    return Crossing("HIGH_FIRST", 0, 60_000, 1.0, 1.0, 100, 99, 1, 1, 1, {5: -1}, {5: velocity}, {5: 0}, "BUL", "BU-", reason, net)


def test_threshold_requires_sample_and_hs_dominance():
    rows = [_row("HS", -0.08) for _ in range(6)] + [_row("PL", -0.08) for _ in range(4)]
    assert select_threshold(rows, 5, 10) == (0.025, 0.6, 10)
    assert select_threshold(rows, 5, 11) is None


def test_counterfactual_counts_hs_and_sacrificed_winner():
    rows = [_row("HS", -0.08, -1.6), _row("PL", -0.08, 0.5)]
    result = counterfactual(rows, exit_cost_pct=0.2, notional=20)
    assert result["hs_avoided"] == 1
    assert result["winners_sacrificed"] == 1
    assert round(float(result["delta"]), 6) == -0.26
