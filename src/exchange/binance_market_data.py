from __future__ import annotations

from typing import Any, Dict, List

import requests


class BinanceMarketDataError(Exception):
    pass


class BinanceMarketDataClient:
    def __init__(self, base_url: str, timeout_seconds: int = 8) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = int(timeout_seconds)
        self.session = requests.Session()

    def klines(self, symbol: str, interval: str, limit: int) -> List[List[Any]]:
        response = self.session.get(
            f"{self.base_url}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": int(limit)},
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise BinanceMarketDataError(f"Binance market data error {response.status_code}: {response.text}")
        data = response.json()
        if not isinstance(data, list):
            raise BinanceMarketDataError(f"unexpected klines response: {data}")
        return data
