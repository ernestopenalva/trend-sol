from __future__ import annotations

import unittest

from tools.donchian_portfolio_replay import (
    PortfolioConfig,
    _ordered_candidates,
    EntryCandidate,
    replay_portfolio,
    summarize,
)
from tools.market_selection_study import HOUR_MS, MarketCandle


def candle(
    index: int,
    open_: float,
    high: float,
    low: float,
    close: float,
) -> MarketCandle:
    return MarketCandle(
        open_time_ms=index * HOUR_MS,
        close_time_ms=(index + 1) * HOUR_MS - 1,
        open=open_,
        high=high,
        low=low,
        close=close,
        quote_volume=1_000_000,
        trades=1_000,
    )


class DonchianPortfolioReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = PortfolioConfig(
            entry_channel_hours=3,
            exit_channel_hours=2,
            hard_stop_pct=2,
            fee_per_side_pct=0.1,
            notional_usdt=20,
            max_positions=5,
        )

    def test_entry_executes_at_next_hour_open_without_lookahead(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101.5, 103.5),
            candle(4, 105, 106, 104, 105),
        ]

        result = replay_portfolio({"TESTUSDT": bars}, self.config)

        self.assertEqual(result.executed_entries, 1)
        self.assertEqual(result.trades[0].entry_price, 105)
        self.assertEqual(result.trades[0].opened_ms, bars[4].open_time_ms)

    def test_hard_stop_applies_on_entry_bar_and_includes_fees(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101.5, 103.5),
            candle(4, 100, 101, 97, 98),
        ]

        result = replay_portfolio({"TESTUSDT": bars}, self.config)
        trade = result.trades[0]

        self.assertEqual(trade.exit_reason, "HARD_STOP")
        self.assertEqual(trade.exit_price, 98)
        self.assertLess(trade.net_pct, -2.1)

    def test_channel_exit_is_scheduled_for_next_open(self) -> None:
        config = PortfolioConfig(
            entry_channel_hours=3,
            exit_channel_hours=2,
            hard_stop_pct=20,
            fee_per_side_pct=0.1,
            notional_usdt=20,
            max_positions=5,
        )
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101, 103.5),
            candle(4, 104, 105, 103, 104),
            candle(5, 104, 104.5, 100, 100.5),
            candle(6, 101, 102, 100, 101),
        ]

        result = replay_portfolio({"TESTUSDT": bars}, config)
        trade = result.trades[0]

        self.assertEqual(trade.exit_reason, "DONCHIAN_EXIT")
        self.assertEqual(trade.closed_ms, bars[6].open_time_ms)
        self.assertEqual(trade.exit_price, bars[6].open)

    def test_strongest_simultaneous_breakout_gets_only_slot(self) -> None:
        config = PortfolioConfig(
            entry_channel_hours=3,
            exit_channel_hours=2,
            hard_stop_pct=20,
            max_positions=1,
        )
        weak = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101, 103.5),
            candle(4, 104, 105, 103, 104),
        ]
        strong = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 108, 101, 107),
            candle(4, 107, 108, 106, 107),
        ]

        result = replay_portfolio(
            {"WEAKUSDT": weak, "STRONGUSDT": strong},
            config,
        )

        self.assertEqual(result.executed_entries, 1)
        self.assertEqual(result.blocked_slots, 1)
        self.assertEqual(result.trades[0].symbol, "STRONGUSDT")

    def test_candidate_order_policies_are_deterministic(self) -> None:
        candidates = [
            EntryCandidate("BBBUSDT", 0, 1, 103, 100, 3),
            EntryCandidate("AAAUSDT", 0, 1, 101, 100, 1),
            EntryCandidate("CCCUSDT", 0, 1, 102, 100, 2),
        ]

        strongest = _ordered_candidates(candidates, "strongest")
        alphabetical = _ordered_candidates(candidates, "alphabetical")
        reverse = _ordered_candidates(candidates, "reverse_alphabetical")

        self.assertEqual([item.symbol for item in strongest], ["BBBUSDT", "CCCUSDT", "AAAUSDT"])
        self.assertEqual([item.symbol for item in alphabetical], ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
        self.assertEqual([item.symbol for item in reverse], ["CCCUSDT", "BBBUSDT", "AAAUSDT"])

    def test_summary_reports_result_without_largest_winner(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101, 103.5),
            candle(4, 104, 110, 103, 109),
        ]

        result = replay_portfolio({"TESTUSDT": bars}, self.config)
        report = summarize(result, self.config)

        self.assertEqual(report["closed_trades"], 1)
        self.assertAlmostEqual(report["total_net_usdt_without_best_trade"], 0.0)

    def test_historical_liquidity_filter_uses_trailing_candles_only(self) -> None:
        config = PortfolioConfig(
            entry_channel_hours=3,
            exit_channel_hours=2,
            hard_stop_pct=20,
            min_quote_volume_24h=24_000,
        )
        bars = [
            MarketCandle(
                open_time_ms=index * HOUR_MS,
                close_time_ms=(index + 1) * HOUR_MS - 1,
                open=100,
                high=101,
                low=99,
                close=100,
                quote_volume=500,
                trades=100,
            )
            for index in range(24)
        ]
        bars.append(
            MarketCandle(
                open_time_ms=24 * HOUR_MS,
                close_time_ms=25 * HOUR_MS - 1,
                open=100,
                high=103,
                low=100,
                close=102,
                quote_volume=500,
                trades=100,
            )
        )
        bars.append(
            MarketCandle(
                open_time_ms=25 * HOUR_MS,
                close_time_ms=26 * HOUR_MS - 1,
                open=102,
                high=103,
                low=101,
                close=102,
                quote_volume=1_000_000,
                trades=100,
            )
        )

        result = replay_portfolio({"TESTUSDT": bars}, config)

        self.assertEqual(result.executed_entries, 0)

    def test_replay_start_excludes_warmup_signals(self) -> None:
        bars = [
            candle(0, 100, 101, 99, 100),
            candle(1, 100, 102, 99, 101),
            candle(2, 101, 103, 100, 102),
            candle(3, 102, 104, 101, 103.5),
            candle(4, 104, 105, 103, 104),
        ]

        result = replay_portfolio(
            {"TESTUSDT": bars},
            self.config,
            replay_start_ms=bars[4].close_time_ms + 1,
        )

        self.assertEqual(result.executed_entries, 0)


if __name__ == "__main__":
    unittest.main()
