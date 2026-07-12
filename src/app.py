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
from src.monitor.entry_engine import EntryEngine
from src.monitor.human_console_reporter import HumanConsoleReporter
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
        self.entry_engine = EntryEngine(str(self.config["symbol"]), self.config, self.logger)
        self.logger.set_entry_console_context(self._entry_console_context)

    def run(self) -> None:
        self.telemetry_writer.start()
        try:
            self.logger.system("validating_startup")
            self._validate_startup()
            self._load_historical_candles()
            market_cfg = self.config["market_data"]
            streams = [market_cfg["trade_stream"], *market_cfg["kline_streams"]]
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
        for timeframe in (str(self.config["trend"]["timeframe"]), str(self.config["entry"]["timeframe"])):
            klines = self.market_data_client.klines(
                symbol=symbol,
                interval=timeframe,
                limit=int(limits.get(timeframe, 120)),
            )
            self.entry_engine.load_history(timeframe, klines, now_ms=self._server_now_ms())

    def _server_now_ms(self) -> int:
        import time

        return int(time.time() * 1000)

    def _on_ws_event(self, stream: str, payload: Dict[str, Any]) -> None:
        if stream.endswith("@aggTrade"):
            import time

            price = float(payload["p"])
            self.last_price = price
            self.last_tick_monotonic = time.monotonic()
            self.registry.on_tick(price, market_ts=_market_timestamp(payload))
            self._stop_after_cycle_if_needed()
            return

        if "@kline_" in stream:
            if self._entry_should_pause(stream, payload):
                return
            signal = self.entry_engine.on_kline(stream, payload)
            if signal is not None:
                try:
                    self.registry.open_pair(signal)
                except BinanceClientError as exc:
                    self.logger.system("order_rejected", error=str(exc), signal_price=signal.price)

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
        if self.cycle_manager.single_cycle_complete:
            self.entry_engine.set_paused("PAUSED_CYCLE_COMPLETE")
            return True
        if self.registry.review_required:
            self.entry_engine.set_paused("PAUSED_NEEDS_REVIEW")
            return True
        if self.ws_manager and self.ws_manager.status != "connected":
            self.entry_engine.set_paused("PAUSED_WEBSOCKET")
            return True
        age = self._last_tick_age_seconds()
        max_age = int(self.config["market_data"].get("max_market_data_age_seconds", 60))
        if age is not None and age > max_age:
            self.entry_engine.set_paused("PAUSED_MARKET_DATA_STALE")
            return True
        return False

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
