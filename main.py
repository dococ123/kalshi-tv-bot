import base64
import os
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import FastAPI, HTTPException, Request
import requests

app = FastAPI()

KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.getenv("KALSHI_KEY_ID")
PRIVATE_KEY_STR = os.getenv("KALSHI_PRIVATE_KEY")
PASSPHRASE = os.getenv("WEBHOOK_PASSPHRASE")

def get_private_key():
    return serialization.load_pem_private_key(PRIVATE_KEY_STR.encode("utf-8"), password=None)

def sign_pss(text: str) -> str:
    key = get_private_key()
    sig = key.sign(
        text.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256()
    )
    return base64.b64encode(sig).decode("utf-8")

def kalshi_request(method: str, path: str, body: dict = None):
    ts = str(int(time.time() * 1000))
    sig = sign_pss(ts + method.upper() + path)
    headers = {
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "Content-Type": "application/json",
    }
    url = f"{KALSHI_API_BASE}{path}"
    if method.upper() == "POST":
        return requests.post(url, json=body, headers=headers)
    return requests.get(url, headers=headers)

def get_active_btc_15m_ticker():
    res = kalshi_request("GET", "/events?series_ticker=KXBTC15M&status=open")
    if res.status_code == 200:
        events = res.json().get("events", [])
        if events and events[0].get("markets"):
            return events[0]["markets"][0].get("ticker")
    return None

@app.post("/webhook")
async def handle_alert(request: Request):
    data = await request.json()
    if data.get("passphrase") != PASSPHRASE:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    ticker = get_active_btc_15m_ticker()
    if not ticker:
        return {"status": "error", "message": "No active KXBTC15M market found"}

    side = data.get("side", "yes").lower()
    count = int(data.get("count", 1))
    max_cost = int(data.get("max_cost", 42))

    order = {
        "action": "buy",
        "ticker": ticker,
        "type": "limit",
        "side": side,
        "count": count,
        "yes_price": max_cost if side == "yes" else (100 - max_cost),
        "client_order_id": str(int(time.time())),
    }
    res = kalshi_request("POST", "/portfolio/orders", order)
    return {"status": "ok", "kalshi": res.json()}
