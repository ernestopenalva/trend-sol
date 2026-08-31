from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import BRASILIA_TZ, console_line
from src.exchange.binance_client import BinanceClient, BinanceClientError
from src.exchange.binance_market_data import BinanceMarketDataClient
from src.logging_utils import JsonlLogger
from src.monitor.cycle_manager import CycleManager
from src.monitor.dmi15_shadow import Dmi15ShadowRegistry
from src.monitor.dmi15_spread_shadow import Dmi15SpreadShadowRegistry
from src.monitor.dmi15_trajectory_shadow import Dmi15TrajectoryShadowRegistry
from src.monitor.dmi15_rsi70_shadow import Dmi15Rsi70ShadowRegistry
from src.monitor.dmi15_combined_shadow import Dmi15CombinedShadowRegistry
from src.monitor.entry_engine import EntryEngine
from src.monitor.context_predicates import passes_dmi15_trajectory, passes_slow_ge45
from src.monitor.context_shadow import RealAContextShadow
from src.monitor.h2_exposure_shadow import H2ExposureShadow
from src.monitor.gcr_shadow import GcrShadowRegistry
from src.monitor.market_context import MarketContextEngine
from src.monitor.human_console_reporter import HumanConsoleReporter
from src.monitor.multi_market_shadow import MultiMarketShadow
from src.monitor.position_registry import PositionRegistry
from src.monitor.ws_manager import WSManager
from src.project_env import load_project_env
from src.state_manager import StateManager
from src.trade_ledger import TradeLedger
from src.telemetry_writer import TelemetryWriter


