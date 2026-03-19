"""
Roostoo API Client
Handles authentication, signing, and all API calls to the Roostoo Mock Exchange.
"""

import time
import hmac
import hashlib
import requests
from config import API_KEY, SECRET_KEY, BASE_URL


class RoostooClient:
    def __init__(self):
        self.api_key = API_KEY
        self.secret_key = SECRET_KEY
        self.base_url = BASE_URL
        self.session = requests.Session()

    def _timestamp(self):
        """Get current timestamp in 13-digit milliseconds."""
        return str(int(time.time() * 1000))

    def _sign(self, params: dict) -> str:
        """
        Create HMAC SHA256 signature.
        1. Sort params alphabetically
        2. Join as key=value with &
        3. Sign with secret key
        """
        sorted_params = sorted(params.items())
        query_string = "&".join(f"{k}={v}" for k, v in sorted_params)
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _headers(self, params: dict) -> dict:
        """Build authenticated headers."""
        return {
            "RST-API-KEY": self.api_key,
            "MSG-SIGNATURE": self._sign(params),
        }

    # ── Public Endpoints (No Auth) ──

    def get_server_time(self) -> dict:
        """Get server time to check connectivity."""
        resp = self.session.get(f"{self.base_url}/v3/serverTime")
        resp.raise_for_status()
        return resp.json()

    def get_exchange_info(self) -> dict:
        """Get all trading pairs, precision settings, min order amounts."""
        resp = self.session.get(f"{self.base_url}/v3/exchangeInfo")
        resp.raise_for_status()
        return resp.json()

    # ── Market Data (Timestamp Auth) ──

    def get_ticker(self, pair: str = None) -> dict:
        """
        Get market ticker data.
        Returns: MaxBid, MinAsk, LastPrice, Change, CoinTradeValue, UnitTradeValue
        """
        params = {"timestamp": self._timestamp()}
        if pair:
            params["pair"] = pair
        resp = self.session.get(f"{self.base_url}/v3/ticker", params=params)
        resp.raise_for_status()
        return resp.json()

    # ── Account Endpoints (Full Auth) ──

    def get_balance(self) -> dict:
        """Get account balances (free and locked per asset)."""
        params = {"timestamp": self._timestamp()}
        headers = self._headers(params)
        resp = self.session.get(
            f"{self.base_url}/v3/balance",
            params=params,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    def get_pending_orders(self) -> dict:
        """Get count of pending orders."""
        params = {"timestamp": self._timestamp()}
        headers = self._headers(params)
        resp = self.session.get(
            f"{self.base_url}/v3/pending_count",
            params=params,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    # ── Trading Endpoints (Full Auth) ──

    def place_order(self, pair: str, side: str, order_type: str,
                    quantity: float, price: float = None) -> dict:
        """
        Place a buy or sell order.

        Args:
            pair: Trading pair e.g. "BTC_USDT"
            side: "BUY" or "SELL"
            order_type: "LIMIT" or "MARKET"
            quantity: Amount to buy/sell
            price: Required for LIMIT orders
        """
        params = {
            "pair": pair,
            "side": side,
            "type": order_type,
            "quantity": str(quantity),
            "timestamp": self._timestamp(),
        }
        if price and order_type == "LIMIT":
            params["price"] = str(price)

        headers = self._headers(params)
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        resp = self.session.post(
            f"{self.base_url}/v3/place_order",
            data=params,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    def query_orders(self, pair: str = None, pending_only: bool = False,
                     limit: int = 50) -> dict:
        """Query order history."""
        params = {
            "timestamp": self._timestamp(),
        }
        if pair:
            params["pair"] = pair

        headers = self._headers(params)
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        resp = self.session.post(
            f"{self.base_url}/v3/query_order",
            data=params,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    def cancel_order(self, order_id: str) -> dict:
        """Cancel a pending order."""
        params = {
            "order_id": order_id,
            "timestamp": self._timestamp(),
        }
        headers = self._headers(params)
        headers["Content-Type"] = "application/x-www-form-urlencoded"

        resp = self.session.post(
            f"{self.base_url}/v3/cancel_order",
            data=params,
            headers=headers
        )
        resp.raise_for_status()
        return resp.json()

    # ── Helper Methods ──

    def buy(self, pair: str, quantity: float, price: float = None,
            order_type: str = "LIMIT") -> dict:
        """Shortcut to place a buy order."""
        return self.place_order(pair, "BUY", order_type, quantity, price)

    def sell(self, pair: str, quantity: float, price: float = None,
             order_type: str = "LIMIT") -> dict:
        """Shortcut to place a sell order."""
        return self.place_order(pair, "SELL", order_type, quantity, price)

    def get_price(self, pair: str) -> float:
        """Get current last price for a pair."""
        ticker = self.get_ticker(pair)
        if isinstance(ticker, dict):
            # Direct format
            if "LastPrice" in ticker:
                return float(ticker["LastPrice"])
            # Nested under 'Data' -> pair
            data = ticker.get("Data")
            if isinstance(data, dict) and pair in data:
                return float(data[pair]["LastPrice"])
            # Nested under pair name directly
            if pair in ticker:
                return float(ticker[pair]["LastPrice"])
        return 0.0
