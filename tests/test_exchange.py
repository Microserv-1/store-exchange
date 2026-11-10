import os
import time
import uuid

os.environ["JWT_SECRET"] = "exchange-unit-test-secret-with-32-plus-bytes"
os.environ["EXCHANGE_CACHE_TTL_SECONDS"] = "60"

from fastapi.testclient import TestClient
import httpx
import jwt
import app.main as exchange_main
from app.main import app, clear_quote_cache


def make_token(sub: str | None = None, secret: str | None = None) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": sub or str(uuid.uuid4()),
            "iss": "store-auth",
            "aud": "store-api",
            "iat": now,
            "exp": now + 600,
        },
        secret or os.environ["JWT_SECRET"],
        algorithm="HS256",
    )


def test_exchange_authenticated_quote_fallback_and_validation():
    clear_quote_cache()
    account_id = str(uuid.uuid4())
    auth = {"Authorization": f"Bearer {make_token(sub=account_id)}"}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "economia.awesomeapi.com.br/json/last/USD-BRL" in url:
            return httpx.Response(
                200,
                json={
                    "USDBRL": {
                        "code": "USD",
                        "codein": "BRL",
                        "bid": "5.80",
                        "ask": "6.00",
                        "create_date": "2026-11-10 14:23:42",
                    }
                },
            )
        if "economia.awesomeapi.com.br/json/last/USD-EUR" in url:
            return httpx.Response(503, text="AwesomeAPI unavailable")
        if "open.er-api.com/v6/latest/USD" in url:
            return httpx.Response(
                200,
                json={"result": "success", "rates": {"EUR": 0.81}},
            )
        return httpx.Response(503, text="Provider down")

    exchange_main.http_client_factory = lambda: httpx.Client(transport=httpx.MockTransport(mock_handler))

    with TestClient(app) as client:
        assert client.get("/health-check").status_code == 200
        assert client.get("/ready").status_code == 200
        assert client.get("/exchanges/USD/BRL").status_code == 401

        # Primary provider (AwesomeAPI) on canonical route /exchanges/{from}/{to}
        res = client.get("/exchanges/USD/BRL", headers=auth)
        assert res.status_code == 200
        body = res.json()
        assert body["sell"] == 6.0
        assert body["buy"] == 5.8
        assert body["date"] == "2026-11-10 14:23:42"
        assert body["id-account"] == account_id
        assert res.headers.get("X-Exchange-Source") == "awesomeapi"

        # Alias route /exchange/{from}/{to} hits cache and returns same quote
        alias_res = client.get("/exchange/USD/BRL", headers=auth)
        assert alias_res.status_code == 200
        assert alias_res.json()["sell"] == 6.0
        assert alias_res.headers.get("X-Exchange-Source") == "awesomeapi-cached"

        # Fallback provider (Open ER API) when AwesomeAPI fails for USD/EUR
        eur_res = client.get("/exchanges/USD/EUR", headers=auth)
        assert eur_res.status_code == 200
        eur_body = eur_res.json()
        assert eur_body["sell"] > eur_body["buy"] > 0
        assert eur_body["id-account"] == account_id
        assert eur_res.headers.get("X-Exchange-Source") == "open-er-api"

        # Same currency identity conversion
        same_res = client.get("/exchanges/USD/USD", headers=auth)
        assert same_res.status_code == 200
        assert same_res.json()["sell"] == 1.0
        assert same_res.json()["buy"] == 1.0

        # Unsupported currency returns 422
        assert client.get("/exchanges/USD/XYZ", headers=auth).status_code == 422
        assert client.get("/exchanges/INVALID/BRL", headers=auth).status_code == 422

        # Total external failure returns 503 when not cached
        clear_quote_cache()
        assert client.get("/exchanges/USD/JPY", headers=auth).status_code == 503