class Monitor:
    def __init__(self, config_path: Path = CONFIG_FILE) -> None:
        load_project_env()
        self.project_root = PROJECT_ROOT
        self.config = effective_config(self._load_yaml(config_path))
        self.config["run_id"] = _run_id(self.config)
        self.logger = JsonlLogger(self.project_root, self.config)
        self.telemetry_writer = TelemetryWriter(self.project_root, self.config, self.logger)
        self.last_price: float | None = None
        self.last_tick_monotonic: float | None = None
        self.started_monotonic = time.monotonic()
        self.ws_manager: WSManager | None = None
        self.status_reporter: HumanConsoleReporter | None = None
        self.state_manager = StateManager(self.project_root)
        self._cycle_stop_announced = False
        self.logger.system(
            "trend_sol_boot",
            config=str(config_path),
            symbol=self.config.get("symbol"),
            profile=self.config.get("active_profile"),
        )
        self.logger.system(
            "context_forward_cohort_started",
            cohort_id=self.config.get("instrumentation", {}).get("context_forward_cohort_id"),
            arms=["REAL_A", "DMI15_TRAJECTORY_CONTEXT_SHADOW", "SLOW_GE_CONTEXT_SHADOW"],
            primary_metric="gross_per_trade",
            ladder="A",
        )
        execution_cfg = self.config["execution"]
        self.logger.system("syncing_binance_server_time", url=execution_cfg["testnet_url"])
        self.client = BinanceClient(
            base_url=execution_cfg["testnet_url"],
            recv_window_ms=int(execution_cfg["recv_window_ms"]),
            use_server_time_sync=bool(execution_cfg.get("use_server_time_sync", True)),
            http_timeout_seconds=int(execution_cfg.get("http_timeout_seconds", 8)),
        )
        market_cfg = self.config["market_data"]
        self.market_data_client = BinanceMarketDataClient(
            base_url=market_cfg.get("rest_url", "https://api.binance.com"),
            timeout_seconds=int(execution_cfg.get("http_timeout_seconds", 8)),
        )
        self.cycle_manager = CycleManager(self.project_root, self.config, self.logger, self.state_manager)
        self.trade_ledger = TradeLedger(self.project_root)
        self.registry = PositionRegistry(
            self.config,
            self.client,
            self.logger,
            self.cycle_manager,
            self.state_manager,
            self.trade_ledger,
            self.telemetry_writer,
        )
        self.h2_exposure_shadow = H2ExposureShadow(
            self.project_root,
            self.config,
            self.logger,
            self.telemetry_writer,
            self.client.symbol_filters,
        )
        self.entry_engine = EntryEngine(str(self.config["symbol"]), self.config, self.logger)
        self.gcr_shadow = GcrShadowRegistry(
            self.project_root, self.config, self.logger, self.telemetry_writer
        )
        self.dmi15_shadow = Dmi15ShadowRegistry(
            self.project_root, self.config, self.logger, self.telemetry_writer
        )
        self.dmi15_spread_shadow = Dmi15SpreadShadowRegistry(
            self.project_root, self.config, self.logger, self.telemetry_writer
        )
        self.dmi15_trajectory_shadow = Dmi15TrajectoryShadowRegistry(
            self.project_root, self.config, self.logger, self.telemetry_writer
        )
        self.dmi15_rsi70_shadow = Dmi15Rsi70ShadowRegistry(
            self.project_root, self.config, self.logger, self.telemetry_writer
        )
        self.dmi15_combined_shadow = Dmi15CombinedShadowRegistry(
            self.project_root, self.config, self.logger, self.telemetry_writer
        )
        self.market_context = MarketContextEngine(self.entry_engine, self.config)
        self.dmi15_trajectory_context_shadow = RealAContextShadow(
            self.project_root,
            self.config,
            self.logger,
            self.telemetry_writer,
            settings_key="dmi15_trajectory_context_shadow",
            strategy="DMI15_TRAJECTORY_CONTEXT_SHADOW",
            shadow_kind="DMI15_TRAJECTORY_CONTEXT_SHADOW",
            pair_prefix="dmi15ctx",
            predicate=lambda _engine, snapshot: passes_dmi15_trajectory(
                snapshot.get("tf_5m", {}) if isinstance(snapshot, dict) else {}
            ),
        )
        self.slow_ge_context_shadow = RealAContextShadow(
            self.project_root,
            self.config,
            self.logger,
            self.telemetry_writer,
            settings_key="slow_ge_context_shadow",
            strategy="SLOW_GE_CONTEXT_SHADOW",
            shadow_kind="SLOW_GE_CONTEXT_SHADOW",
            pair_prefix="slowgectx",
            predicate=lambda engine, _snapshot: passes_slow_ge45(engine._candles_for("15m")),
        )
        no_progress = self.config.get("risk", {}).get("no_progress", {})
        context_settings = self.config.get("instrumentation", {}).get("market_context", {})
        ge_sync = self.config.get("trend_gate", {}).get("sync", {})
        self.logger.system(
            "coordinated_package_resolved",
            real_logic="REAL_A",
            historical_runtime_label="B",
            ge_label="GE15",
            ge_sync_enabled=ge_sync.get("enabled"),
            ge_sync_timeout_seconds=ge_sync.get("timeout_seconds"),
            ge_sync_timeout_action=ge_sync.get("timeout_action"),
            ge_sync_expire_on_next_entry_candle=ge_sync.get("expire_on_next_entry_candle"),
            max_entries_per_5m_candle=self.config.get("entry", {}).get("max_entries_per_candle"),
            admission_candle_interval=self.config.get("entry", {}).get("admission_candle_interval"),
            hard_stop_pct=self.config.get("risk", {}).get("hard_stop", {}).get("stop_pct"),
            no_progress_default_hours=no_progress.get("default_hours"),
            no_progress_enabled=no_progress.get("enabled"),
            no_progress_rolling_window=no_progress.get("rolling_window"),
            no_progress_min_be_samples=no_progress.get("min_be_samples"),
            no_progress_statistic=no_progress.get("statistic"),
            no_progress_tolerance_buffer_pct=no_progress.get("tolerance_buffer_pct"),
            gcr_shadow="GCR_SHADOW_B",
            gcr_previous_must_arm_be=True,
            h2_exposure_shadow="H2_EXPOSURE_SHADOW",
            h2_exposure_rule="unchanged REAL_A signal/admission/ladder; harmonic sizing by uncovered H2 positions",
            dmi15_shadow="DMI15_SHADOW_C",
            dmi15_entry_rule="+DI_now>+DI_15m_ago AND -DI_now<-DI_15m_ago AND +DI_now>-DI_now",
            dmi15_spread_shadow="DMI15_SPREAD6_SHADOW_D",
            dmi15_spread_entry_rule="DMI15 AND (+DI_now - -DI_now)>=6",
            dmi15_trajectory_shadow="DMI15_TRAJECTORY_SHADOW_E",
            dmi15_trajectory_entry_rule="DMI15 AND +DI_now>+DI_5m_ago AND -DI_now<-DI_5m_ago",
            dmi15_rsi70_shadow="DMI15_RSI70_SHADOW_F",
            dmi15_rsi70_entry_rule="DMI15 AND RSI_MA_5m<=70",
            dmi15_combined_shadow="DMI15_COMBINED_SHADOW_G",
            dmi15_combined_entry_rule="DMI15 AND spread>=6 AND trajectory AND RSI_MA_5m<=70",
            dmi15_trajectory_context_shadow="DMI15_TRAJECTORY_CONTEXT_SHADOW",
            dmi15_trajectory_context_rule="DMI15_TRAJECTORY THEN unchanged REAL_A: GE15 + G2 + G3 + G4",
            slow_ge_context_shadow="SLOW_GE_CONTEXT_SHADOW",
            slow_ge_context_rule="GE45 THEN unchanged REAL_A: GE15 + G2 + G3 + G4",
            market_context_telemetry_only=True,
            market_context_timeframes=context_settings.get("timeframes"),
            market_context_indicators=["EMA20", "EMA50", "EMA20_SLOPE", "EMA50_SLOPE", "ADX14", "+DI14", "-DI14", "RSI14", "RSI14_SMA14_5M", "RVOL", "GE15"],
        )
        self.market_shadow = MultiMarketShadow(
            self.project_root,
            self.config,
            self.market_data_client,
            self.logger,
            self.telemetry_writer,
            on_streams_changed=self._refresh_market_streams,
            shadow_kind="TOP3_MARKET",
            gate1_mode="legacy_ema",
        )
        shadow_settings = self.config.get("instrumentation", {}).get("multi_market_shadow", {})
        ge30_settings = (
            shadow_settings.get("ge30_variant", {})
            if isinstance(shadow_settings, dict)
            else {}
        )
        self.market_shadow_ge30 = MultiMarketShadow(
            self.project_root,
            self.config,
            self.market_data_client,
            self.logger,
            self.telemetry_writer,
            on_streams_changed=self._refresh_market_streams,
            shadow_kind="TOP3_MARKET_GE30",
            gate1_mode="ge30",
            settings_override=ge30_settings,
            selection_source=self.market_shadow,
        )
        self.logger.set_entry_console_context(self._entry_console_context)

    def run(self) -> None:
        self.telemetry_writer.start()
        try:
            self.logger.system("validating_startup")
            self._validate_startup()
            self._load_historical_candles()
            initial_context = self._safe_refresh_market_context()
            self.registry.record_market_context(initial_context)
            self.gcr_shadow.record_market_context(initial_context)
            self.dmi15_shadow.record_market_context(initial_context)
            self.dmi15_spread_shadow.record_market_context(initial_context)
            self.dmi15_trajectory_shadow.record_market_context(initial_context)
            self.dmi15_rsi70_shadow.record_market_context(initial_context)
            self.dmi15_combined_shadow.record_market_context(initial_context)
            self.market_shadow.start()
            self.market_shadow_ge30.start()
            market_cfg = self.config["market_data"]
            streams = self._market_streams()
            self.ws_manager = WSManager(
                market_cfg["ws_url"],
                streams,
                self.logger,
                self._on_ws_event,
                ping_interval_seconds=int(market_cfg.get("ws_ping_interval_seconds", 180)),
                ping_timeout_seconds=int(market_cfg.get("ws_ping_timeout_seconds", 30)),
            )
            console_cfg = self.config.get("console", {})
            self.status_reporter = HumanConsoleReporter(
                entry_engine=self.entry_engine,
                registry=self.registry,
                cycle_manager=self.cycle_manager,
                ws_status=lambda: self.ws_manager.status if self.ws_manager else "starting",
                uptime_seconds=lambda: time.monotonic() - self.started_monotonic,
                last_tick_age_seconds=self._last_tick_age_seconds,
                last_price=lambda: self.last_price,
                interval_seconds=int(console_cfg.get("interval_seconds", 60)),
                max_market_data_age_seconds=int(self.config["market_data"].get("max_market_data_age_seconds", 60)),
            )
            self.status_reporter.start()
            self.logger.system("monitor_starting", symbol=self.config["symbol"], streams=streams)
            self.ws_manager.run_forever()
        finally:
            if self.status_reporter:
                self.status_reporter.stop()
            self.market_shadow.stop()
            self.market_shadow_ge30.stop()
            self.telemetry_writer.stop()

    def _validate_startup(self) -> None:
        self.client.require_credentials()
        if self.config.get("position_mode") != "bot_exit_only":
            self.client.validate_trailing_delta(
                str(self.config["symbol"]),
                int(self.config["exit_server_simple_trail"]["trailing_delta_bips"]),
            )
        self.logger.system("startup_validation_ok", symbol=self.config["symbol"])

    def _load_historical_candles(self) -> None:
        market_cfg = self.config["market_data"]
        limits = market_cfg.get("historical_klines_limit", {})
        symbol = str(self.config["symbol"])
        self.logger.system("loading_historical_candles", symbol=symbol)
        for timeframe in self.entry_engine.required_timeframes():
            klines = self.market_data_client.klines(
                symbol=symbol,
                interval=timeframe,
                limit=int(limits.get(timeframe, 120)),
            )
            self.entry_engine.load_history(timeframe, klines, now_ms=self._server_now_ms())
            self.dmi15_trajectory_context_shadow.engine.load_history(
                timeframe, klines, now_ms=self._server_now_ms()
            )
            self.slow_ge_context_shadow.engine.load_history(
                timeframe, klines, now_ms=self._server_now_ms()
            )

    def _server_now_ms(self) -> int:
        import time

        return int(time.time() * 1000)

    def _on_ws_event(self, stream: str, payload: Dict[str, Any]) -> None:
        self.market_shadow.on_ws_event(stream, payload)
        market_shadow_ge30 = getattr(self, "market_shadow_ge30", None)
        if market_shadow_ge30:
            kline = payload.get("k") if isinstance(payload.get("k"), dict) else {}
            boundary_ms = int(kline.get("T", payload.get("T", payload.get("E", 0)))) + (
                1 if kline else 0
            )
            market_shadow_ge30.sync_selection_from_source(boundary_ms)
            market_shadow_ge30.on_ws_event(stream, payload)
        live_symbol = str(self.config["symbol"]).lower()
        if stream == f"{live_symbol}@aggTrade":
            import time

            price = float(payload["p"])
            self.last_price = price
            self.last_tick_monotonic = time.monotonic()
            self.registry.on_tick(price, market_ts=_market_timestamp(payload))
            try:
                self.h2_exposure_shadow.on_tick(price, _market_timestamp(payload))
            except Exception as exc:
                self.logger.system("h2_exposure_shadow_tick_failed", price=price, error=str(exc))
            try:
                self.gcr_shadow.on_tick(price, _market_timestamp(payload))
            except Exception as exc:
                self.logger.system("gcr_shadow_tick_failed", price=price, error=str(exc))
            try:
                self.dmi15_shadow.on_tick(price, _market_timestamp(payload))
            except Exception as exc:
                self.logger.system("dmi15_shadow_tick_failed", price=price, error=str(exc))
            try:
                self.dmi15_spread_shadow.on_tick(price, _market_timestamp(payload))
            except Exception as exc:
                self.logger.system("dmi15_spread_shadow_tick_failed", price=price, error=str(exc))
            for name, shadow in (
                ("dmi15_trajectory_shadow", self.dmi15_trajectory_shadow),
                ("dmi15_rsi70_shadow", self.dmi15_rsi70_shadow),
                ("dmi15_combined_shadow", self.dmi15_combined_shadow),
            ):
                try:
                    shadow.on_tick(price, _market_timestamp(payload))
                except Exception as exc:
                    self.logger.system(f"{name}_tick_failed", price=price, error=str(exc))
            for name, shadow in (
                ("dmi15_trajectory_context_shadow", self.dmi15_trajectory_context_shadow),
                ("slow_ge_context_shadow", self.slow_ge_context_shadow),
            ):
                try:
                    shadow.on_tick(price, _market_timestamp(payload))
                except Exception as exc:
                    self.logger.system(f"{name}_tick_failed", price=price, error=str(exc))
            self._stop_after_cycle_if_needed()
            return

        if stream.startswith(f"{live_symbol}@") and "@kline_" in stream:
            if self._entry_should_pause(stream, payload):
                return
            signal = self.entry_engine.on_kline(stream, payload)
            kline = payload.get("k") if isinstance(payload.get("k"), dict) else {}
            timeframe = stream.rsplit("@kline_", 1)[-1]
            if bool(kline.get("x")) and timeframe in ("5m", "15m"):
                snapshot = self._safe_refresh_market_context()
                if timeframe == "5m":
                    self.registry.record_market_context(snapshot)
                    self.gcr_shadow.record_market_context(snapshot)
                    try:
                        self.dmi15_shadow.on_closed_5m(
                            snapshot,
                            entry_atr=self.entry_engine._current_entry_atr(),
                            atr_timeframe=self.entry_engine.entry_timeframe,
                            atr_period=int(self.config["entry"]["atr_period"]),
                        )
                    except Exception as exc:
                        self.logger.system("dmi15_shadow_candle_failed", error=str(exc))
                    try:
                        self.dmi15_spread_shadow.on_closed_5m(
                            snapshot,
                            entry_atr=self.entry_engine._current_entry_atr(),
                            atr_timeframe=self.entry_engine.entry_timeframe,
                            atr_period=int(self.config["entry"]["atr_period"]),
                        )
                    except Exception as exc:
                        self.logger.system("dmi15_spread_shadow_candle_failed", error=str(exc))
                    for name, shadow in (
                        ("dmi15_trajectory_shadow", self.dmi15_trajectory_shadow),
                        ("dmi15_rsi70_shadow", self.dmi15_rsi70_shadow),
                        ("dmi15_combined_shadow", self.dmi15_combined_shadow),
                    ):
                        try:
                            shadow.on_closed_5m(
                                snapshot,
                                entry_atr=self.entry_engine._current_entry_atr(),
                                atr_timeframe=self.entry_engine.entry_timeframe,
                                atr_period=int(self.config["entry"]["atr_period"]),
                            )
                        except Exception as exc:
                            self.logger.system(f"{name}_candle_failed", error=str(exc))
            else:
                snapshot = self.market_context.latest
            for name, shadow in (
                ("dmi15_trajectory_context_shadow", self.dmi15_trajectory_context_shadow),
                ("slow_ge_context_shadow", self.slow_ge_context_shadow),
            ):
                try:
                    shadow.on_kline(stream, payload, snapshot)
                except Exception as exc:
                    self.logger.system(f"{name}_candle_failed", error=str(exc))
            if signal is not None and self._entry_operational_pause_reason() is not None:
                signal = None
            if signal is not None:
                try:
                    snapshot = self.market_context.latest or self._safe_refresh_market_context()
                    try:
                        self.gcr_shadow.on_signal(signal, snapshot)
                    except Exception as exc:
                        self.logger.system(
                            "gcr_shadow_signal_failed", signal_price=signal.price, error=str(exc)
                        )
                    try:
                        self.h2_exposure_shadow.on_signal(signal)
                    except Exception as exc:
                        self.logger.system(
                            "h2_exposure_shadow_signal_failed", signal_price=signal.price, error=str(exc)
                        )
                    self.registry.open_pair(signal, snapshot)
                except BinanceClientError as exc:
                    self.logger.system("order_rejected", error=str(exc), signal_price=signal.price)

    def _safe_refresh_market_context(self) -> Dict[str, Any] | None:
        try:
            return self.market_context.refresh()
        except Exception as exc:
            self.logger.system("market_context_refresh_failed", error=str(exc))
            return self.market_context.latest

    def _market_streams(self) -> list[str]:
        market_cfg = self.config["market_data"]
        market_shadow_ge30 = getattr(self, "market_shadow_ge30", None)
        streams = [
            market_cfg["trade_stream"],
            *market_cfg["kline_streams"],
            *self.market_shadow.required_streams(),
            *(market_shadow_ge30.required_streams() if market_shadow_ge30 else []),
        ]
        return list(dict.fromkeys(str(stream) for stream in streams))

    def _refresh_market_streams(self) -> None:
        if self.ws_manager:
            self.ws_manager.update_streams(self._market_streams())

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Arquivo de configuracao nao encontrado: {path}")
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}

    def _last_tick_age_seconds(self) -> float | None:
        if self.last_tick_monotonic is None:
            return None
        import time

        return time.monotonic() - self.last_tick_monotonic

    def _entry_should_pause(self, stream: str, payload: Dict[str, Any]) -> bool:
        if not stream.endswith(f"@kline_{self.config['entry']['timeframe']}"):
            return False
        kline = payload.get("k") or {}
        if not bool(kline.get("x")):
            return True
        return self._entry_operational_pause_reason() is not None

    def _entry_operational_pause_reason(self) -> str | None:
        if self.cycle_manager.single_cycle_complete:
            self.entry_engine.set_paused("PAUSED_CYCLE_COMPLETE")
            return "PAUSED_CYCLE_COMPLETE"
        if self.registry.review_required:
            self.entry_engine.set_paused("PAUSED_NEEDS_REVIEW")
            return "PAUSED_NEEDS_REVIEW"
        if self.ws_manager and self.ws_manager.status != "connected":
            self.entry_engine.set_paused("PAUSED_WEBSOCKET")
            return "PAUSED_WEBSOCKET"
        age = self._last_tick_age_seconds()
        max_age = int(self.config["market_data"].get("max_market_data_age_seconds", 60))
        if age is not None and age > max_age:
            self.entry_engine.set_paused("PAUSED_MARKET_DATA_STALE")
            return "PAUSED_MARKET_DATA_STALE"
        return None

    def _entry_console_context(self) -> Dict[str, Any]:
        cycle_total = self.cycle_manager.pairs_per_cycle
        cycle_done = cycle_total if self.cycle_manager.single_cycle_complete else self.cycle_manager.closed_pairs_in_current_cycle
        return {
            "gates": _gate_status(self.entry_engine.last_diagnostic.get("gates", {})),
            "open_pairs": self.registry.open_pair_count,
            "cycle": f"{cycle_done}/{cycle_total}",
        }

    def _stop_after_cycle_if_needed(self) -> None:
        run_cfg = self.config.get("run_control", {})
        should_stop = bool(run_cfg.get("stop_after_cycle_complete", False))
        if not should_stop or not self.cycle_manager.single_cycle_complete:
            return
        if self._cycle_stop_announced:
            return
        self._cycle_stop_announced = True
        self.entry_engine.set_paused("PAUSED_CYCLE_COMPLETE")
        self.logger.system(
            "single_cycle_complete",
            completed_cycles=self.cycle_manager.completed_cycles,
            closed_pairs=len(self.cycle_manager.closed_pair_ids),
        )
        print(console_line("[SYSTEM] single_cycle concluido; monitor encerrando."), flush=True)
        if self.ws_manager:
            self.ws_manager.stop()


