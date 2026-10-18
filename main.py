import base64
import os
import time
import uuid
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
    private_key = get_private_key()
    signature = private_key.sign(
        text.encode("utf-8"),
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH
        ),
        hashes.SHA256()
    )
    return base64.b64encode(signature).decode("utf-8")


def kalshi_request(method: str, path: str, payload: dict = None):
    timestamp_ms = str(int(time.time() * 1000))
    msg_to_sign = timestamp_ms + method.upper() + "/trade-api/v2" + path
    sig = sign_pss(msg_to_sign)

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms
    }

    url = KALSHI_API_BASE + path
    if method.upper() == "GET":
        resp = requests.get(url, headers=headers, params=payload)
    elif method.upper() == "POST":
        resp = requests.post(url, headers=headers, json=payload)
    elif method.upper() == "DELETE":
        resp = requests.delete(url, headers=headers)
    else:
        raise ValueError(f"Unsupported method: {method}")

    return resp


def cancel_resting_orders():
    try:
        resp = kalshi_request("DELETE", "/portfolio/events/orders")
        if resp.status_code in [200, 204]:
            print("[CLEANUP] Successfully canceled resting orders.")
    except Exception as e:
        print(f"[CLEANUP ERROR] {e}")


def check_previous_settlement():
    global state
    if not state["last_ticker"]:
        return

    try:
        resp = kalshi_request("GET", f"/markets/{state['last_ticker']}")
        if resp.status_code == 200:
            m = resp.json().get("market", {})
            result = m.get("result")  # "yes" or "no"

            if result:
                if result == state["last_side"]:
                    print(f"[SETTLEMENT] WIN on {state['last_ticker']}! Resetting multiplier to 1.")
                    state["multiplier"] = 1
                else:
                    new_mult = min(state["multiplier"] * 2, MAX_MULTIPLIER)
                    print(f"[SETTLEMENT] LOSS on {state['last_ticker']}. Multiplier increased to {new_mult}.")
                    state["multiplier"] = new_mult
                state["last_ticker"] = None
                state["last_side"] = None
    except Exception as e:
        print(f"[SETTLEMENT CHECK ERROR] {e}")


def get_current_btc_market():
    resp = kalshi_request("GET", "/markets", {
        "series_ticker": "KXBTC15M",
        "status": "open",
        "limit": 5
    })
    if resp.status_code != 200:
        raise Exception(f"Failed to fetch KXBTC15M markets: {resp.text}")

    markets = resp.json().get("markets", [])
    if not markets:
        raise Exception("No active open KXBTC15M market found")

    markets_sorted = sorted(markets, key=lambda x: x.get("close_time", ""))
    return markets_sorted[0].get("ticker")


@app.get("/")
def root():
    return {"status": "ok", "service": "kalshi-tradingview-bot", "state": state}


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

    # 1. Housekeeping: Cancel unfilled resting orders & evaluate previous settlement
    cancel_resting_orders()
    check_previous_settlement()

    # 2. Identify the active 15m contract
    ticker = get_current_btc_market()
    contracts = state["multiplier"]

    # 3. Kalshi V2 Order Shape: bid = Buy YES, ask = Buy NO / Sell YES
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
        print(f"[LIVE ORDER V2] Ticker: {ticker} | Side: {side.upper()} ({v2_side}) | Qty: {contracts} | Status: {order_resp.status_code}")
        print(f"[LIVE RESPONSE] {order_resp.text}")
        state["last_was_live"] = True
    else:
        print(f"[PAPER ORDER] Ticker: {ticker} | Side: {side.upper()} | Qty: {contracts} @ 50¢")
        state["last_was_live"] = False

    # Store position context for next settlement check
    state["last_ticker"] = ticker
    state["last_side"] = side

    return {
        "status": "success",
        "mode": state["mode"],
        "ticker": ticker,
        "side": side,
        "contracts": contracts,
        "price": 0.50
    }
