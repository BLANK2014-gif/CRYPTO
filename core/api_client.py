"""
Roostoo Trading Bot — API Client
==================================
All Roostoo exchange interaction is isolated here.
No other module should make direct HTTP requests to Roostoo.

Features:
- HMAC SHA-256 authentication
- Rate limiting (token bucket)
- Exponential backoff retry
- Request ID tracking
- Latency measurement
- Structured error handling
- Idempotency protection for orders
- Success/failure logging for every request
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import requests

from config.trading_params import BotConfig, RateLimitConfig

logger = logging.getLogger("api_client")


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------

class APIError(Exception):
    """Base API error."""
    def __init__(self, message: str, status_code: int = 0, response_body: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


class AuthenticationError(APIError):
    """API key / signature rejected."""
    pass


class RateLimitError(APIError):
    """Rate limit exceeded."""
    pass


class OrderError(APIError):
    """Order-specific error (insufficient balance, invalid pair, etc.)."""
    pass


class ExchangeMaintenanceError(APIError):
    """Exchange is down for maintenance."""
    pass


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class TokenBucketRateLimiter:
    """
    Thread-safe token bucket rate limiter.
    Separate buckets for general requests and order requests.
    """

    def __init__(self, config: RateLimitConfig):
        self._lock = threading.Lock()
        self._general_tokens: float = config.max_requests_per_minute
        self._general_max: int = config.max_requests_per_minute
        self._order_tokens: float = config.max_order_requests_per_minute
        self._order_max: int = config.max_order_requests_per_minute
        self._last_refill: float = time.monotonic()
        self._refill_rate_general: float = config.max_requests_per_minute / 60.0
        self._refill_rate_order: float = config.max_order_requests_per_minute / 60.0

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._general_tokens = min(
            self._general_max,
            self._general_tokens + elapsed * self._refill_rate_general,
        )
        self._order_tokens = min(
            self._order_max,
            self._order_tokens + elapsed * self._refill_rate_order,
        )
        self._last_refill = now

    def acquire(self, is_order: bool = False) -> float:
        """
        Acquire a token. Returns wait time in seconds (0 if immediately available).
        Blocks until token is available.
        """
        with self._lock:
            self._refill()

            # Check general bucket
            if self._general_tokens < 1.0:
                wait = (1.0 - self._general_tokens) / self._refill_rate_general
                return wait

            # Check order bucket for order requests
            if is_order and self._order_tokens < 1.0:
                wait = (1.0 - self._order_tokens) / self._refill_rate_order
                return wait

            self._general_tokens -= 1.0
            if is_order:
                self._order_tokens -= 1.0
            return 0.0

    def wait_and_acquire(self, is_order: bool = False) -> None:
        """Block until a token is available, then consume it."""
        while True:
            wait = self.acquire(is_order)
            if wait <= 0:
                return
            logger.debug("Rate limiter: waiting %.2fs", wait)
            time.sleep(wait)

    @property
    def tokens_remaining(self) -> Tuple[float, float]:
        """Return (general_tokens, order_tokens)."""
        with self._lock:
            self._refill()
            return self._general_tokens, self._order_tokens


# ---------------------------------------------------------------------------
# API Response
# ---------------------------------------------------------------------------

@dataclass
class APIResponse:
    """Structured API response."""
    success: bool
    data: Dict[str, Any]
    status_code: int = 200
    latency_ms: float = 0.0
    request_id: str = ""
    error_message: str = ""
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Roostoo API Client
# ---------------------------------------------------------------------------

class RoostooAPIClient:
    """
    Complete Roostoo exchange API client.

    All exchange interaction goes through this class.
    Thread-safe. Rate-limited. Retry-capable.
    """

    def __init__(self, config: BotConfig):
        self._config = config
        self._base_url = config.exchange.base_url.rstrip("/")
        self._api_key = config.secrets.api_key
        self._secret_key = config.secrets.secret_key
        self._rate_limiter = TokenBucketRateLimiter(config.rate_limit)
        self._session = requests.Session()
        self._session.timeout = 15  # seconds

        # Backoff config
        self._backoff_base = config.rate_limit.backoff_base_seconds
        self._backoff_max = config.rate_limit.backoff_max_seconds
        self._backoff_mult = config.rate_limit.backoff_multiplier

        # Track recent order IDs to prevent duplicates
        self._recent_order_ids: set[int] = set()
        self._lock = threading.Lock()

        # Server time offset (for timestamp sync)
        self._server_time_offset_ms: int = 0

        # Statistics
        self._total_requests: int = 0
        self._failed_requests: int = 0
        self._last_request_time: float = 0.0
        self._last_error: str = ""

        logger.info(
            "RoostooAPIClient initialized — base_url=%s api_key=%s...",
            self._base_url,
            self._api_key[:8] + "..." if len(self._api_key) > 8 else "[not set]",
        )

    # -- Authentication --

    def _generate_signature(self, params: Dict[str, Any]) -> str:
        """Generate HMAC SHA-256 signature from sorted params."""
        sorted_keys = sorted(params.keys())
        query_string = "&".join(f"{k}={params[k]}" for k in sorted_keys)
        sig = hmac.new(
            self._secret_key.encode("utf-8"),
            query_string.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return sig

    def _get_timestamp(self) -> str:
        """Get 13-digit millisecond timestamp adjusted for server offset."""
        ts = int(time.time() * 1000) + self._server_time_offset_ms
        return str(ts)

    def _signed_headers(self, params: Dict[str, Any]) -> Dict[str, str]:
        """Build headers for RCL_TopLevelCheck endpoints."""
        return {
            "RST-API-KEY": self._api_key,
            "MSG-SIGNATURE": self._generate_signature(params),
        }

    # -- Core request methods --

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        signed: bool = False,
        is_order: bool = False,
        max_retries: int = 3,
    ) -> APIResponse:
        """
        Core request method with rate limiting, retries, and error handling.

        Never call this from outside api_client.py directly for trading.
        """
        request_id = str(uuid.uuid4())[:12]

        if params is None:
            params = {}

        # Add timestamp for signed requests
        if signed or path == "/v3/ticker":
            params["timestamp"] = self._get_timestamp()

        # Rate limit
        self._rate_limiter.wait_and_acquire(is_order=is_order)

        url = f"{self._base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(1, max_retries + 1):
            start_time = time.monotonic()

            try:
                if signed:
                    # Refresh timestamp on retry
                    if attempt > 1:
                        params["timestamp"] = self._get_timestamp()

                    headers = self._signed_headers(params)

                    if method == "GET":
                        resp = self._session.get(
                            url, params=params, headers=headers, timeout=15,
                        )
                    else:
                        headers["Content-Type"] = "application/x-www-form-urlencoded"
                        # Build body as sorted query string (per API docs)
                        sorted_keys = sorted(params.keys())
                        body = "&".join(f"{k}={params[k]}" for k in sorted_keys)
                        resp = self._session.post(
                            url, data=body, headers=headers, timeout=15,
                        )
                else:
                    if method == "GET":
                        resp = self._session.get(url, params=params, timeout=15)
                    else:
                        resp = self._session.post(url, data=params, timeout=15)

                latency_ms = (time.monotonic() - start_time) * 1000
                self._total_requests += 1
                self._last_request_time = time.time()

                # Parse response
                try:
                    data = resp.json()
                except ValueError:
                    logger.error(
                        "[%s] Invalid JSON response — status=%d body=%s",
                        request_id, resp.status_code, resp.text[:200],
                    )
                    self._failed_requests += 1
                    self._last_error = f"Invalid JSON: {resp.text[:100]}"
                    raise APIError(
                        f"Invalid JSON response from {path}",
                        status_code=resp.status_code,
                        response_body=resp.text[:500],
                    )

                # Check HTTP status
                if resp.status_code in (401, 403):
                    logger.warning("[%s] Authentication failed on %s: status=%d", request_id, path, resp.status_code)
                    self._failed_requests += 1
                    raise AuthenticationError(
                        f"Authentication failed: {resp.text[:100]}",
                        status_code=resp.status_code,
                    )

                if resp.status_code == 429:
                    logger.warning("[%s] Rate limited on %s", request_id, path)
                    self._failed_requests += 1
                    raise RateLimitError("Rate limited", status_code=429)

                if resp.status_code >= 500:
                    logger.warning(
                        "[%s] Server error %d on %s", request_id, resp.status_code, path,
                    )
                    raise ExchangeMaintenanceError(
                        f"Server error {resp.status_code}",
                        status_code=resp.status_code,
                    )

                # Check Roostoo-level success
                success = data.get("Success", True)
                err_msg = data.get("ErrMsg", "")

                if not success and err_msg:
                    # Don't retry business logic errors
                    if any(
                        msg in err_msg.lower()
                        for msg in [
                            "insufficient balance",
                            "pair not found",
                            "no permission",
                            "not allow",
                            "minimum",
                        ]
                    ):
                        logger.warning(
                            "[%s] Order rejected: %s", request_id, err_msg,
                        )
                        return APIResponse(
                            success=False,
                            data=data,
                            status_code=resp.status_code,
                            latency_ms=latency_ms,
                            request_id=request_id,
                            error_message=err_msg,
                            raw_text=resp.text,
                        )

                # Log success
                logger.debug(
                    "[%s] %s %s — %dms success=%s",
                    request_id, method, path, int(latency_ms), success,
                )

                return APIResponse(
                    success=success if isinstance(success, bool) else True,
                    data=data,
                    status_code=resp.status_code,
                    latency_ms=latency_ms,
                    request_id=request_id,
                    error_message=err_msg,
                    raw_text=resp.text,
                )

            except (
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.ChunkedEncodingError,
            ) as exc:
                latency_ms = (time.monotonic() - start_time) * 1000
                last_error = exc
                self._failed_requests += 1
                self._last_error = str(exc)

                if is_order and attempt > 1:
                    # CRITICAL: Do NOT retry order submissions blindly
                    logger.error(
                        "[%s] Order request failed on retry — NOT retrying to prevent duplicates: %s",
                        request_id, exc,
                    )
                    raise APIError(
                        f"Order request failed (not retrying): {exc}",
                        status_code=0,
                    ) from exc

                # Exponential backoff
                backoff = min(
                    self._backoff_base * (self._backoff_mult ** (attempt - 1)),
                    self._backoff_max,
                )
                logger.warning(
                    "[%s] %s %s attempt %d/%d failed: %s — retrying in %.1fs",
                    request_id, method, path, attempt, max_retries, exc, backoff,
                )
                time.sleep(backoff)

            except RateLimitError:
                # Back off significantly
                backoff = min(
                    self._backoff_base * (self._backoff_mult ** attempt) * 5,
                    self._backoff_max,
                )
                logger.warning(
                    "[%s] Rate limited — backing off %.1fs", request_id, backoff,
                )
                time.sleep(backoff)
                last_error = RateLimitError("Rate limited")

            except ExchangeMaintenanceError as exc:
                last_error = exc
                backoff = min(
                    self._backoff_base * (self._backoff_mult ** attempt) * 10,
                    self._backoff_max,
                )
                logger.warning(
                    "[%s] Exchange error — backing off %.1fs", request_id, backoff,
                )
                time.sleep(backoff)

        # All retries exhausted
        self._failed_requests += 1
        error_msg = f"All {max_retries} retries exhausted for {method} {path}"
        if last_error:
            error_msg += f": {last_error}"
        logger.error("[%s] %s", request_id, error_msg)
        raise APIError(error_msg)

    # =======================================================================
    # PUBLIC ENDPOINTS (no auth required)
    # =======================================================================

    def get_server_time(self) -> APIResponse:
        """GET /v3/serverTime — no auth required."""
        return self._request("GET", "/v3/serverTime")

    def sync_server_time(self) -> int:
        """
        Synchronize local clock with server.
        Returns offset in milliseconds.
        """
        local_before = int(time.time() * 1000)
        resp = self.get_server_time()
        local_after = int(time.time() * 1000)

        if resp.success and "ServerTime" in resp.data:
            server_time = resp.data["ServerTime"]
            local_mid = (local_before + local_after) // 2
            self._server_time_offset_ms = server_time - local_mid
            logger.info(
                "Time sync — server=%d local=%d offset=%dms",
                server_time, local_mid, self._server_time_offset_ms,
            )
            return self._server_time_offset_ms
        else:
            logger.warning("Time sync failed — using local clock")
            return 0

    def get_exchange_info(self) -> APIResponse:
        """GET /v3/exchangeInfo — trading pairs, precision rules."""
        return self._request("GET", "/v3/exchangeInfo")

    def get_ticker(self, pair: Optional[str] = None) -> APIResponse:
        """
        GET /v3/ticker — market data.

        Returns: MaxBid, MinAsk, LastPrice, Change, CoinTradeValue, UnitTradeValue
        """
        params: Dict[str, Any] = {}
        if pair:
            params["pair"] = pair
        # ticker requires RCL_TSCheck (timestamp but no signature)
        params["timestamp"] = self._get_timestamp()
        return self._request("GET", "/v3/ticker", params=params, signed=False)

    # =======================================================================
    # SIGNED ENDPOINTS — Account
    # =======================================================================

    def get_balance(self) -> APIResponse:
        """GET /v3/balance — wallet balances (Free/Lock per coin)."""
        return self._request("GET", "/v3/balance", signed=True)

    def get_pending_count(self) -> APIResponse:
        """GET /v3/pending_count — count of pending orders."""
        return self._request("GET", "/v3/pending_count", signed=True)

    # =======================================================================
    # SIGNED ENDPOINTS — Trading
    # =======================================================================

    def place_order(
        self,
        pair: str,
        side: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> APIResponse:
        """
        POST /v3/place_order — place a buy/sell order.

        Args:
            pair: e.g. "BTC/USD"
            side: "BUY" or "SELL"
            quantity: amount of the coin
            order_type: "MARKET" or "LIMIT"
            price: required for LIMIT orders
        """
        params: Dict[str, Any] = {
            "pair": pair,
            "side": side.upper(),
            "type": order_type.upper(),
            "quantity": str(quantity),
        }

        if order_type.upper() == "LIMIT":
            if price is None:
                raise OrderError("LIMIT orders require a price")
            params["price"] = str(price)

        logger.info(
            "Placing order: pair=%s side=%s type=%s qty=%s price=%s",
            pair, side, order_type, quantity, price,
        )

        resp = self._request(
            "POST", "/v3/place_order",
            params=params, signed=True, is_order=True, max_retries=1,
        )

        # Track order ID for duplicate prevention
        if resp.success and "OrderDetail" in resp.data:
            order_id = resp.data["OrderDetail"].get("OrderID")
            if order_id:
                with self._lock:
                    self._recent_order_ids.add(order_id)
                    # Keep only last 100 order IDs
                    if len(self._recent_order_ids) > 100:
                        oldest = sorted(self._recent_order_ids)[:50]
                        self._recent_order_ids -= set(oldest)

        return resp

    def query_order(
        self,
        order_id: Optional[int] = None,
        pair: Optional[str] = None,
        pending_only: Optional[bool] = None,
        limit: Optional[int] = None,
    ) -> APIResponse:
        """POST /v3/query_order — query order status/history."""
        params: Dict[str, Any] = {}
        if order_id is not None:
            params["order_id"] = str(order_id)
        else:
            if pair:
                params["pair"] = pair
            if pending_only is not None:
                params["pending_only"] = "TRUE" if pending_only else "FALSE"
            if limit is not None:
                params["limit"] = str(limit)

        return self._request("POST", "/v3/query_order", params=params, signed=True)

    def cancel_order(
        self,
        order_id: Optional[int] = None,
        pair: Optional[str] = None,
    ) -> APIResponse:
        """
        POST /v3/cancel_order — cancel pending orders.

        If neither order_id nor pair is given, cancels ALL pending orders.
        """
        params: Dict[str, Any] = {}
        if order_id is not None:
            params["order_id"] = str(order_id)
        elif pair:
            params["pair"] = pair

        logger.info(
            "Cancelling order: order_id=%s pair=%s",
            order_id, pair,
        )

        return self._request(
            "POST", "/v3/cancel_order",
            params=params, signed=True, is_order=True, max_retries=2,
        )

    # =======================================================================
    # SIGNED ENDPOINTS — Shorting (v6)
    # =======================================================================

    def short_open(
        self,
        pair: str,
        collateral: float,
        price: Optional[float] = None,
    ) -> APIResponse:
        """
        POST /v6/short_open — open a short position.

        Sized by collateral (USD), not quantity.
        """
        params: Dict[str, Any] = {
            "pair": pair,
            "collateral": str(collateral),
        }
        if price is not None:
            params["order_type"] = "LIMIT"
            params["price"] = str(price)

        logger.info(
            "Opening short: pair=%s collateral=%s price=%s",
            pair, collateral, price,
        )

        return self._request(
            "POST", "/v6/short_open",
            params=params, signed=True, is_order=True, max_retries=1,
        )

    def short_close(
        self,
        pair: str,
        close_qty: Optional[float] = None,
        close_pct: Optional[float] = None,
    ) -> APIResponse:
        """
        POST /v6/short_close — close a short position.

        If neither close_qty nor close_pct given, closes the whole position.
        """
        params: Dict[str, Any] = {"pair": pair}
        if close_qty is not None:
            params["close_qty"] = str(close_qty)
        elif close_pct is not None:
            params["close_pct"] = str(close_pct)

        logger.info(
            "Closing short: pair=%s qty=%s pct=%s",
            pair, close_qty, close_pct,
        )

        return self._request(
            "POST", "/v6/short_close",
            params=params, signed=True, is_order=True, max_retries=1,
        )

    def get_short_positions(self) -> APIResponse:
        """GET /v6/short_positions — get all open short positions."""
        return self._request("GET", "/v6/short_positions", signed=True)

    # =======================================================================
    # Health / Stats
    # =======================================================================

    def is_exchange_running(self) -> bool:
        """Quick check if the exchange is operational."""
        try:
            resp = self.get_exchange_info()
            return resp.success and resp.data.get("IsRunning", False)
        except APIError:
            return False

    def has_valid_credentials(self) -> bool:
        """Test if API credentials are valid."""
        if not self._api_key or not self._secret_key:
            return False
        try:
            resp = self.get_balance()
            return resp.success or resp.error_message == ""
        except APIError:
            return False

    @property
    def stats(self) -> Dict[str, Any]:
        """Return API client statistics."""
        gen_tokens, ord_tokens = self._rate_limiter.tokens_remaining
        return {
            "total_requests": self._total_requests,
            "failed_requests": self._failed_requests,
            "last_request_time": self._last_request_time,
            "last_error": self._last_error,
            "server_time_offset_ms": self._server_time_offset_ms,
            "rate_limit_general_tokens": round(gen_tokens, 1),
            "rate_limit_order_tokens": round(ord_tokens, 1),
        }