def monitor() -> None:
    Monitor().run()


def main() -> None:
    try:
        monitor()
    except KeyboardInterrupt:
        print(console_line("[INFO] Interrupcao recebida. Monitor encerrado."))
    except Exception as exc:
        print(console_line(f"[ERRO] O monitor parou antes de subir: {exc}"))
        print(console_line("[DICA] Confira o .env, a internet e as chaves BINANCE_TESTNET_API_KEY/BINANCE_TESTNET_API_SECRET."))


def _gate_status(gates: Dict[str, Any]) -> str:
    labels = [
        ("trend", "T"),
        ("pullback", "P"),
        ("exhaustion", "E"),
        ("recovery", "R"),
    ]
    return "".join(f"{short}{_gate_mark((gates.get(name) or {}).get('passed'))}" for name, short in labels)


def _gate_mark(value: Any) -> str:
    if value is True:
        return "+"
    if value is False:
        return "-"
    return "."


def _run_id(config: Dict[str, Any]) -> str:
    strategy = str(config.get("strategy_version", "strategy")).replace(".", "_")
    from datetime import datetime

    return f"{datetime.now(BRASILIA_TZ).strftime('%Y%m%d_%H%M')}_{strategy}"


def _market_timestamp(payload: Dict[str, Any]) -> str:
    raw = payload.get("T", payload.get("E"))
    try:
        return datetime.fromtimestamp(float(raw) / 1000, timezone.utc).isoformat(timespec="milliseconds")
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


if __name__ == "__main__":
    main()
