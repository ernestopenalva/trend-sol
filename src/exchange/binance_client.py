from __future__ import annotations

import hashlib
import hmac
import math
import os
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, Optional
from urllib.parse import urlencode

import requests


class BinanceClientError(Exception):
    pass


@dataclass(frozen=True)
class SymbolFilters:
    min_qty: Decimal
    step_size: Decimal
    min_notional: Decimal
    min_trailing_above_delta: int
    max_trailing_above_delta: int
    min_trailing_below_delta: int
    max_trailing_below_delta: int


class BinanceClient:
    def __init__(self, base_url: str, recv_window_ms: int, use_server_time_sync: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = int(recv_window_ms)
        self.api_key = os.getenv("BINANCE_TESTNET_API_KEY", "")
        self.api_secret = os.getenv("BINANCE_TESTNET_API_SECRET", "")
        self.time_offset_ms = 0
        self.session = requests.Session()
        if self.api_key:
            self.session.headers.update({"X-MBX-APIKEY": self.api_key})
        if use_server_time_sync:
            self.sync_time()

    def require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise BinanceClientError(
                "BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET must be set in .env"
            )

    def sync_time(self) -> None:
        payload = self._request("GET", "/api/v3/time", signed=False)
        server_time = int(payload["serverTime"])
        local_time = int(time.time() * 1000)
        self.time_offset_ms = server_time - local_time

    def exchange_info(self, symbol: str) -> Dict[str, Any]:
        return self._request("GET", "/api/v3/exchangeInfo", signed=False, params={"symbol": symbol})

    def symbol_filters(self, symbol: str) -> SymbolFilters:
        data = self.exchange_info(symbol)
        symbols = data.get("symbols") or []
        if not symbols:
            raise BinanceClientError(f"symbol not found in exchangeInfo: {symbol}")

        filters = {item.get("filterType"): item for item in symbols[0].get("filters", [])}
        lot_size = filters.get("LOT_SIZE") or {}
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        trailing = filters.get("TRAILING_DELTA") or {}
        return SymbolFilters(
            min_qty=Decimal(str(lot_size.get("minQty", "0"))),
            step_size=Decimal(str(lot_size.get("stepSize", "0.000001"))),
            min_notional=Decimal(str(notional.get("minNotional", "0"))),
            min_trailing_above_delta=int(trailing.get("minTrailingAboveDelta", 0)),
            max_trailing_above_delta=int(trailing.get("maxTrailingAboveDelta", 10_000_000)),
            min_trailing_below_delta=int(trailing.get("minTrailingBelowDelta", 0)),
            max_trailing_below_delta=int(trailing.get("maxTrailingBelowDelta", 10_000_000)),
        )

    def validate_trailing_delta(self, symbol: str, trailing_delta_bips: int) -> None:
        filters = self.symbol_filters(symbol)
        if not (filters.min_trailing_below_delta <= trailing_delta_bips <= filters.max_trailing_below_delta):
            raise BinanceClientError(
                "trailing_delta_bips outside TRAILING_DELTA below range: "
                f"{trailing_delta_bips} not in "
                f"{filters.min_trailing_below_delta}-{filters.max_trailing_below_delta}"
            )

    def normalize_quantity(self, symbol: str, quantity: float) -> str:
        filters = self.symbol_filters(symbol)
        qty = Decimal(str(quantity))
        if qty < filters.min_qty:
            raise BinanceClientError(f"quantity below LOT_SIZE minQty: {qty} < {filters.min_qty}")
        steps = (qty / filters.step_size).to_integral_value(rounding=ROUND_DOWN)
        normalized = steps * filters.step_size
        if normalized <= 0:
            raise BinanceClientError("normalized quantity is zero")
        return format(normalized.normalize(), "f")

    def validate_notional(self, symbol: str, quantity: float, price: float) -> None:
        filters = self.symbol_filters(symbol)
        notional = Decimal(str(quantity)) * Decimal(str(price))
        if notional < filters.min_notional:
            raise BinanceClientError(f"notional below minimum: {notional} < {filters.min_notional}")

    def account(self) -> Dict[str, Any]:
        self.require_credentials()
        return self._request("GET", "/api/v3/account", signed=True)

    def market_buy_quote(self, symbol: str, quote_order_qty: float, client_order_id: str) -> Dict[str, Any]:
        self.require_credentials()
        return self._request(
            "POST",
            "/api/v3/order",
            signed=True,
            params={
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": _plain_number(quote_order_qty),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
        )

    def market_sell(self, symbol: str, quantity: float, client_order_id: str) -> Dict[str, Any]:
        self.require_credentials()
        return self._request(
            "POST",
            "/api/v3/order",
            signed=True,
            params={
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": self.normalize_quantity(symbol, quantity),
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
        )

    def trailing_sell(
        self,
        symbol: str,
        quantity: float,
        trailing_delta_bips: int,
        order_type: str,
        client_order_id: str,
        limit_price: Optional[float] = None,
    ) -> Dict[str, Any]:
        self.require_credentials()
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": "SELL",
            "type": order_type,
            "quantity": self.normalize_quantity(symbol, quantity),
            "trailingDelta": int(trailing_delta_bips),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "FULL",
        }
        if order_type == "STOP_LOSS_LIMIT":
            if limit_price is None:
                raise BinanceClientError("STOP_LOSS_LIMIT fallback needs a limit price")
            params["price"] = _plain_number(limit_price)
            params["timeInForce"] = "GTC"
        return self._request("POST", "/api/v3/order", signed=True, params=params)

    def get_order(self, symbol: str, order_id: Optional[str] = None, client_order_id: Optional[str] = None) -> Dict[str, Any]:
        self.require_credentials()
        params: Dict[str, Any] = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return self._request("GET", "/api/v3/order", signed=True, params=params)

    def _request(
        self,
        method: str,
        path: str,
        signed: bool,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        request_params: Dict[str, Any] = dict(params or {})
        if signed:
            request_params["timestamp"] = int(time.time() * 1000) + self.time_offset_ms
            request_params["recvWindow"] = self.recv_window_ms
            query = urlencode(request_params)
            signature = hmac.new(self.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
            request_params["signature"] = signature

        response = self.session.request(method, f"{self.base_url}{path}", params=request_params, timeout=20)
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                time.sleep(max(1, math.ceil(float(retry_after))))
        if response.status_code >= 400:
            raise BinanceClientError(f"Binance error {response.status_code}: {response.text}")
        return response.json()


def _plain_number(value: float) -> str:
    return format(Decimal(str(value)).normalize(), "f")
