from __future__ import annotations

import unittest
import json
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import tools.forward_experiment_report as report
from tools.forward_experiment_report import (
    accepted_trade_rows,
    calculate_max_simultaneous,
    event_counts,
    filter_since,
    first_comparable_instant,
    main,
    _print_ema_macd_warmup,
    _print_macd_warmup,
    operational_warmup_counts,
    pair_by_source,
    parse_time,
    realized_max_drawdown,
)


COHORT = datetime(2026, 9, 27, 23, 7, 29, tzinfo=timezone.utc)


class ForwardExperimentReportTests(unittest.TestCase):
    def test_histogram_mode_reports_sequential_block_counts(self):
        arm = report.EXPERIMENTS['ema_macd_hist_1m']
        events = [{'event':'ADMISSION_FILTERS', 'ema_macd_pass':base,
                   'histogram_pass':hist, 'confirmation_1m_pass':tactical,
                   'final_decision':decision} for base,hist,tactical,decision in
                  ((False,False,False,'blocked'), (True,False,False,'blocked'),
                   (True,True,False,'blocked'), (True,True,True,'admitted'))]
        window = report.ComparableWindow(COHORT, COHORT, COHORT+timedelta(days=1))
        output = StringIO()
        with tempfile.TemporaryDirectory() as tmp, patch.object(report,'ROOT',Path(tmp)), patch.object(report,'determine_comparable_window',return_value=window), patch.object(report,'_events',return_value=events), patch('sys.argv',['report','--experiment','ema_macd_hist_1m']), redirect_stdout(output):
            main()
        for line in ('total oportunidades | 4', 'bloqueadas por EMA_MACD | 1',
                     'bloqueadas adicionalmente por histogram | 1',
                     'bloqueadas adicionalmente por 1m | 1', 'admitidas finais | 1'):
            self.assertIn(line, output.getvalue())

    def test_every_experiment_shows_control_and_trade_list_without_real_a(self):
        window = report.ComparableWindow(COHORT, COHORT, COHORT + timedelta(days=1))
        for name in report.EXPERIMENTS:
            with self.subTest(experiment=name), tempfile.TemporaryDirectory() as tmp:
                output = StringIO()
                with patch.object(report, 'ROOT', Path(tmp)), patch.object(report, 'determine_comparable_window', return_value=window), patch('sys.argv', ['report', '--experiment', name]), redirect_stdout(output):
                    main()
                value = output.getvalue()
                self.assertIn(report.CONTROL.name + ' |', value)
                self.assertIn(report.EXPERIMENTS[name].name + ' |', value)
                self.assertNotIn('REAL_A |', value)
                self.assertNotIn('WARM-UP / NON-COMPARABLE', value)
                self.assertIn('source_candle | entry BRT | entry price | exit BRT | exit price | net | EMA context | MACD context | reason / exit type', value)

    def test_general_summary_includes_operational_benchmark_and_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = StringIO()
            with patch.object(report, 'ROOT', Path(tmp)), redirect_stdout(output):
                report.print_summary(COHORT)
            self.assertIn('REAL_A |', output.getvalue())
            self.assertIn('BE_OFF_CB_FAST_DROP_EMA_SHADOW |', output.getvalue())
            self.assertIn(' | net | net $/trade |', output.getvalue())
            self.assertIn(' | FAST |', output.getvalue())

    def test_default_brt_cohort_timestamp_is_exact(self) -> None:
        self.assertEqual(parse_time("27/09/2026 20:07:29"), COHORT)

    def test_filter_since_rejects_prior_cohort_and_missing_timestamps(self) -> None:
        rows = [
            {"pair_id": "old", "opened_at": "2026-09-27T23:07:28+00:00"},
            {"pair_id": "edge", "opened_at": "2026-09-27T23:07:29+00:00"},
            {"pair_id": "new", "opened_at": "2026-09-27T23:08:00+00:00"},
            {"pair_id": "missing"},
        ]
        self.assertEqual([row["pair_id"] for row in filter_since(rows, COHORT, "opened_at")], ["edge", "new"])

    def test_pairing_uses_only_exact_source_candle(self) -> None:
        control = [
            {"pair_id": "c1", "source_candle_open_time": 100},
            {"pair_id": "c2", "source_candle_open_time": 200},
        ]
        experiment = [
            {"pair_id": "e1", "source_candle_open_time": 100},
            {"pair_id": "e3", "source_candle_open_time": 300},
        ]
        pairs = pair_by_source(control, experiment)
        self.assertEqual([(left["pair_id"], right["pair_id"]) for left, right in pairs], [("e1", "c1")])

    def test_event_reader_counts_only_new_hypothesis_events_inside_cohort(self) -> None:
        events = [
            {"ts": "2026-09-27T23:07:28+00:00", "event": "HS_ELASTIC_STARTED"},
            {"ts": "2026-09-27T23:07:29+00:00", "event": "HS_ELASTIC_STARTED"},
            {"ts": "2026-09-27T23:10:00+00:00", "event": "HS_ELASTIC_ENDED_RECOVERED"},
            {"ts": "2026-09-27T23:11:00+00:00", "event": "HS_BEAR_CLUSTER_TRIGGERED"},
            {"event": "CB_EXIT_ALL_TRIGGERED"},
        ]
        self.assertEqual(
            event_counts(events, COHORT),
            {
                "HS_ELASTIC_STARTED": 1,
                "HS_ELASTIC_ENDED_RECOVERED": 1,
                "HS_BEAR_CLUSTER_TRIGGERED": 1,
            },
        )

    def test_comparable_instant_waits_for_overlapping_inherited_positions_and_cooldown(self) -> None:
        floor = datetime(2026, 9, 28, 4, 47, tzinfo=timezone.utc)
        positions = [
            (floor-timedelta(hours=1), floor+timedelta(minutes=5), "arm-a:p1"),
            (floor+timedelta(minutes=2), floor+timedelta(minutes=9), "arm-b:p2"),
        ]
        cooldowns = [(floor-timedelta(minutes=10), floor+timedelta(minutes=7), "control:CB")]
        instant, reason = first_comparable_instant(
            positions, cooldowns, floor, floor+timedelta(minutes=20)
        )
        self.assertEqual(instant, floor+timedelta(minutes=9))
        self.assertIsNone(reason)

    def test_comparable_instant_does_not_invent_close_for_open_carryover(self) -> None:
        floor = datetime(2026, 9, 28, 4, 47, tzinfo=timezone.utc)
        instant, reason = first_comparable_instant(
            [(floor-timedelta(minutes=1), None, "arm:p1")], [], floor, floor+timedelta(hours=1)
        )
        self.assertIsNone(instant)
        self.assertIn("arm:p1", reason or "")

    def test_max_simultaneous_is_recomputed_only_from_window_admissions(self) -> None:
        since = datetime(2026, 9, 28, 5, 0, tzinfo=timezone.utc)
        closed = [
            {"pair_id": "old", "opened_at": "2026-09-28T04:59:00Z", "closed_at": "2026-09-28T05:30:00Z"},
            {"pair_id": "a", "opened_at": "2026-09-28T05:01:00Z", "closed_at": "2026-09-28T05:10:00Z"},
            {"pair_id": "b", "opened_at": "2026-09-28T05:02:00Z", "closed_at": "2026-09-28T05:03:00Z"},
        ]
        opened = [{"pair_id": "c", "open_ts": "2026-09-28T05:10:00Z", "status": "OPEN"}]
        self.assertEqual(calculate_max_simultaneous(closed, opened, since), 2)

    def test_operational_warmup_counts_own_arm_events_without_economics(self) -> None:
        events = [
            {"event": "HS_BEAR_CLUSTER_TRIGGERED"},
            {"event": "EXPERIMENTAL_CLOSE"},
            {"event": "EXPERIMENTAL_CLOSE"},
        ]
        records = [
            {"exit_reason": "HS_BEAR_CLUSTER_EXIT", "net_pnl_pct": -1.0},
            {"exit_reason": "HS_BEAR_CLUSTER_EXIT", "net_pnl_pct": -2.0},
        ]
        counts = operational_warmup_counts("hs_bear", events, records)
        self.assertEqual(counts["HS_BEAR_CLUSTER_TRIGGERED"], 1)
        self.assertEqual(counts["EXPERIMENTAL_CLOSE"], 2)
        self.assertEqual(counts["positions closed by cluster"], 2)
        self.assertNotIn("net", counts)

    def test_macd_warmup_shows_context_and_non_policy_blocks(self) -> None:
        events = [
            {"event": "SIGNAL_OPPORTUNITY", "source_candle_open_time": 1, "macd_context": "BU-"},
            {"event": "ENTRY_BLOCKED_MACD_BU_MINUS", "source_candle_open_time": 1},
            {"event": "SIGNAL_OPPORTUNITY", "source_candle_open_time": 2, "macd_context": "BE+"},
            {"event": "OPEN", "source_candle_open_time": 2},
            {"event": "SIGNAL_OPPORTUNITY", "source_candle_open_time": 3, "macd_context": "BU+"},
            {"event": "ENTRY_BLOCKED_SHADOW_CAPACITY", "source_candle_open_time": 3},
        ]
        output = StringIO()
        with redirect_stdout(output):
            _print_macd_warmup(events)
        text = output.getvalue()
        self.assertIn("BU- opportunities | 1", text)
        self.assertIn("accepted non-BU- opportunities | 1", text)
        self.assertIn("ENTRY_BLOCKED_SHADOW_CAPACITY | 1", text)
        self.assertIn("BE+ | 1", text)

    def test_ema_macd_warmup_prints_full_seven_by_four_matrix(self) -> None:
        events = [
            {"event": "SIGNAL_OPPORTUNITY", "source_candle_open_time": 1,
             "ema_context": "LON", "macd_context": "BU+"},
            {"event": "OPEN", "source_candle_open_time": 1},
            {"event": "SIGNAL_OPPORTUNITY", "source_candle_open_time": 2,
             "ema_context": "SHO", "macd_context": "BE-"},
            {"event": "ENTRY_BLOCKED_EMA_MACD_SHO_BE-", "source_candle_open_time": 2},
        ]
        output = StringIO()
        with redirect_stdout(output):
            _print_ema_macd_warmup(events)
        text = output.getvalue()
        self.assertIn("accepted | 1", text)
        self.assertIn("blocked | 1", text)
        self.assertIn("LON | BU+=1 | BU-=0 | BE+=0 | BE-=0", text)
        self.assertIn("MIX | BU+=0 | BU-=0 | BE+=0 | BE-=0", text)

    def test_list_accepted_joins_open_and_closed_trade_fields(self) -> None:
        events = [
            {"event": "OPEN", "ts": "2026-09-28T03:00:00Z", "source_candle_open_time": 1000,
             "ema_context": "LON", "macd_context": "BU+", "price": 150},
            {"event": "OPEN", "ts": "2026-09-28T03:05:00Z", "source_candle_open_time": 2000,
             "ema_context": "BUL", "macd_context": "BE+", "price": 151},
        ]
        closed = [{
            "source_candle_open_time": 1000, "opened_at": "2026-09-28T03:00:00Z",
            "closed_at": "2026-09-28T03:10:00Z",
            "entry_price": 150, "exit_price": 152, "exit_reason": "PROFIT_LOCK",
            "net_pnl_pct": 1, "position_notional_usdt": 20,
        }]
        opened = [{
            "source_candle_open_time": 2000, "open_ts": "2026-09-28T03:05:00Z",
            "entry_price": 151, "status": "OPEN",
        }]
        rows = accepted_trade_rows(events, closed, opened)
        self.assertEqual(rows[0]["status"], "CLOSED")
        self.assertEqual(rows[0]["exit"], 152)
        self.assertEqual(rows[0]["net"], 0.2)
        self.assertEqual(rows[0]["closed_at"], datetime(2026, 9, 28, 3, 10, tzinfo=timezone.utc))
        self.assertEqual(rows[1]["status"], "OPEN")
        self.assertIsNone(rows[1]["exit"])
        self.assertIsNone(rows[1]["closed_at"])

    def test_list_accepted_is_allowed_for_macd_bu_minus(self) -> None:
        output = StringIO()
        with patch("sys.argv", ["forward_experiment_report.py", "--experiment", "macd_bu_minus",
                                "--list-accepted"]), redirect_stdout(output):
            main()
        self.assertIn("ACCEPTED TRADES", output.getvalue())
        self.assertIn("source_candle | entry BRT | entry price | exit BRT | exit price | net | EMA context | MACD context | reason / exit type", output.getvalue())
        self.assertNotIn("WARM-UP / NON-COMPARABLE", output.getvalue())

    def test_warmup_is_hidden_and_switch_removed(self) -> None:
        hidden = StringIO()
        with patch("sys.argv", ["forward_experiment_report.py"]), redirect_stdout(hidden):
            main()
        shown = StringIO()
        with patch("sys.argv", ["forward_experiment_report.py", "--show-warmup"]), redirect_stdout(shown), self.assertRaises(SystemExit):
            main()
        self.assertNotIn("WARM-UP / NON-COMPARABLE", hidden.getvalue())
        self.assertNotIn("WARM-UP / NON-COMPARABLE", shown.getvalue())

    def test_resolved_comparable_outputs_include_net_pf_dd_and_time_filter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def arm(name: str) -> report.Arm:
                key = name.lower()
                return report.Arm(name, f"{key}.ledger", f"{key}.state", f"{key}.events")

            def write(target: str, rows: list[dict]) -> None:
                path = root / target
                path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

            control = arm("BE_OFF_CB_SHADOW")
            macd = arm("BE_OFF_CB_MACD_BU_MINUS_SHADOW")
            ema = arm("BE_OFF_CB_EMA_MACD_SHADOW")
            old = {"pair_id": "old", "source_candle_open_time": 50,
                   "opened_at": "2026-09-28T04:00:00Z", "closed_at": "2026-09-28T04:30:00Z",
                   "net_pnl_pct": -50, "position_notional_usdt": 20, "exit_reason": "HARD_STOP",
                   "age_seconds": 1800}
            win = {"pair_id": "c1", "source_candle_open_time": 100,
                   "opened_at": "2026-09-28T05:00:00Z", "closed_at": "2026-09-28T05:10:00Z",
                   "net_pnl_pct": 5, "position_notional_usdt": 20, "exit_reason": "PROFIT_LOCK",
                   "age_seconds": 600}
            loss = {"pair_id": "c2", "source_candle_open_time": 200,
                    "opened_at": "2026-09-28T05:05:00Z", "closed_at": "2026-09-28T05:20:00Z",
                    "net_pnl_pct": -2.5, "position_notional_usdt": 20, "exit_reason": "HARD_STOP",
                    "age_seconds": 900}
            experiment_win = dict(win, pair_id="e1")
            control_events = [
                {"ts": "2026-09-28T05:00:00Z", "event": "OPEN", "source_candle_open_time": 100},
                {"ts": "2026-09-28T05:05:00Z", "event": "OPEN", "source_candle_open_time": 200},
            ]
            macd_events = [
                {"ts": "2026-09-28T05:00:00Z", "event": "SIGNAL_OPPORTUNITY",
                 "source_candle_open_time": 100, "macd_context": "BU+"},
                {"ts": "2026-09-28T05:00:01Z", "event": "OPEN", "source_candle_open_time": 100},
                {"ts": "2026-09-28T05:05:00Z", "event": "SIGNAL_OPPORTUNITY",
                 "source_candle_open_time": 200, "macd_context": "BU-"},
                {"ts": "2026-09-28T05:05:01Z", "event": "ENTRY_BLOCKED_MACD_BU_MINUS",
                 "source_candle_open_time": 200},
            ]
            ema_events = [
                {"ts": "2026-09-28T05:00:00Z", "event": "SIGNAL_OPPORTUNITY",
                 "source_candle_open_time": 100, "ema_context": "LON", "macd_context": "BU+"},
                {"ts": "2026-09-28T05:00:01Z", "event": "OPEN", "source_candle_open_time": 100},
                {"ts": "2026-09-28T05:05:00Z", "event": "SIGNAL_OPPORTUNITY",
                 "source_candle_open_time": 200, "ema_context": "SHO", "macd_context": "BE-"},
                {"ts": "2026-09-28T05:05:01Z", "event": "ENTRY_BLOCKED_EMA_MACD_SHO_BE-",
                 "source_candle_open_time": 200},
            ]
            for item, ledger, events in (
                (control, [old, win, loss], control_events),
                (macd, [experiment_win], macd_events),
                (ema, [experiment_win], ema_events),
            ):
                write(item.ledger, ledger)
                (root / item.state).write_text(json.dumps({"positions": []}), encoding="utf-8")
                write(item.events, events)

            cohort = datetime(2026, 9, 27, 23, 7, 29, tzinfo=timezone.utc)
            floor = datetime(2026, 9, 28, 4, 47, tzinfo=timezone.utc)
            observed = datetime(2026, 9, 28, 6, 0, tzinfo=timezone.utc)
            with patch.object(report, "ROOT", root), patch.object(report, "CONTROL", control):
                window = report.determine_comparable_window(
                    cohort, floor, observed_at=observed, arms=(control, macd, ema)
                )
                self.assertEqual(window.comparable_since, floor)
                self.assertEqual(realized_max_drawdown([win, loss]), 0.5)
                macd_output = StringIO()
                with redirect_stdout(macd_output):
                    report.print_macd_bu_minus(macd, floor)
                ema_output = StringIO()
                with redirect_stdout(ema_output):
                    report.print_ema_macd(ema, floor, False)

            expected_header = "arm | closed | open | net | net $/trade | PF | DD $ | HS | PL | TRAIL | median age | max simultaneous"
            expected_control = "BE_OFF_CB_SHADOW | 2 | 0 | $+0.5000 | $+0.2500 | 2.000 | $0.5000 | 1 | 1 | 0 | 12.5m | 2"
            for text in (macd_output.getvalue(), ema_output.getvalue()):
                self.assertIn(expected_header, text)
                self.assertIn(expected_control, text)
                self.assertNotIn("$-10.0000", text)
            self.assertIn("trades common | 1", macd_output.getvalue())
            self.assertIn("control-only | 1", macd_output.getvalue())
            self.assertIn("net=$-0.5000", macd_output.getvalue())
            self.assertIn("trades common | 1", ema_output.getvalue())
            self.assertIn("control-only | 1", ema_output.getvalue())
            self.assertIn("net=$-0.5000", ema_output.getvalue())


if __name__ == "__main__":
    unittest.main()
