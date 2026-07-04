from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PROJECT_ROOT / "config" / "config.yaml"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.exchange.binance_client import BinanceClient, BinanceClientError
from src.logging_utils import JsonlLogger
from src.monitor.cycle_manager import CycleManager
from src.monitor.entry_engine import EntryEngine
from src.monitor.position_registry import PositionRegistry
from src.monitor.ws_manager import WSManager
from src.project_env import load_project_env


class Monitor:
    def __init__(self, config_path: Path = CONFIG_FILE) -> None:
        load_project_env()
        self.project_root = PROJECT_ROOT
        self.config = self._load_yaml(config_path)
        self.logger = JsonlLogger(self.project_root, self.config)
        execution_cfg = self.config["execution"]
        self.client = BinanceClient(
            base_url=execution_cfg["testnet_url"],
            recv_window_ms=int(execution_cfg["recv_window_ms"]),
            use_server_time_sync=bool(execution_cfg.get("use_server_time_sync", True)),
        )
        self.cycle_manager = CycleManager(self.project_root, self.config, self.logger)
        self.registry = PositionRegistry(self.config, self.client, self.logger, self.cycle_manager)
        self.entry_engine = EntryEngine(str(self.config["symbol"]), self.config, self.logger)

    def run(self) -> None:
        self._validate_startup()
        market_cfg = self.config["market_data"]
        streams = [market_cfg["trade_stream"], *market_cfg["kline_streams"]]
        manager = WSManager(market_cfg["ws_url"], streams, self.logger, self._on_ws_event)
        self.logger.system("monitor starting", symbol=self.config["symbol"], streams=streams)
        manager.run_forever()

    def _validate_startup(self) -> None:
        self.client.require_credentials()
        self.client.validate_trailing_delta(
            str(self.config["symbol"]),
            int(self.config["exit_server_simple_trail"]["trailing_delta_bips"]),
        )
        self.logger.system("startup validation ok", symbol=self.config["symbol"])

    def _on_ws_event(self, stream: str, payload: Dict[str, Any]) -> None:
        if stream.endswith("@aggTrade"):
            price = float(payload["p"])
            self.registry.on_tick(price)
            return

        if "@kline_" in stream:
            signal = self.entry_engine.on_kline(stream, payload)
            if signal is not None:
                try:
                    self.registry.open_pair(signal)
                except BinanceClientError as exc:
                    self.logger.system("order rejected", error=str(exc), signal_price=signal.price)

    @staticmethod
    def _load_yaml(path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Arquivo de configuracao nao encontrado: {path}")
        with path.open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle) or {}


def monitor() -> None:
    Monitor().run()


def main() -> None:
    try:
        monitor()
    except KeyboardInterrupt:
        print("[INFO] Interrupcao recebida. Monitor encerrado.")


if __name__ == "__main__":
    main()
