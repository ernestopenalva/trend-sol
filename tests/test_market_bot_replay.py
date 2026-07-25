from __future__ import annotations

import unittest

from tools.market_bot_replay import (
    NullLogger,
    OpenReplayPosition,
    ReplayExecutionClient,
    SelectionTimeline,
    SignalEvent,
    _kline_payload,
    _process_candle,
    build_selection_timeline,
    filter_signals_by_quiet_period,
    generate_pipeline_signals,
    signal_repetition_stats,
)
from src.monitor.entry_engine import EntrySignal
from src.position.bot_full_engine import BotFullExitPosition
from tools.market_selection_study import MarketCandle


HOUR = 3_600_000
MINUTE = 60_000


def candle(open_ms: int, price: float, quote_volume: float = 1_000_000) -> MarketCandle:
    return MarketCandle(
        open_time_ms=open_ms,
        close_time_ms=open_ms + HOUR - 1,
        open=price,
        high=price * 1.01,
        low=price * 0.99,
        close=price,
        quote_volume=quote_volume,
        trades=100,
    )


class MarketBotReplayTests(unittest.TestCase):
    def test_selection_timeline_never_uses_future_decision(self) -> None:
        timeline = SelectionTimeline(
            {4 * HOUR: ("AAAUSDT",), 8 * HOUR: ("BBBUSDT",)},
            {4 * HOUR: {"AAAUSDT": 0}, 8 * HOUR: {"BBBUSDT": 0}},
        )

        self.assertEqual(timeline.selected(4 * HOUR - 1, 5), ())
        self.assertEqual(timeline.selected(7 * HOUR, 5), ("AAAUSDT",))
        self.assertEqual(timeline.selected(8 * HOUR, 5), ("BBBUSDT",))

    def test_selection_uses_closed_24h_and_7d_history(self) -> None:
        candles = {
            "AAAUSDT": [candle(index * HOUR, 100 + index) for index in range(200)],
            "BBBUSDT": [candle(index * HOUR, 200 - index * 0.1) for index in range(200)],
        }
        start = 7 * 24 * HOUR
        timeline = build_selection_timeline(
            candles,
            decision_interval_hours=4,
            min_quote_volume_usdt=1,
            top_count=1,
            start_ms=start,
            end_ms=199 * HOUR,
        )

        self.assertEqual(timeline.selected(196 * HOUR, 1), ("AAAUSDT",))

    def test_kline_payload_matches_real_entry_engine_contract(self) -> None:
        payload = _kline_payload(candle(0, 100))

        self.assertTrue(payload["k"]["x"])
        self.assertEqual(payload["k"]["t"], 0)
        self.assertEqual(float(payload["k"]["c"]), 100)

    def test_empty_pipeline_data_generates_no_signals(self) -> None:
        config = {
            "active_profile": "intraday",
            "trend": {"timeframe": "15m", "ema_period": 50, "ema_slope_lookback": 3},
            "entry": {
                "timeframe": "1m",
                "lookback_candles": 30,
                "atr_period": 14,
                "pullback_atr_multiplier": 0.8,
                "rsi_period": 14,
                "rsi_threshold": 55,
                "rsi_lookback_candles": 1,
                "volume_ma_candles": 5,
                "require_volume_drying": False,
            },
        }

        self.assertEqual(
            generate_pipeline_signals(
                config, {"SOLUSDT": []}, {"SOLUSDT": []}, 0, MINUTE
            ),
            [],
        )

    def test_intrabar_paths_bound_same_minute_profit_lock(self) -> None:
        low_first = self._position()
        high_first = self._position()
        minute = MarketCandle(
            open_time_ms=0,
            close_time_ms=MINUTE - 1,
            open=100,
            high=101,
            low=99,
            close=100.5,
            quote_volume=1_000,
            trades=10,
        )

        _process_candle([low_first], [], "SOLUSDT", minute, "LOW_FIRST", 0.2)
        high_trades = []
        _process_candle(
            [high_first], high_trades, "SOLUSDT", minute, "HIGH_FIRST", 0.2
        )

        self.assertEqual(low_first.position.status, "OPEN")
        self.assertEqual(high_first.position.status, "CLOSED")
        self.assertEqual(high_trades[0].exit_reason, "PROFIT_LOCK")

    def test_quiet_period_rearm_keeps_only_new_signal_episode(self) -> None:
        raw = [
            self._signal(1),
            self._signal(2),
            self._signal(3),
            self._signal(5),
            self._signal(9),
        ]

        one_quiet = filter_signals_by_quiet_period(raw, 1)
        three_quiet = filter_signals_by_quiet_period(raw, 3)

        self.assertEqual([item.boundary_ms // MINUTE for item in one_quiet], [1, 5, 9])
        self.assertEqual([item.boundary_ms // MINUTE for item in three_quiet], [1, 9])

    def test_signal_repetition_stats_are_per_symbol(self) -> None:
        raw = [
            self._signal(1, "SOLUSDT"),
            self._signal(2, "SOLUSDT"),
            self._signal(10, "SOLUSDT"),
            self._signal(2, "BTCUSDT"),
            self._signal(4, "BTCUSDT"),
        ]

        stats = signal_repetition_stats(raw)

        self.assertEqual(stats["comparisons"], 3)
        self.assertEqual(stats["consecutive"], 1)
        self.assertEqual(stats["within_5m"], 2)

    @staticmethod
    def _signal(minute: int, symbol: str = "SOLUSDT") -> SignalEvent:
        return SignalEvent(
            boundary_ms=minute * MINUTE,
            symbol=symbol,
            signal=EntrySignal(
                symbol=symbol,
                price=100,
                ts="2026-01-01T00:00:00+00:00",
                source_candle_open_time=(minute - 1) * MINUTE,
                entry_atr=0.2,
                atr_timeframe="1m",
                atr_period=14,
            ),
        )

    @staticmethod
    def _position() -> OpenReplayPosition:
        client = ReplayExecutionClient(0)
        position = BotFullExitPosition(
            pair_id="test",
            symbol="SOLUSDT",
            entry_price=100,
            quantity=0.2,
            entry_order={},
            open_ts="2026-01-01T00:00:00+00:00",
            config={
                "hard_stop": {"enabled": True, "stop_pct": 2},
                "review_stop_pct": 30,
                "breakeven": {
                    "mode": "atr",
                    "trigger_atr": 100,
                    "offset_atr": 0.1,
                },
                "profit_lock": {
                    "mode": "atr",
                    "steps": [{"trigger_atr": 5, "lock_atr": 1.5}],
                },
                "trailing": {
                    "mode": "atr",
                    "activation_atr": 100,
                    "gap_atr": 5,
                },
            },
            client=client,  # type: ignore[arg-type]
            logger=NullLogger(),  # type: ignore[arg-type]
            entry_atr=0.2,
            atr_timeframe="1m",
            atr_period=14,
            position_notional_usdt=20,
        )
        return OpenReplayPosition(position, client, 0, 20)


if __name__ == "__main__":
    unittest.main()
