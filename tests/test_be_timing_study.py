from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from tools.be_timing_study import (
    Candle,
    Trade,
    evaluate_no_progress,
    reconstruct_be_results,
)


class BeTimingStudyTests(unittest.TestCase):
    def test_exact_runtime_event_has_priority(self) -> None:
        trade = make_trade("one", activation=None)
        armed_at = trade.opened + timedelta(minutes=37)

        results = reconstruct_be_results([trade], {trade.pair_id: armed_at}, [])

        self.assertEqual(results[0].status, "ARMED")
        self.assertEqual(results[0].source, "EVENT_EXACT")
        self.assertEqual(results[0].time_seconds, 37 * 60)

    def test_missing_event_is_reconstructed_from_complete_one_minute_path(self) -> None:
        trade = make_trade("one", activation=100.50, age_hours=2)
        candles = minute_candles(trade.opened, 120, default_high=100.20)
        candles[44] = Candle(candles[44].open_ms, candles[44].close_ms, 100.50)

        results = reconstruct_be_results([trade], {}, candles)

        self.assertEqual(results[0].status, "ARMED")
        self.assertEqual(results[0].source, "BINANCE_1M")
        self.assertEqual(results[0].time_seconds, 44 * 60)

    def test_missing_activation_price_is_never_invented(self) -> None:
        trade = make_trade("one", activation=None)

        results = reconstruct_be_results([trade], {}, [])

        self.assertEqual(results[0].status, "UNAVAILABLE")
        self.assertEqual(results[0].source, "MISSING_ACTIVATION_PRICE")

    def test_no_progress_at_two_hours_uses_state_at_checkpoint(self) -> None:
        opened = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
        early = make_trade("early", activation=100.50, opened=opened, age_hours=4, reason="BREAKEVEN")
        late = make_trade("late", activation=100.50, opened=opened, age_hours=4, reason="TRAILING")
        never = make_trade("never", activation=105.0, opened=opened, age_hours=4, reason="HARD_STOP")
        unknown = make_trade("unknown", activation=None, opened=opened, age_hours=4, reason="PROFIT_LOCK")
        candles = minute_candles(opened, 240, default_high=100.20)
        for index in range(180, 240):
            candles[index] = Candle(candles[index].open_ms, candles[index].close_ms, 102.0)

        evaluation = evaluate_no_progress(
            [early, late, never, unknown],
            {
                "early": opened + timedelta(hours=1),
                "late": opened + timedelta(hours=3),
            },
            candles,
            2.0,
        )

        self.assertEqual(evaluation.eligible, 4)
        self.assertEqual(evaluation.armed_by_checkpoint, 1)
        self.assertEqual(evaluation.unavailable, 1)
        self.assertEqual({item.trade.pair_id for item in evaluation.results}, {"late", "never"})
        self.assertTrue(all(abs((item.future_mfe_pct or 0) - 2.0) < 1e-9 for item in evaluation.results))

    def test_partial_entry_minute_that_may_have_crossed_is_unavailable(self) -> None:
        opened = datetime(2026, 8, 13, 12, 0, 30, tzinfo=timezone.utc)
        trade = make_trade("one", activation=100.50, opened=opened, age_hours=3)
        floor_open = int(datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
        candles = [Candle(floor_open, floor_open + 59_999, 100.60)]
        candles.extend(minute_candles(datetime(2026, 8, 13, 12, 1, tzinfo=timezone.utc), 179, 100.20))

        evaluation = evaluate_no_progress([trade], {}, candles, 2.0)

        self.assertEqual(evaluation.eligible, 1)
        self.assertEqual(evaluation.unavailable, 1)
        self.assertEqual(evaluation.results, [])


def make_trade(
    pair_id: str,
    activation: float | None,
    opened: datetime | None = None,
    age_hours: int = 3,
    reason: str = "BREAKEVEN",
) -> Trade:
    start = opened or datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)
    return Trade(
        record={
            "pair_id": pair_id,
            "exit_reason": reason,
            "entry_price": 100.0,
            "exit_price": 99.0,
            "peak_price": 102.0,
        },
        opened=start,
        closed=start + timedelta(hours=age_hours),
        entry=100.0,
        activation_price=activation,
    )


def minute_candles(opened: datetime, count: int, default_high: float) -> list[Candle]:
    first = int(opened.timestamp() * 1000)
    return [
        Candle(first + index * 60_000, first + (index + 1) * 60_000 - 1, default_high)
        for index in range(count)
    ]


if __name__ == "__main__":
    unittest.main()
