import os
import time
from fastapi import FastAPI, HTTPException, Request
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
import base64

app = FastAPI()

# ---------------------------------------------------------
# CONFIGURATION & CREDENTIALS
# ---------------------------------------------------------
WEBHOOK_PASSPHRASE = os.environ.get("WEBHOOK_PASSPHRASE", "my_secret_key_123")
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY")
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# ---------------------------------------------------------
# BOT INTERNAL STATE (In-Memory Streak & Bankroll Tracking)
# ---------------------------------------------------------
state = {
    "mode": "LIVE",              # "LIVE" or "PAPER"
    "multiplier": 1,             # Martingale level: 1, 2, 4, 8...
    "last_ticker": None,         # Last traded Kalshi ticker
    "last_side": None,           # "yes" or "no"
    "last_was_live": True        # Was the last trade actual money or virtual
}

# ---------------------------------------------------------
# KALSHI RSA AUTHENTICATION
# ---------------------------------------------------------
def get_kalshi_headers(method: str, path: str):
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY_PEM:
        raise ValueError("Missing Kalshi API credentials in environment.")
    
    timestamp = str(int(time.time() * 1000))
    msg = f"{timestamp}{method}{path}".encode("utf-8")
    
    private_key = serialization.load_pem_private_key(
        KALSHI_PRIVATE_KEY_PEM.encode("utf-8"),
        password=None
    )
    signature = private_key.sign(
        msg,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH
        ),
        hashes.SHA256()
    )
    sig_b64 = base64.b64encode(signature).decode("utf-8")
    
    return {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }

# ---------------------------------------------------------
# SETTLEMENT EVALUATOR
# ---------------------------------------------------------
def evaluate_previous_trade():
    """Checks whether the last tracked market resolved YES or NO."""
    if not state["last_ticker"]:
        return

    path = f"/markets/{state['last_ticker']}"
    headers = get_kalshi_headers("GET", path)
    res = requests.get(f"{KALSHI_BASE_URL}{path}", headers=headers)
    
    if res.status_code == 200:
        data = res.json().get("market", {})
        result = data.get("result") # "yes", "no", or None if still active
        
        if result:
            won = (result == state["last_side"])
            print(f"[SETTLEMENT] {state['last_ticker']} result: {result} | Bot side: {state['last_side']} | Won: {won}")
            
            if state["last_was_live"]:
                if won:
                    print("[RESULT] Live WIN. Continuing live streak at base size.")
                    state["mode"] = "LIVE"
                    state["multiplier"] = 1
                else:
                    print("[RESULT] Live LOSS. Switching to PAPER tracking mode to absorb losing streaks.")
                    state["mode"] = "PAPER"
            else: # Last trade was paper
                if won:
                    print("[RESULT] Paper WIN recorded. Regime restored -> Resuming LIVE on next setup (Doubled).")
                    state["mode"] = "LIVE"
                    state["multiplier"] *= 2
                else:
                    print("[RESULT] Paper LOSS. Staying in PAPER mode until a paper win appears.")
                    state["mode"] = "PAPER"

# ---------------------------------------------------------
# WEBHOOK ENDPOINT
# ---------------------------------------------------------
@app.post("/webhook")
async def receive_signal(request: Request):
    payload = await request.json()
    
    # 1. Security check
    if payload.get("passphrase") != WEBHOOK_PASSPHRASE:
        raise HTTPException(status_code=401, detail="Invalid passphrase")

    side = payload.get("side", "yes").lower() # "yes" or "no"
    series_ticker = "KXBTC15M"

    # 2. Check previous market result before acting on new setup
    evaluate_previous_trade()

    # 3. Fetch currently open 15m BTC contract
    headers = get_kalshi_headers("GET", f"/markets?series_ticker={series_ticker}&status=open")
    m_res = requests.get(f"{KALSHI_BASE_URL}/markets?series_ticker={series_ticker}&status=open", headers=headers)
    
    if m_res.status_code != 200:
        raise HTTPException(status_code=500, detail="Failed to fetch active Kalshi markets")
    
    markets = m_res.json().get("markets", [])
    if not markets:
        return {"status": "skipped", "reason": "No open 15m market available right now"}
    
    target_market = markets[0]
    ticker = target_market["ticker"]
    
    # 4. Check 1:1 Risk/Reward Rule (Must be <= 50 cents)
    current_price = target_market.get(f"{side}_ask", 100)
    if current_price > 50:
        print(f"[REJECTED 1:1] {ticker} {side.upper()} ask price is {current_price}¢ (> 50¢). Skipping trade.")
        return {"status": "skipped", "reason": f"Entry price {current_price}¢ exceeds 1:1 max of 50¢"}

    # 5. Route between LIVE and PAPER
    if state["mode"] == "PAPER":
        print(f"[PAPER TRADE] Virtual order recorded for {ticker} | Side: {side.upper()} @ {current_price}¢")
        state["last_ticker"] = ticker
        state["last_side"] = side
        state["last_was_live"] = False
        return {"status": "paper_recorded", "ticker": ticker, "mode": "PAPER"}

    # 6. Execute LIVE Order (Base count * Martingale multiplier)
    contracts = 1 * state["multiplier"]
    order_payload = {
        "action": "buy",
        "count": contracts,
        "side": side,
        "ticker": ticker,
        "type": "limit",
        "yes_price": current_price if side == "yes" else (100 - current_price)
    }

    order_headers = get_kalshi_headers("POST", "/portfolio/orders")
    o_res = requests.post(f"{KALSHI_BASE_URL}/portfolio/orders", json=order_payload, headers=order_headers)
    
    state["last_ticker"] = ticker
    state["last_side"] = side
    state["last_was_live"] = True

    print(f"[LIVE ORDER] Placed {contracts}x {side.upper()} on {ticker} @ {current_price}¢ | Status: {o_res.status_code}")
    return o_res.json()
