#!/usr/bin/env python3
"""
MEXC Futures Bot - Robust Auto-Reconnect & Early Reversal Strategy
"""

import sys
import time
import hmac
import hashlib
import json
import urllib.parse
from datetime import datetime, timezone
import requests

sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# =====================================================================
# API CONFIGURATION
# =====================================================================
MEXC_API_KEY = "mx0vglCptK1Uteb07M"
MEXC_SECRET_KEY = "c19530d6d7404a509bdff4975bef5538"

SYMBOL = "BTC_USDT"
TIMEFRAME_EXEC = "Min5"
TIMEFRAME_HTF = "Min60"
POLL_INTERVAL_SEC = 5

QUANTITY = 1
DRY_RUN = False

in_position = False
active_trade_type = None
active_entry_price = 0.0
active_sl_price = 0.0
active_tp_price = 0.0
highest_price_seen = 0.0
lowest_price_seen = 0.0
last_signal_bar_time = 0

# Persistent session to handle keep-alive and drops smoothly
session = requests.Session()

# =====================================================================
# ROBUST API REQUEST WITH SESSION & DNS ERROR RECOVERY
# =====================================================================
def mexc_request(method, endpoint, params=None):
    base_url = "https://contract.mexc.com"
    url = base_url + endpoint
    
    for attempt in range(1, 5):
        try:
            timestamp = str(int(time.time() * 1000))
            if params is None:
                params = {}
                
            sorted_params = urllib.parse.urlencode(sorted(params.items()))
            param_str = sorted_params
            
            sign_str = MEXC_API_KEY + timestamp + param_str
            signature = hmac.new(
                MEXC_SECRET_KEY.encode('utf-8'),
                sign_str.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()

            headers = {
                "ApiKey": MEXC_API_KEY,
                "Request-Time": timestamp,
                "Signature": signature,
                "Content-Type": "application/json"
            }

            if method.upper() == "GET":
                resp = session.get(url + ("?" + sorted_params if sorted_params else ""), headers=headers, timeout=20)
            elif method.upper() == "POST":
                resp = session.post(url, data=json.dumps(params), headers=headers, timeout=20)
                
            if resp.status_code == 200 and resp.text.strip():
                return resp.json()
            else:
                print(f"⚠️ MEXC Server Status {resp.status_code}: {resp.text}", flush=True)
                return None
        except requests.exceptions.RequestException as e:
            print(f"⏳ [Network Connection Drop/DNS Error] (Attempt {attempt}/4): {e}", flush=True)
            time.sleep(3)
        except Exception as e:
            print(f"❌ [API Exception] (Attempt {attempt}/4): {e}", flush=True)
            time.sleep(3)
            
    return None

def place_mexc_order(symbol, side, type_code=1, price=0.0, vol=QUANTITY):
    if DRY_RUN:
        print(f"[DRY RUN] Executing Order: {symbol} | Side: {side} | Vol: {vol}", flush=True)
        return True

    params = {
        "symbol": symbol,
        "price": float(price) if price else 0.0,
        "vol": int(vol),
        "side": int(side),
        "type": int(type_code),
        "openType": 1
    }

    res = mexc_request("POST", "/api/v1/private/order/submit", params)
    
    if res and isinstance(res, dict):
        if res.get("success") or res.get("code") == 0:
            print(f"🚀 [MEXC LIVE ORDER SUCCESS]: {res}", flush=True)
            return True
        else:
            print(f"⚠️ [MEXC REJECTED]: {res}", flush=True)
            return False
    else:
        print(f"❌ [MEXC API RESPONSE ERROR]: Empty or failed response.", flush=True)
        return False

# =====================================================================
# DATA FETCHING & INDICATORS
# =====================================================================
def get_mexc_price(symbol=SYMBOL):
    res = mexc_request("GET", f"/api/v1/contract/ticker?symbol={symbol}")
    if res and res.get("success") and "data" in res:
        return float(res["data"]["lastPrice"])
    return None

def get_mexc_klines(symbol=SYMBOL, interval=TIMEFRAME_EXEC):
    res = mexc_request("GET", f"/api/v1/contract/kline/{symbol}?interval={interval}")
    if res and res.get("success") and "data" in res:
        data = res["data"]
        time_arr = data.get("time", [])
        open_arr = data.get("open", [])
        high_arr = data.get("high", [])
        low_arr = data.get("low", [])
        close_arr = data.get("close", [])
        vol_arr = data.get("vol", [])
        
        candles = []
        for i in range(len(time_arr)):
            candles.append([
                int(time_arr[i]),
                float(open_arr[i]),
                float(high_arr[i]),
                float(low_arr[i]),
                float(close_arr[i]),
                float(vol_arr[i])
            ])
        return candles
    return []

def calculate_rsi(candles, period=14):
    if len(candles) < period + 1:
        return 50.0
    closes = [c[4] for c in candles]
    gains, losses = 0.0, 0.0
    for i in range(1, period + 1):
        diff = closes[-i] - closes[-i-1]
        if diff > 0:
            gains += diff
        else:
            losses -= diff
    avg_gain = gains / period
    avg_loss = losses / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_atr(candles, period=14):
    if len(candles) < period + 1:
        return 150.0
    tr_list = []
    for i in range(1, len(candles)):
        h, l, pc = candles[i][2], candles[i][3], candles[i-1][4]
        tr_list.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(tr_list[-period:]) / period

# =====================================================================
# STRATEGY: EARLY REVERSAL LOGIC
# =====================================================================
def check_early_reversal_signal(candles):
    global last_signal_bar_time
    if len(candles) < 20:
        return None

    curr = candles[-1]
    prev1 = candles[-2]
    c_time = curr[0]
    if c_time == last_signal_bar_time:
        return None

    rsi = calculate_rsi(candles, 14)

    recent_lows = [c[3] for c in candles[-15:-2]]
    lowest_support = min(recent_lows)
    is_bottom_sweep = (prev1[3] <= lowest_support) or (curr[3] <= lowest_support)
    is_green_reversal = (curr[4] > curr[1]) and (curr[4] > prev1[2])
    
    if is_bottom_sweep and is_green_reversal and (rsi < 60) and (rsi > 32):
        last_signal_bar_time = c_time
        return "BUY"

    recent_highs = [c[2] for c in candles[-15:-2]]
    highest_resistance = max(recent_highs)
    is_top_sweep = (prev1[2] >= highest_resistance) or (curr[2] >= highest_resistance)
    is_red_reversal = (curr[4] < curr[1]) and (curr[4] < prev1[3])

    if is_top_sweep and is_red_reversal and (rsi > 40) and (rsi < 68):
        last_signal_bar_time = c_time
        return "SELL"

    return None

# =====================================================================
# MAIN LOOP
# =====================================================================
def main():
    global in_position, active_trade_type, active_entry_price, active_sl_price, active_tp_price
    global highest_price_seen, lowest_price_seen

    print("=" * 65, flush=True)
    print("MEXC BOT - ROBUST AUTO-RECONNECT & EARLY REVERSAL ACTIVE", flush=True)
    print("=" * 65, flush=True)

    while True:
        try:
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            price = get_mexc_price(SYMBOL)
            candles = get_mexc_klines(SYMBOL, TIMEFRAME_EXEC)

            if price and len(candles) >= 20:
                atr = calculate_atr(candles, 14)

                if in_position:
                    if active_trade_type == "BUY":
                        if price > highest_price_seen:
                            highest_price_seen = price

                        if (highest_price_seen - active_entry_price) >= (atr * 1.5):
                            new_sl = max(active_sl_price, active_entry_price + (atr * 0.2))
                            if new_sl > active_sl_price:
                                active_sl_price = new_sl
                                print(f"🛡️ [TRAILING SL LONG]: New SL at ${active_sl_price:.2f}", flush=True)

                        if price <= active_sl_price:
                            print(f"\n[{now_str} UTC] 🛑 STOP-LOSS HIT! Closing BUY...", flush=True)
                            if place_mexc_order(SYMBOL, side=4):
                                in_position = False
                        elif price >= active_tp_price:
                            print(f"\n[{now_str} UTC] 🎯 TAKE-PROFIT HIT! Closing BUY...", flush=True)
                            if place_mexc_order(SYMBOL, side=4):
                                in_position = False

                    elif active_trade_type == "SELL":
                        if price < lowest_price_seen or lowest_price_seen == 0:
                            lowest_price_seen = price

                        if (active_entry_price - lowest_price_seen) >= (atr * 1.5):
                            new_sl = min(active_sl_price, active_entry_price - (atr * 0.2))
                            if new_sl < active_sl_price:
                                active_sl_price = new_sl
                                print(f"🛡️ [TRAILING SL SHORT]: New SL at ${active_sl_price:.2f}", flush=True)

                        if price >= active_sl_price:
                            print(f"\n[{now_str} UTC] 🛑 STOP-LOSS HIT! Closing SHORT...", flush=True)
                            if place_mexc_order(SYMBOL, side=2):
                                in_position = False
                        elif price <= active_tp_price:
                            print(f"\n[{now_str} UTC] 🎯 TAKE-PROFIT HIT! Closing SHORT...", flush=True)
                            if place_mexc_order(SYMBOL, side=2):
                                in_position = False
                else:
                    sig = check_early_reversal_signal(candles)

                    if sig == "BUY":
                        active_entry_price = price
                        highest_price_seen = price
                        active_sl_price = price - (atr * 1.1)
                        active_tp_price = price + (atr * 3.2)

                        print(f"\n[{now_str} UTC] 🔔 EARLY DIP BUY SIGNAL at ${price:.2f}", flush=True)
                        if place_mexc_order(SYMBOL, side=1):
                            in_position = True
                            active_trade_type = "BUY"
                            print(f"✅ Position Active | SL: ${active_sl_price:.2f} | TP: ${active_tp_price:.2f}\n", flush=True)

                    elif sig == "SELL":
                        active_entry_price = price
                        lowest_price_seen = price
                        active_sl_price = price + (atr * 1.1)
                        active_tp_price = price - (atr * 3.2)

                        print(f"\n[{now_str} UTC] 🔔 EARLY TOP SHORT SIGNAL at ${price:.2f}", flush=True)
                        if place_mexc_order(SYMBOL, side=3):
                            in_position = True
                            active_trade_type = "SELL"
                            print(f"✅ Position Active | SL: ${active_sl_price:.2f} | TP: ${active_tp_price:.2f}\n", flush=True)

                    else:
                        print(f"[{now_str} UTC] Price: ${price:.2f} | Scanning Early Reversal Signals...", flush=True)

            time.sleep(POLL_INTERVAL_SEC)

        except Exception as e:
            print(f"[Main Loop Exception Guard]: {e}", flush=True)
            time.sleep(5)

if __name__ == "__main__":
    main()
