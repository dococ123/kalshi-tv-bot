import base64
import os
import time
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import FastAPI, HTTPException, Request
import requests

app = FastAPI()

# ---------------------------------------------------------
# CREDENTIALS & CONSTANTS
# ---------------------------------------------------------
KALSHI_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KEY_ID = os.environ.get("KALSHI_API_KEY_ID") or os.environ.get("KALSHI_KEY_ID")
PRIVATE_KEY_STR = os.environ.get("KALSHI_PRIVATE_KEY")
PASSPHRASE = os.environ.get("WEBHOOK_PASSPHRASE", "my_secret_key_123")
MAX_MULTIPLIER = 8  # Risk Cap: Prevents Martingale from exceeding 8 contracts ($8)

# ---------------------------------------------------------
# BOT INTERNAL STATE
# ---------------------------------------------------------
state = {
    "mode": "LIVE",        # "LIVE" or "PAPER"
    "multiplier": 1,       # 1, 2, 4, max 8
    "last_ticker": None,   
    "last_side": None,     
    "last_was_live": True  
}

def get_private_key():
    if not PRIVATE_KEY_STR:
        raise ValueError("Missing KALSHI_PRIVATE_KEY environment variable")
    return serialization.load_pem_private_key(PRIVATE_KEY_STR.encode("utf-8"), password=None)

def sign_pss(text: str) -> str:
    key = get_private_key()
    sig = key.sign(
        text.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
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
    if method.upper() == "GET":
        return requests.get(url, headers=headers)
    elif method.upper() == "DELETE":
        return requests.delete(url, headers=headers)
    return requests.post(url, headers=headers, json=body)

def cancel_stale_orders():
    """Cancels any unfilled resting limit orders from prior cycles."""
    res = kalshi_request("GET", "/portfolio/orders?status=resting")
    if res.status_code == 200:
        orders = res.json().get("orders", [])
        for order in orders:
            order_id = order.get("order_id")
            if order_id:
                kalshi_request("DELETE", f"/portfolio/orders/{order_id}")
                print(f"[CLEANUP] Canceled resting order: {order_id}")

def evaluate_previous_trade():
    """Audits previous market settlement to route state between LIVE and PAPER."""
    if not state["last_ticker"]:
        return

    path = f"/markets/{state['last_ticker']}"
    res = kalshi_request("GET", path)
    if res.status_code == 200:
        market = res.json().get("market", {})
        result = market.get("result")
        if result:
            won = (result == state["last_side"])
            print(f"[SETTLEMENT] {state['last_ticker']} result: {result} | Choice: {state['last_side']} | Won: {won}")
            
            if state["last_was_live"]:
                if won:
                    print("[RESULT] Live WIN. Continuing streak at 1x base size.")
                    state["mode"] = "LIVE"
                    state["multiplier"] = 1
                else:
                    print("[RESULT] Live LOSS. Switching to PAPER mode to absorb losing streaks.")
                    state["mode"] = "PAPER"
            else:
                if won:
                    new_mult = min(state["multiplier"] * 2, MAX_MULTIPLIER)
                    print(f"[RESULT] Paper WIN achieved. Returning to LIVE mode at {new_mult}x size.")
                    state["mode"] = "LIVE"
                    state["multiplier"] = new_mult
                else:
                    print("[RESULT] Paper LOSS. Staying in PAPER mode.")
                    state["mode"] = "PAPER"

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    
    if payload.get("passphrase") != PASSPHRASE:
        raise HTTPException(status_code=401, detail="Unauthorized")

    side = payload.get("side", "yes").lower()

    # 1. Cancel previous unfilled limit orders so they do not stack
    cancel_stale_orders()

    # 2. Check settlement on previous market
    evaluate_previous_trade()

    # 3. Fetch active 15m contract
    path = "/markets?series_ticker=KXBTC15M&status=open"
    m_res = kalshi_request("GET", path)
    if m_res.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch active Kalshi markets")

    markets = m_res.json().get("markets", [])
    if not markets:
        return {"status": "skipped", "reason": "No open 15m market"}

    target_market = markets[0]
    ticker = target_market["ticker"]

    # 4. Virtual Paper Tracking
    if state["mode"] == "PAPER":
        print(f"[PAPER TRADE] Virtual order tracking for {ticker} | Side: {side.upper()} @ 50¢ limit")
        state["last_ticker"] = ticker
        state["last_side"] = side
        state["last_was_live"] = False
        return {"status": "paper_recorded", "ticker": ticker, "mode": "PAPER"}

    # 5. Live Limit Order Execution at 1:1 (50¢ limit price)
    contracts = 1 * state["multiplier"]
    order_body = {
        "action": "buy",
        "count": contracts,
        "side": side,
        "ticker": ticker,
        "type": "limit",
        "yes_price": 50 if side == "yes" else 50
    }

    o_res = kalshi_request("POST", "/portfolio/orders", order_body)
    state["last_ticker"] = ticker
    state["last_side"] = side
    state["last_was_live"] = True

    print(f"[LIVE ORDER] Placed resting 1:1 limit order ({contracts}x {side.upper()} @ 50¢) on {ticker} | Status: {o_res.status_code}")
    return o_res.json()
