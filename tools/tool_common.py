from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config_profiles import effective_config
from src.console_utils import console_line
from src.project_env import load_project_env


def load_config():
    import yaml

    with (PROJECT_ROOT / "config" / "config.yaml").open("r", encoding="utf-8") as handle:
        return effective_config(yaml.safe_load(handle) or {})


def build_client(config):
    from src.exchange.binance_client import BinanceClient

    execution = config["execution"]
    return BinanceClient(
        base_url=execution["testnet_url"],
        recv_window_ms=int(execution["recv_window_ms"]),
        use_server_time_sync=bool(execution.get("use_server_time_sync", True)),
        http_timeout_seconds=int(execution.get("http_timeout_seconds", 8)),
    )


def bootstrap():
    load_project_env()
    config = load_config()
    return config, build_client(config)


def print_line(message: str) -> None:
    print(console_line(message))


def confirm_or_exit(prompt: str, assume_yes: bool = False) -> None:
    if assume_yes:
        return
    print_line(prompt)
    answer = input(console_line("Digite YES para confirmar: ")).strip()
    if answer != "YES":
        print_line("Operacao cancelada.")
        raise SystemExit(1)
