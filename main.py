import base64
import os
import time
import uuid
from urllib.parse import urlparse
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI, HTTPException, Request
import requests

app = FastAPI()

# ---------------------------------------------------------
# CREDENTIALS & CONSTANTS
# ---------------------------------------------------------
KALSHI_API_BASE = "https://external-api.kalshi.com/trade-api/v2"
KEY_ID = (os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_KEY_ID") or "").strip()
PRIVATE_KEY_STR = os.environ.get("KALSHI_PRIVATE_KEY", "").strip()
PASSPHRASE = os.environ.get("WEBHOOK_PASSPHRASE", "my_secret_key_123")
MAX_MULTIPLIER = 8

state = {
    "mode": "LIVE",
    "multiplier": 1,
    "last_ticker": None,
    "last_side": None
}


def get_private_key():
    if not PRIVATE_KEY_STR:
        raise ValueError("Missing KALSHI_PRIVATE_KEY environment variable")
    return serialization.load_pem_private_key(PRIVATE_KEY_STR.encode("utf-8"), password=None)


def sign_request(priv_key, timestamp: str, method: str, full_path: str) -> str:
    # Sign timestamp + HTTP_METHOD + full URL path without query params
    path_without_query = full_path.split("?")[0]
    message = f"{timestamp}{method.upper()}{path_without_query}".encode("utf-8")

    if isinstance(priv_key, Ed25519PrivateKey):
        sig = priv_key.sign(message)
    else:
        sig = priv_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
    return base64.b64encode(sig).decode("utf-8")


def kalshi_request(method: str, path: str, payload: dict = None):
    priv_key = get_private_key()
    timestamp_ms = str(int(time.time() * 1000))
    sign_path = urlparse(KALSHI_API_BASE + path).path
    sig = sign_request(priv_key, timestamp_ms, method, sign_path)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms
    }

    url = KALSHI_API_BASE + path
    if method.upper() == "GET":
        return requests.get(url, headers=headers, params=payload)
    elif method.upper() == "POST":
        return requests.post(url, headers=headers, json=payload)
    elif method.upper() == "DELETE":
        return requests.delete(url, headers=headers)
    raise ValueError(f"Unsupported method: {method}")


def get_current_btc_market():
    resp = kalshi_request("GET", "/events", {
        "series_ticker": "KXBTC15M",
        "status": "open",
        "with_nested_markets": True
    })
    if resp.status_code == 200:
        events = resp.json().get("events", [])
        if events:
            events_sorted = sorted(events, key=lambda e: min([m.get("close_time", "") for m in e.get("markets", [])] or ["9999"]))
            markets = events_sorted[0].get("markets", [])
            for m in markets:
                if m.get("status") in ["active", "open"] or not m.get("status"):
                    return m.get("ticker")

    resp = kalshi_request("GET", "/markets", {
        "series_ticker": "KXBTC15M",
        "status": "open",
        "limit": 10
    })
    markets = resp.json().get("markets", [])
    if not markets:
        raise Exception("No active open KXBTC15M market found")
    markets_sorted = sorted(markets, key=lambda x: x.get("close_time", ""))
    return markets_sorted[0].get("ticker")


@app.get("/")
def root():
    # Diagnostic health check: tests credentials on startup/load
    test_result = "not tested"
    try:
        b_resp = kalshi_request("GET", "/portfolio/balance")
        test_result = f"Authenticated! Balance HTTP {b_resp.status_code}"
    except Exception as e:
        test_result = f"Auth check error: {e}"
    return {"status": "ok", "state": state, "auth_test": test_result}


@app.post("/webhook")
async def webhook(request: Request):
    global state
    try:
        data = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if data.get("passphrase") != PASSPHRASE:
        raise HTTPException(status_code=401, detail="Unauthorized passphrase")

    side = data.get("side", "").lower()
    if side not in ["yes", "no"]:
        raise HTTPException(status_code=400, detail="Side must be 'yes' or 'no'")

    try:
        kalshi_request("DELETE", "/portfolio/events/orders")
    except Exception:
        pass

    ticker = get_current_btc_market()
    contracts = state["multiplier"]
    v2_side = "bid" if side == "yes" else "ask"

    order_body = {
        "ticker": ticker,
        "client_order_id": str(uuid.uuid4()),
        "side": v2_side,
        "count": f"{float(contracts):.2f}",
        "price": "0.5000",
        "time_in_force": "good_till_canceled",
        "self_trade_prevention_type": "taker_at_cross",
        "post_only": False,
        "cancel_order_on_pause": False,
        "reduce_only": False,
        "subaccount": 0,
        "exchange_index": 0
    }

    if state["mode"] == "LIVE":
        order_resp = kalshi_request("POST", "/portfolio/events/orders", order_body)
        print(f"[LIVE ORDER V2] {ticker} | {side.upper()} ({v2_side}) | Status: {order_resp.status_code}")
        print(f"[LIVE RESPONSE] {order_resp.text}")

    state["last_ticker"] = ticker
    state["last_side"] = side

    return {"status": "success", "ticker": ticker, "side": side, "contracts": contracts}
