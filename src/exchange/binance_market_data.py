from __future__ import annotations

import json
from typing import Any, Dict, List

import requests


class BinanceMarketDataError(Exception):
    pass


class BinanceMarketDataClient:
    def __init__(self, base_url: str, timeout_seconds: int = 8) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = int(timeout_seconds)
        self.session = requests.Session()

    def get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        response = self.session.get(
            f"{self.base_url}{path}",
            params=params,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise BinanceMarketDataError(
                f"Binance market data error {response.status_code}: {response.text}"
            )
        return response.json()

    def klines(self, symbol: str, interval: str, limit: int) -> List[List[Any]]:
        data = self.get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": int(limit)},
        )
        if not isinstance(data, list):
            raise BinanceMarketDataError(f"unexpected klines response: {data}")
        return data

    def exchange_info(self) -> Dict[str, Any]:
        data = self.get(
            "/api/v3/exchangeInfo",
            {"permissions": "SPOT", "symbolStatus": "TRADING"},
        )
        if not isinstance(data, dict):
            raise BinanceMarketDataError(f"unexpected exchangeInfo response: {data}")
        return data

    def tickers_24h(self) -> List[Dict[str, Any]]:
        data = self.get("/api/v3/ticker/24hr", {"symbolStatus": "TRADING"})
        if not isinstance(data, list):
            raise BinanceMarketDataError(f"unexpected 24hr ticker response: {data}")
        return [item for item in data if isinstance(item, dict)]

    def rolling_tickers(
        self,
        symbols: List[str],
        window: str = "7d",
    ) -> List[Dict[str, Any]]:
        output = []
        for start in range(0, len(symbols), 100):
            data = self.get(
                "/api/v3/ticker",
                {
                    "symbols": json.dumps(symbols[start : start + 100], separators=(",", ":")),
                    "windowSize": window,
                    "type": "FULL",
                    "symbolStatus": "TRADING",
                },
            )
            if isinstance(data, dict):
                output.append(data)
            elif isinstance(data, list):
                output.extend(item for item in data if isinstance(item, dict))
            else:
                raise BinanceMarketDataError(f"unexpected rolling ticker response: {data}")
        return output

    def book_tickers(self) -> List[Dict[str, Any]]:
        data = self.get("/api/v3/ticker/bookTicker", {"symbolStatus": "TRADING"})
        if not isinstance(data, list):
            raise BinanceMarketDataError(f"unexpected bookTicker response: {data}")
        return [item for item in data if isinstance(item, dict)]
