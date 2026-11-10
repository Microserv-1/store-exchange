from datetime import datetime, timezone
import json
import os
import threading
import time
from typing import Callable, Optional
from fastapi import Depends, FastAPI, HTTPException, Response
import httpx
from prometheus_client import Counter
from .common import identity, instrument, logger

SUPPORTED_CURRENCIES = {
    "USD",
    "BRL",
    "EUR",
    "GBP",
    "JPY",
    "CAD",
    "AUD",
    "CHF",
    "ARS",
    "CNY",
    "MXN",
}

AWESOMEAPI_BASE_URL = os.environ.get("AWESOMEAPI_BASE_URL", "https://economia.awesomeapi.com.br/json/last")
OPEN_ER_BASE_URL = os.environ.get("OPEN_ER_BASE_URL", "https://open.er-api.com/v6/latest")
CACHE_TTL_SECONDS = int(os.environ.get("EXCHANGE_CACHE_TTL_SECONDS", "60"))

EXTERNAL_REQUESTS = Counter(
    "exchange_external_requests_total",
    "External exchange rate provider calls",
    ["provider", "status"],
)

app = FastAPI(title="Store Exchange")
instrument(app)

_quote_cache: dict[tuple[str, str], tuple[dict, float]] = {}
_cache_lock = threading.Lock()


def now_formatted() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def normalize_currency(code: str) -> str:
    cleaned = code.strip().upper()
    if len(cleaned) != 3 or not cleaned.isalpha() or cleaned not in SUPPORTED_CURRENCIES:
        raise HTTPException(422, f"Unsupported currency code: {code}")
    return cleaned


def fetch_from_awesomeapi(client: httpx.Client, src: str, dst: str) -> Optional[dict]:
    url = f"{AWESOMEAPI_BASE_URL.rstrip('/')}/{src}-{dst}"
    response = client.get(url)
    if response.status_code != 200:
        EXTERNAL_REQUESTS.labels(provider="awesomeapi", status=str(response.status_code)).inc()
        return None
    data = response.json()
    pair_key = f"{src}{dst}"
    quote = data.get(pair_key) or (next(iter(data.values()), None) if isinstance(data, dict) else None)
    if not isinstance(quote, dict) or "ask" not in quote or "bid" not in quote:
        EXTERNAL_REQUESTS.labels(provider="awesomeapi", status="invalid_payload").inc()
        return None
    sell = round(float(quote["ask"]), 4)
    buy = round(float(quote["bid"]), 4)
    if sell <= 0 or buy <= 0:
        EXTERNAL_REQUESTS.labels(provider="awesomeapi", status="invalid_rate").inc()
        return None
    EXTERNAL_REQUESTS.labels(provider="awesomeapi", status="200").inc()
    return {
        "sell": sell,
        "buy": buy,
        "date": str(quote.get("create_date") or now_formatted()),
        "source": "awesomeapi",
    }


def fetch_from_open_er(client: httpx.Client, src: str, dst: str) -> Optional[dict]:
    url = f"{OPEN_ER_BASE_URL.rstrip('/')}/{src}"
    response = client.get(url)
    if response.status_code != 200:
        EXTERNAL_REQUESTS.labels(provider="open_er_api", status=str(response.status_code)).inc()
        return None
    data = response.json()
    rates = data.get("rates") if isinstance(data, dict) else None
    if not isinstance(rates, dict) or dst not in rates:
        EXTERNAL_REQUESTS.labels(provider="open_er_api", status="missing_currency").inc()
        return None
    mid = float(rates[dst])
    if mid <= 0:
        EXTERNAL_REQUESTS.labels(provider="open_er_api", status="invalid_rate").inc()
        return None
    # Apply a standard 0.5% spread around mid-market when provider supplies single rate
    sell = round(mid * 1.005, 4)
    buy = round(mid * 0.995, 4)
    EXTERNAL_REQUESTS.labels(provider="open_er_api", status="200").inc()
    return {
        "sell": sell,
        "buy": buy,
        "date": now_formatted(),
        "source": "open-er-api",
    }


def default_http_client_factory() -> httpx.Client:
    return httpx.Client(timeout=httpx.Timeout(4.0, connect=2.0), follow_redirects=False)


http_client_factory: Callable[[], httpx.Client] = default_http_client_factory


def clear_quote_cache() -> None:
    with _cache_lock:
        _quote_cache.clear()


def resolve_quote(src: str, dst: str) -> dict:
    if src == dst:
        return {"sell": 1.0, "buy": 1.0, "date": now_formatted(), "source": "identity"}

    now = time.monotonic()
    with _cache_lock:
        cached = _quote_cache.get((src, dst))
        if cached is not None and now < cached[1]:
            return {**cached[0], "source": f"{cached[0]['source']}-cached"}

    errors = []
    with http_client_factory() as client:
        for provider_name, fetcher in (("awesomeapi", fetch_from_awesomeapi), ("open_er_api", fetch_from_open_er)):
            try:
                quote = fetcher(client, src, dst)
                if quote is not None:
                    with _cache_lock:
                        _quote_cache[(src, dst)] = (quote, time.monotonic() + CACHE_TTL_SECONDS)
                    return quote
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                EXTERNAL_REQUESTS.labels(provider=provider_name, status="error").inc()
                errors.append(f"{provider_name}: {exc}")

    logger.error(json.dumps({"service": "exchange", "event": "external_provider_failure", "pair": f"{src}-{dst}", "errors": errors}))
    raise HTTPException(503, "External exchange provider unavailable")


@app.get("/ready")
def ready():
    return {"status": "ready", "supported_currencies": sorted(SUPPORTED_CURRENCIES)}


@app.get("/exchanges/{from_currency}/{to_currency}")
@app.get("/exchange/{from_currency}/{to_currency}", include_in_schema=False)
def get_exchange_rate(
    from_currency: str,
    to_currency: str,
    response: Response,
    account_id: str = Depends(identity),
):
    src = normalize_currency(from_currency)
    dst = normalize_currency(to_currency)
    quote = resolve_quote(src, dst)
    response.headers["X-Exchange-Source"] = quote["source"]
    return {
        "sell": quote["sell"],
        "buy": quote["buy"],
        "date": quote["date"],
        "id-account": account_id,
    }
