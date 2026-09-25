"""
Unit tests for Roostoo API Client.
Tests HMAC-SHA256 signature, token bucket rate limiter,
timestamp generation, headers, error handling, and endpoint methods.
"""

import time
import pytest
from unittest.mock import MagicMock, patch

from config.trading_params import BotConfig, RateLimitConfig, SecretsConfig, ExchangeConfig
from core.api_client import (
    RoostooAPIClient,
    TokenBucketRateLimiter,
    APIError,
    AuthenticationError,
    RateLimitError,
    OrderError,
    APIResponse,
)


@pytest.fixture
def mock_config():
    config = BotConfig(
        secrets=SecretsConfig(
            api_key="test_api_key_12345678",
            secret_key="test_secret_key_87654321",
        ),
        exchange=ExchangeConfig(
            base_url="https://mock-api.roostoo.com",
            initial_portfolio_value=100000.0,
        ),
        rate_limit=RateLimitConfig(
            max_requests_per_minute=120,
            max_order_requests_per_minute=30,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.05,
            backoff_multiplier=1.2,
        ),
    )
    return config


@pytest.fixture
def client(mock_config):
    return RoostooAPIClient(mock_config)


class TestSignatureAndAuth:
    def test_signature_deterministic(self, client):
        params = {"pair": "BTC/USD", "timestamp": "1700000000000"}
        sig1 = client._generate_signature(params)
        sig2 = client._generate_signature(params)
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA256 hex string

    def test_signature_sorts_keys(self, client):
        # Key order should not change signature
        params1 = {"b": "2", "a": "1", "c": "3"}
        params2 = {"a": "1", "c": "3", "b": "2"}
        assert client._generate_signature(params1) == client._generate_signature(params2)

    def test_signed_headers(self, client):
        params = {"timestamp": "1700000000000"}
        headers = client._signed_headers(params)
        assert "RST-API-KEY" in headers
        assert headers["RST-API-KEY"] == "test_api_key_12345678"
        assert "MSG-SIGNATURE" in headers
        assert len(headers["MSG-SIGNATURE"]) == 64

    def test_timestamp_length(self, client):
        ts = client._get_timestamp()
        assert len(ts) == 13
        assert int(ts) > 0


class TestRateLimiter:
    def test_rate_limiter_allows_burst(self):
        cfg = RateLimitConfig(max_requests_per_minute=60, max_order_requests_per_minute=10)
        limiter = TokenBucketRateLimiter(cfg)
        # Should allow several immediate calls (wait == 0.0)
        for _ in range(5):
            assert limiter.acquire(is_order=False) == 0.0

    def test_order_rate_limiter_exhaustion(self):
        cfg = RateLimitConfig(max_requests_per_minute=60, max_order_requests_per_minute=2)
        limiter = TokenBucketRateLimiter(cfg)
        assert limiter.acquire(is_order=True) == 0.0
        assert limiter.acquire(is_order=True) == 0.0
        # Third order in rapid succession should require a wait time > 0
        assert limiter.acquire(is_order=True) > 0.0


class TestClientEndpoints:
    def test_get_server_time_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"ServerTime": 1700000001000}
        mock_resp.text = '{"ServerTime": 1700000001000}'

        with patch.object(client._session, "get", return_value=mock_resp):
            resp = client.get_server_time()
            assert resp.success is True
            assert resp.data["ServerTime"] == 1700000001000

    def test_get_ticker_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "BTC/USD": {
                "LastPrice": 65000.0,
                "HighestBid": 64990.0,
                "LowestAsk": 65010.0,
                "Volume": 1500.5,
                "UnitVolume": 25.0,
                "Change": 2.5,
            }
        }
        mock_resp.text = "{}"

        with patch.object(client._session, "get", return_value=mock_resp):
            resp = client.get_ticker(pair="BTC/USD")
            assert resp.success is True
            assert "BTC/USD" in resp.data
            assert resp.data["BTC/USD"]["LastPrice"] == 65000.0

    def test_get_balance_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "USD": {"Free": 50000.0, "Lock": 10000.0},
            "BTC": {"Free": 0.5, "Lock": 0.0},
        }
        mock_resp.text = "{}"

        with patch.object(client._session, "get", return_value=mock_resp):
            resp = client.get_balance()
            assert resp.success is True
            assert resp.data["USD"]["Free"] == 50000.0
            assert resp.data["BTC"]["Free"] == 0.5

    def test_place_order_success(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "Success": True,
            "OrderDetail": {
                "OrderID": 98765,
                "Status": "PENDING",
                "Pair": "ETH/USD",
                "Side": "BUY",
                "Quantity": 1.5,
                "Price": 3200.0,
            }
        }
        mock_resp.text = "{}"

        with patch.object(client._session, "post", return_value=mock_resp):
            res = client.place_order(
                pair="ETH/USD",
                side="BUY",
                order_type="LIMIT",
                quantity=1.5,
                price=3200.0,
            )
            assert res.success is True
            assert res.data["OrderDetail"]["OrderID"] == 98765

    def test_place_order_limit_requires_price(self, client):
        with pytest.raises(OrderError, match="require a price"):
            client.place_order(
                pair="ETH/USD",
                side="BUY",
                order_type="LIMIT",
                quantity=1.0,
                price=None,
            )

    def test_authentication_error_handling(self, client):
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Signature check failed"
        mock_resp.json.return_value = {"error": "Signature check failed"}

        with patch.object(client._session, "get", return_value=mock_resp):
            with pytest.raises(AuthenticationError):
                client.get_balance()

    def test_retry_on_server_error(self, client):
        mock_500 = MagicMock()
        mock_500.status_code = 502
        mock_500.text = "Bad Gateway"
        mock_500.json.return_value = {"error": "Bad Gateway"}

        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.text = '{"ServerTime": 1700000005000}'
        mock_200.json.return_value = {"ServerTime": 1700000005000}

        # Fail twice with 502, then succeed
        with patch.object(client._session, "get", side_effect=[mock_500, mock_500, mock_200]) as mock_get:
            resp = client.get_server_time()
            assert resp.success is True
            assert resp.data["ServerTime"] == 1700000005000
            assert mock_get.call_count == 3
