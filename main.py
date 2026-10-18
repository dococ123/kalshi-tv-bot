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

# ---------------------------------------------------------
# STATE MACHINE (Streak Rider & Martingale on Paper Win)
# ---------------------------------------------------------
state = {
    "mode": "LIVE",        # "LIVE" or "PAPER"
    "multiplier": 1,       # Martingale level: 1, 2, 4, 8...
    "last_ticker": None,   # Last Kalshi market
    "last_side": None,     # "yes" or "no"
    "last_was_live": True  # True if placed with real money, False if paper
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
    return requests.post(url, headers=headers, json=body)

def evaluate_previous_trade():
    """Checks settlement of the previous market to evaluate win/loss."""
    if not state["last_ticker"]:
        return

    path = f"/markets/{state['last_ticker']}"
    res = kalshi_request("GET", path)
    if res.status_code == 200:
        market = res.json().get("market", {})
        result = market.get("result") # "yes", "no", or None if open
        if result:
            won = (result == state["last_side"])
            print(f"[SETTLEMENT] Market: {state['last_ticker']} | Result: {result} | Bot Side: {state['last_side']} | Won: {won}")
            
            if state["last_was_live"]:
                if won:
                    print("[RESULT] Live WIN. Keeping live streak active at base size.")
                    state["mode"] = "LIVE"
                    state["multiplier"] = 1
                else:
                    print("[RESULT] Live LOSS. Switching to PAPER mode to avoid streak losses.")
                    state["mode"] = "PAPER"
            else:
                if won:
                    print("[RESULT] Paper WIN achieved. Resuming LIVE mode with doubled size.")
                    state["mode"] = "LIVE"
                    state["multiplier"] *= 2
                else:
                    print("[RESULT] Paper LOSS. Remaining in PAPER mode until next paper win.")
                    state["mode"] = "PAPER"

@app.post("/webhook")
async def webhook(request: Request):
    payload = await request.json()
    
    # 1. Verify passphrase
    if payload.get("passphrase") != PASSPHRASE:
        raise HTTPException(status_code=401, detail="Unauthorized")

    side = payload.get("side", "yes").lower()

    # 2. Check previous settlement to maintain streak state
    evaluate_previous_trade()

    # 3. Locate active 15m BTC contract
    path = "/markets?series_ticker=KXBTC15M&status=open"
    m_res = kalshi_request("GET", path)
    if m_res.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch Kalshi markets")

    markets = m_res.json().get("markets", [])
    if not markets:
        return {"status": "skipped", "reason": "No open 15m markets available"}

    target_market = markets[0]
    ticker = target_market["ticker"]

    # 4. Handle Paper Mode (Virtual Tracking)
    if state["mode"] == "PAPER":
        print(f"[PAPER TRADE] Virtual order recorded for {ticker} | Side: {side.upper()} (Targeting 1:1 <= 50¢)")
        state["last_ticker"] = ticker
        state["last_side"] = side
        state["last_was_live"] = False
        return {"status": "paper_recorded", "ticker": ticker, "mode": "PAPER"}

    # 5. Execute LIVE Order with a 50¢ Rest Limit Order
    # Setting limit price to 50¢ ensures fill ONLY occurs if price is 1:1 or better (<= 50¢).
    # If market is > 50¢, the order rests on Kalshi's book waiting for the dip.
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
